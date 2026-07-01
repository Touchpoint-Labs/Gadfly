#!/usr/bin/env python3
"""Claude Code PreToolUse hook — Gadfly's live gate.

Reads the tool call from stdin, normalizes it, and for actions that need review
polls the transcript for the turn's conversation, runs review(), and prints the
verdict as a permissionDecision. Non-mutating / routine actions short-circuit to an
allow with no poll and no LLM. Per-process: it builds the pieces fresh each call.

Configure in the supervised project's .claude/settings.json as a PreToolUse hook:
    <venv>/bin/python -m gadfly.adapters.claudecode.hooks.pretooluse
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from dataclasses import replace
from pathlib import Path

from gadfly.adapters.claudecode import batch
from gadfly.adapters.claudecode.normalize import normalize
from gadfly.adapters.claudecode.transcript import TurnView, poll_turn
from gadfly.adapters.claudecode.verdict import defer, to_hook_output
from gadfly.config import Config, load
from gadfly.contracts import InterventionEvent, Verdict
from gadfly.core import review
from gadfly.factory import build_provider, build_reviewers, build_route_fn, build_store
from gadfly.router import managed_doc_verdict
from gadfly.worker import maybe_start_digest_worker, maybe_start_feedback_worker


def _emit(obj: dict) -> None:
    print(json.dumps(obj))


def _gated_calls(view: TurnView, route_fn):
    """The batch's calls that THIS hook layer actually gates — normalized, non-
    terminal (a sibling Read/routine command needs no review and fired no blocking
    hook). These are exactly the ids whose hooks are waiting on a verdict."""
    out = []
    for call in view.batch:
        action = normalize(call.tool_name, call.tool_input)
        if action is None or route_fn(action).terminal is not None:
            continue
        out.append((call.tool_use_id, action))
    return out


def _review_one(action, session: str, cwd: str, config: Config, messages) -> Verdict:
    store = build_store(cwd)
    store.append_convo(session, messages)
    maybe_start_digest_worker(Path(cwd), session)
    maybe_start_feedback_worker(Path(cwd), session)
    provider = build_provider(config)
    reviewers = build_reviewers(config, provider, cwd, store)
    event = InterventionEvent(
        unit=[action], workspace=cwd, session=session, messages=messages
    )
    return review(event, reviewers, store, route_fn=build_route_fn(config))[0]


def _review_batch(
    view: TurnView, gated, my_id: str, action, session: str, cwd: str, config: Config
) -> Verdict:
    """Leader reviews the whole batch in one review() and publishes every verdict;
    followers read their own. Returns this hook's verdict for `my_id`; `action` is
    this hook's own normalized action, used for the degrade so it never needs a
    lookup that could KeyError into the silent fail-open path."""
    store = build_store(cwd)
    store.append_convo(session, view.messages)
    maybe_start_digest_worker(Path(cwd), session)
    maybe_start_feedback_worker(Path(cwd), session)
    provider = build_provider(config)
    reviewers = build_reviewers(config, provider, cwd, store)
    ids = [i for i, _ in gated]
    actions = [a for _, a in gated]
    event = InterventionEvent(
        unit=actions, workspace=cwd, session=session, messages=view.messages
    )
    try:
        verdicts = review(event, reviewers, store, route_fn=build_route_fn(config))
    except Exception:
        batch.write_verdicts(
            store.gadfly_dir, view.batch_id, {}
        )  # unblock followers → degrade NOW
        raise  # leader fails open + logs upstream
    # review() guarantees one verdict per action; defend that invariant at the gate
    # rather than zip-and-hope. On a (impossible) short return, by_id is empty: every
    # follower then sees the file present without its id and degrades to per-call,
    # and the leader degrades for its own action below — never a silent allow.
    by_id = dict(zip(ids, verdicts)) if len(verdicts) == len(actions) else {}
    batch.write_verdicts(store.gadfly_dir, view.batch_id, by_id)
    mine = by_id.get(my_id)
    if mine is not None:
        return mine
    # Degrade, never silent-allow — single attempt: the leader already spent its
    # review budget, so the retry loop here could blow the hook's fail-open ceiling.
    return _review_one(
        action, session, cwd, replace(config, llm_retries=1), view.messages
    )


def main() -> None:
    # Recursion guard: the reviewers shell out to `claude -p`, which would otherwise
    # re-enter this hook. That subprocess sets GADFLY_HOOK_DISABLED, so we defer.
    if os.environ.get("GADFLY_HOOK_DISABLED"):
        _emit(defer())
        return

    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        _emit(defer())
        return

    action = normalize(data.get("tool_name", ""), data.get("tool_input") or {})
    if action is None:
        _emit(defer())  # unknown / custom tool — let CC's own permission flow apply
        return

    # The one config-INDEPENDENT hard invariant: the builder never edits the memory files
    # directly. Enforced before config load so a broken gadfly.toml can't downgrade it.
    denied = managed_doc_verdict(action)
    if denied is not None:
        _emit(to_hook_output(denied))
        return

    cwd = data.get("cwd") or "."
    session = data.get("session_id", "unknown")
    my_id = data.get("tool_use_id", "")
    try:
        # Config load is inside the guard: a malformed/invalid gadfly.toml defers (D2)
        # rather than crashing the gate with an unhandled traceback.
        config = load(Path(cwd) / "gadfly.toml")
        route_fn = build_route_fn(config)

        # Deterministic, free: reads / routine commands terminal-allow — no poll, no LLM.
        r = route_fn(action)
        if r.terminal is not None:
            _emit(to_hook_output(r.terminal))
            return

        view = poll_turn(
            data.get("transcript_path"), my_id, timeout=config.poll_timeout
        )
        gated = _gated_calls(view, route_fn) if view.found else []
        if len(gated) > 1:
            # Parallel batch: one process leads (reviews all together, publishes
            # verdicts), the rest follow (read their own). A follower whose verdict
            # never arrives — leader crashed, or its id wasn't in the leader's batch
            # — gets None and degrades to reviewing its own action.
            gadfly_dir = build_store(cwd).gadfly_dir
            if batch.claim_leader(gadfly_dir, view.batch_id):
                _emit(
                    to_hook_output(
                        _review_batch(view, gated, my_id, action, session, cwd, config)
                    )
                )
                return
            # CC's command-hook timeout is 600s and fails OPEN on overrun, so the
            # whole follower path (wait + a possible degrade review) must finish well
            # inside it. A crashed leader publishes an empty map and we degrade at
            # once; this wait only bounds a leader that's slow-but-alive.
            wait = min(config.llm_timeout, 180)
            follower = batch.read_verdict(
                gadfly_dir, view.batch_id, my_id, timeout=wait
            )
            if follower is not None:
                _emit(to_hook_output(follower))
                return
            # Follower degrade: wait (≤180s) is already spent, so review with a
            # single attempt — worst case ≈ wait + llm_timeout, inside the hook
            # ceiling pinned in settings.json (which fails OPEN on overrun).
            _emit(
                to_hook_output(
                    _review_one(
                        action,
                        session,
                        cwd,
                        replace(config, llm_retries=1),
                        view.messages,
                    )
                )
            )
            return
        # Single action: full budget — nothing was spent waiting. Pass view.messages
        # unconditionally — poll_turn populates the conversation even on a batch-poll
        # miss, so the store stays current instead of freezing when the poll times out.
        _emit(
            to_hook_output(
                _review_one(
                    action, session, cwd, config, view.messages
                )
            )
        )
    except Exception:
        # Review errored (e.g. the model endpoint is down — rare). STEP ASIDE rather
        # than vouch: emit no permissionDecision so CC's native flow applies, with a
        # log line so nothing goes unlogged. Never an explicit allow, never a freeze.
        traceback.print_exc(file=sys.stderr)
        _emit(
            defer(
                "Gadfly: review errored — deferred to your normal permission flow (action unreviewed)."
            )
        )


if __name__ == "__main__":
    main()
