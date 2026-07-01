"""Filesystem leader-election for a parallel batch (Claude Code, per-process).

CC fires one PreToolUse hook process per gated call in a parallel batch; the
siblings share a message.id (the batch_id). The first to claim an atomic lock on
that id is the LEADER — it reviews the whole batch in one pass (one review() over
all gated actions) and writes every verdict to a shared file keyed by tool_use_id.
The others (FOLLOWERS) read their own verdict from that file.

Correctness does not depend on the leader having seen a complete batch. The
verdicts file is published atomically (temp + rename), so once it is visible it is
the COMPLETE set the leader reviewed. A follower therefore:
  - keeps polling only while the file is ABSENT (leader still working);
  - returns its verdict the moment the file appears WITH its id;
  - degrades immediately (returns None) if the file appears WITHOUT its id — the
    leader's view fragmented past this sibling, so the caller reviews it alone.
Never a wait-forever on a missing id. Transport only — agent-neutral review() is
untouched.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from ...contracts import Decision, DecisionOp, ScopeRef, UndiscussedDecision, Verdict


def _dir(gadfly_dir: Path) -> Path:
    return Path(gadfly_dir) / "batch"


def _slug(batch_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", batch_id)[:120] or "batch"


def claim_leader(gadfly_dir: Path, batch_id: str) -> bool:
    """Atomic claim: the first process to create the lock for this batch leads."""
    d = _dir(gadfly_dir)
    d.mkdir(parents=True, exist_ok=True)
    try:
        (d / f"{_slug(batch_id)}.lock").open("x").close()
        return True
    except FileExistsError:
        return False


def _verdict_from_dict(d: dict) -> Verdict:
    u = d.get("undiscussed")
    return Verdict(
        decision=Decision(d["decision"]),
        note=d.get("note"),
        undiscussed=UndiscussedDecision(**u) if u else None,
        ops=[DecisionOp(**{**o, "scope": [ScopeRef(**s) for s in o.get("scope") or []]})
             for o in d.get("ops") or []],
    )


def write_verdicts(gadfly_dir: Path, batch_id: str, verdicts_by_id: dict) -> None:
    """Publish the leader's full verdict map atomically — once visible, complete.
    asdict() leaves `Decision` as-is, but it's a `str` Enum, so json.dumps writes it
    as its string value with no encoder hook (same as the session store relies on)."""
    d = _dir(gadfly_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_slug(batch_id)}.verdicts.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({k: asdict(v) for k, v in verdicts_by_id.items()}))
    tmp.replace(path)


def read_verdict(gadfly_dir: Path, batch_id: str, tool_use_id: str, *,
                 timeout: float, interval: float = 0.1) -> Optional[Verdict]:
    """A follower's verdict. None means 'review this action yourself' — either the
    leader vanished (timeout, file never appeared) or it reviewed a batch that
    didn't include this id (file present, id absent → degrade now, don't wait)."""
    path = _dir(gadfly_dir) / f"{_slug(batch_id)}.verdicts.json"
    deadline = time.monotonic() + timeout
    while True:
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                data = None
            if isinstance(data, dict):  # visible ⇒ complete: present → use it, absent → degrade now
                try:
                    d = data.get(tool_use_id)
                    return _verdict_from_dict(d) if d is not None else None
                except (KeyError, TypeError, ValueError, AttributeError):
                    return None  # malformed entry → degrade, never crash the follower hook
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)
