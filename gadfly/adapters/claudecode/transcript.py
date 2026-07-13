"""Claude Code adapter — read the in-flight turn from transcript_path.

At PreToolUse the current turn lags ~100ms-1s behind the hook but flushes BEFORE
execution (validated by the Phase-0 spike), so a short poll on the triggering
tool_use_id catches it — yielding the builder's reasoning AND every sibling call in
a parallel batch. In the real transcript each tool_use is its OWN JSONL record, and
parallel siblings share one `message.id` — that id is the batch key the hook uses
for leader-election.

This module only TRANSLATES Claude Code's transcript format into neutral pieces and
hands them over COMPLETE — the batch of raw calls and the turn's conversation as
whole entries. It does not select, slice, or cap anything: building the unified
session file and grabbing the intent slice for a supervisor are the core's job
(the core never sees this native format). Claude-Code-specific by design.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ...contracts import ConvoEntry


@dataclass
class RawCall:
    tool_use_id: str
    tool_name: str
    tool_input: dict[str, Any]


@dataclass
class TurnView:
    found: bool                       # did the triggering tool_use_id appear before timeout?
    batch_id: Optional[str] = None    # message.id grouping the parallel siblings (leader key)
    batch: list[RawCall] = field(default_factory=list)       # all sibling calls, in order
    messages: list[ConvoEntry] = field(default_factory=list)  # the session's convo, complete & uncut


def _read(path: str) -> list[dict]:
    out: list[dict] = []
    try:
        with open(path) as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a still-flushing / torn line loses one record, not the whole read
    except OSError:
        return []
    return out


def _content(rec: dict) -> list:
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return content if isinstance(content, list) else []


def _message_id_of(records: list[dict], tool_use_id: str) -> Optional[str]:
    """The message.id of the assistant message whose batch contains this call."""
    for rec in records:
        for b in _content(rec):
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") == tool_use_id:
                msg = rec.get("message")
                return msg.get("id") if isinstance(msg, dict) else None
    return None


def batch_of(records: list[dict], message_id: str) -> list[RawCall]:
    """All tool calls under one assistant message.id, in transcript order — the
    parallel batch. CC splits each call into its own record; message.id groups them."""
    out: list[RawCall] = []
    for rec in records:
        msg = rec.get("message")
        if not (isinstance(msg, dict) and msg.get("id") == message_id):
            continue
        for b in _content(rec):
            if isinstance(b, dict) and b.get("type") == "tool_use":
                out.append(RawCall(b.get("id"), b.get("name", ""), b.get("input") or {}))
    return out


def session_messages(records: list[dict]) -> list[ConvoEntry]:
    """The WHOLE session's conversation as complete neutral entries: user prompts
    and assistant text/thinking, in order. No cap, no truncation — the core stores
    them (deduped, so re-handing the session each gate is idempotent) and decides
    what slice a supervisor sees. Whole-session, not current-turn, so a mid-session
    install isn't blind to everything said before the first gate. Harness-injected
    user records (\"<system-reminder>\"-style) are noise, not conversation — skipped.
    An AskUserQuestion answer arrives as a user record whose content is a tool_result,
    not a string; its summary is the user's decision, so it's captured too — otherwise
    a supervisor is blind to every choice the builder gathered by asking."""
    out: list[ConvoEntry] = []
    ask_ids: set[str] = set()  # AskUserQuestion tool_use ids → their results are user answers
    for rec in records:
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        content = msg.get("content")
        if rec.get("type") == "user":
            if isinstance(content, str):
                if content.strip() and not content.lstrip().startswith("<"):
                    out.append(ConvoEntry("user", "text", content))
                continue
            for b in content if isinstance(content, list) else []:
                if (isinstance(b, dict) and b.get("type") == "tool_result"
                        and b.get("tool_use_id") in ask_ids):
                    text = _result_text(b.get("content"))
                    if text:
                        out.append(ConvoEntry("user", "text", text))
            continue
        if rec.get("type") != "assistant":
            continue
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion":
                if b.get("id"):
                    ask_ids.add(b["id"])
            elif b.get("type") == "text" and b.get("text"):
                out.append(ConvoEntry("assistant", "text", b["text"]))
            elif b.get("type") == "thinking" and b.get("thinking"):
                out.append(ConvoEntry("assistant", "thinking", b["thinking"]))
    return out


def _result_text(content) -> str:
    """The text of a tool_result whose content is a string or a list of blocks."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p).strip()
    return ""


_TAIL_BYTES = 256 * 1024  # initial batch-detection tail; doubles if a batch spans more


def _read_tail(path: str, nbytes: int) -> tuple[list[dict], bool]:
    """The last `nbytes` of the transcript as complete JSONL records, plus whether the
    read reached the file start. A mid-record seek leaves a partial leading line (dropped);
    a still-flushing final line fails to parse and is skipped — the poll retries."""
    try:
        start = max(0, os.path.getsize(path) - nbytes)
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read()
    except OSError:
        return [], True
    lines = data.decode("utf-8", "replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # partial first line from seeking mid-record
    out: list[dict] = []
    for line in lines:
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out, start == 0


def _msg_id(rec: dict) -> Optional[str]:
    msg = rec.get("message")
    return msg.get("id") if isinstance(msg, dict) else None


def _turn_tail(path: str, min_bytes: int = _TAIL_BYTES) -> list[dict]:
    """The transcript TAIL — read O(current turn), not O(whole file), never into older
    history. The current turn is the last assistant message; the window widens ONLY while it
    sits entirely inside that message. Once the message's START is in view (an older/absent
    message.id above it, or the file start), the whole current turn is present: if the
    triggering call is there, so is its entire message.id batch — return it; if it's absent
    it simply hasn't flushed — return and let the outer poll retry (widening can't surface an
    unflushed record, so we don't waste reads on it). Widening within the message means a big
    later sibling can't hide an earlier triggering call (or the batch start) past the window,
    so a huge parallel batch is never truncated — while a normal turn finishes in one read."""
    want = min_bytes
    while True:
        recs, at_start = _read_tail(path, want)
        if not recs:
            if at_start:
                return recs   # empty file
            want *= 2          # window landed inside one huge record — widen
            continue
        cur = _msg_id(recs[-1])                                  # id of the current message
        if at_start or cur is None or _msg_id(recs[0]) != cur:
            return recs        # current message's start is in view — batch complete, or unflushed
        want *= 2              # window still entirely inside the current message — widen


def _settle_batch(transcript_path: str, message_id: str, settle: float,
                  interval: float) -> list[RawCall]:
    """Once the triggering call appears, the sibling tool_use records may still be
    flushing one at a time. Re-read until the batch stops growing for one interval
    (or `settle` elapses), so leader-election sees the WHOLE batch, not a fragment."""
    batch = batch_of(_turn_tail(transcript_path), message_id)
    deadline = time.monotonic() + settle
    while time.monotonic() < deadline:
        time.sleep(interval)
        again = batch_of(_turn_tail(transcript_path), message_id)
        if len(again) == len(batch):
            break
        batch = again
    return batch


def poll_turn(transcript_path: Optional[str], tool_use_id: str, *,
              timeout: float = 3.0, interval: float = 0.05, settle: float = 0.3) -> TurnView:
    """Poll until the triggering tool_use_id appears (the in-flight turn flushes before
    execution), then settle the batch. Batch detection reads only the transcript TAIL
    (O(turn), not O(whole file)) so it stays fast — and reliable — as the session grows.
    Conversation capture is a SEPARATE whole-file read (uncut, whole-session — never the
    tail). On timeout, `found` is False and the caller degrades to per-call review; the
    conversation is still captured, off the poll's clock."""
    if not transcript_path:
        return TurnView(found=False)
    deadline = time.monotonic() + timeout
    while True:
        message_id = _message_id_of(_turn_tail(transcript_path), tool_use_id)
        if message_id is not None:
            return TurnView(found=True, batch_id=message_id,
                            batch=_settle_batch(transcript_path, message_id, settle, interval),
                            messages=session_messages(_read(transcript_path)))
        if time.monotonic() >= deadline:
            # Capture the conversation even when batch detection misses — the transcript
            # holds it regardless of whether THIS tool_use flushed, so the store never lags.
            return TurnView(found=False, messages=session_messages(_read(transcript_path)))
        time.sleep(interval)
