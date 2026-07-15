"""Core orchestration.

`review()` routes each action, runs whatever reviewers the router selected, merges
their verdicts, and records the unit to the event log. Reviewers are injected
callables, so the core is testable without any LLM; the real ones (architect/code
over the LLM client + prompts, and the Haiku safety triage) plug in later.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, cast

from .contracts import (
    Decision,
    InterventionEvent,
    NormalizedAction,
    UndiscussedDecision,
    Verdict,
)
from .state.decisions import DecisionLedger
from .state.session import SessionStore
from .router import ARCHITECT, CODE, SAFETY, Route
from .router import route as default_route

# "disagreement wins" — strongest decision prevails when reviewers differ. ABSTAIN
# ("couldn't review") is lowest: a real verdict from the other reviewer always
# outranks it; only all-abstain merges to ABSTAIN, i.e. step aside (host decides).
_PRECEDENCE = {
    Decision.ABSTAIN: -1,
    Decision.ALLOW: 0,
    Decision.ALLOW_WITH_NOTE: 1,
    Decision.ASK: 2,
    Decision.DENY: 3,
}

# A reviewer judges a whole group of actions in ONE call and returns one verdict per
# action, index-aligned (it guarantees the count — see supervisors._aligned_review).
ReviewFn = Callable[[InterventionEvent, list[NormalizedAction]], list[Verdict]]
TriageFn = Callable[
    [InterventionEvent, NormalizedAction], bool
]  # dangerous? (per command)


@dataclass
class Reviewers:
    code: Optional[ReviewFn]  # None when disabled — the architect covers code, running solo
    architect: Optional[ReviewFn]  # None when disabled — the code reviewer reviews correctness only
    safety_triage: TriageFn


def merge(verdicts: list[Verdict]) -> Verdict:
    """Combine reviewers' verdicts for one action: strongest decision wins, notes
    are combined, undiscussed flags and ledger ops are preserved."""
    if not verdicts:
        return Verdict(
            decision=Decision.ABSTAIN
        )  # nothing reviewed it → step aside, never silent-allow
    winner = max(verdicts, key=lambda v: _PRECEDENCE[v.decision])
    notes = [v.note for v in verdicts if v.note]
    undiscussed = next((v.undiscussed for v in verdicts if v.undiscussed), None)
    ops = [o for v in verdicts for o in v.ops]
    return Verdict(
        decision=winner.decision,
        note=" | ".join(notes) or None,
        undiscussed=undiscussed,
        ops=ops,
    )


# True deadlock: the builder retrying the SAME action past this many denials stops
# being a review problem and becomes a human call (spec: surface both sides).
_DEADLOCK_CAP = 3
_DEADLOCK_Q = "Deadlock:"  # prefix marking a deadlock surface in the ledger, so a later
#                            gate can tell the action was already put to the human


def _action_key(type_str, target, payload) -> str:
    return json.dumps([type_str, target, payload], sort_keys=True, default=str)


def _deadlock_verdict(
    store: SessionStore, session: str, action: NormalizedAction
) -> Optional[Verdict]:
    """Break a recurring deny loop. Once an identical action has been denied past the cap,
    surface both sides to the human ONCE. After that, stop short-circuiting (return None) so
    the retry falls to normal review — where the supervisors read the user's actual answer in
    the conversation (allow a user-approved repeat with a note, hold the line otherwise).
    Without the one-time surface the ASK recurs forever (its verdict is an 'ask', never a
    'deny', so the cap never clears); a deterministic allow instead would ignore the answer
    and could let a 'hold' decision sail through."""
    key = _action_key(action.type.value, action.target, action.payload)
    denials: list[str] = []
    surfaced = False
    for r in store.records(session):
        if r.get("t") != "gate":
            continue
        for a, v in zip(r.get("actions", []), r.get("verdicts", [])):
            if _action_key(a.get("type"), a.get("target"), a.get("payload")) != key:
                continue
            if v.get("decision") == "deny":
                denials.append(v.get("note") or "")
            elif v.get("decision") == "ask" and (
                v.get("undiscussed") or {}
            ).get("question", "").startswith(_DEADLOCK_Q):
                surfaced = True
    if len(denials) < _DEADLOCK_CAP or surfaced:
        return None  # under the cap, or already surfaced once → let normal review proceed
    sides = " | ".join(dict.fromkeys(n for n in denials if n))  # dedupe, keep order
    return Verdict(
        decision=Decision.ASK,
        undiscussed=UndiscussedDecision(
            question=(
                f"{_DEADLOCK_Q} the builder has retried this same action after "
                f"{len(denials)} denials, and it stays paused until you decide. "
                f"Objections so far: {sides or 'none recorded'}. How should this proceed?"
            ),
            options=[
                "Let the action through as-is",
                "Hold the builder to the objections",
            ],
        ),
    )


def _review_unit(
    event: InterventionEvent,
    reviewers: Reviewers,
    route_fn,
    store: Optional[SessionStore] = None,
) -> list[Verdict]:
    """Review the whole unit. Each action is routed independently, then the actions
    bound for a given supervisor are reviewed TOGETHER in one call (plan B) — so a
    parallel batch is one code call + one architect call, not N of each. Per-action
    terminals/triage are resolved first; the rest are grouped, reviewed, and the
    aligned verdicts distributed back and merged by index."""
    actions = event.unit
    verdicts: list[Optional[Verdict]] = [None] * len(actions)
    code_idx: list[int] = []
    arch_idx: list[int] = []

    for i, action in enumerate(actions):
        r: Route = route_fn(action)
        if r.terminal is not None:
            verdicts[i] = r.terminal
            continue
        if store is not None:
            dl = _deadlock_verdict(store, event.session, action)
            if dl is not None:  # past the cap: surface to the human, review nothing
                verdicts[i] = dl
                continue
        if r.supervisors == frozenset({SAFETY}):
            if reviewers.safety_triage(
                event, action
            ):  # flagged → the surviving supervisor decides with context
                (arch_idx if reviewers.architect is not None else code_idx).append(i)
            else:
                verdicts[i] = Verdict(
                    decision=Decision.ALLOW
                )  # triage cleared it as routine
            continue
        if CODE in r.supervisors and reviewers.code is not None:
            code_idx.append(i)
        if ARCHITECT in r.supervisors and reviewers.architect is not None:
            arch_idx.append(i)

    code_v: dict[int, Verdict] = {}
    arch_v: dict[int, Verdict] = {}
    if (
        code_idx and arch_idx
    ):  # both supervisors: separate parallel calls, isolated contexts
        with ThreadPoolExecutor(max_workers=2) as pool:
            code_f = pool.submit(reviewers.code, event, [actions[i] for i in code_idx])
            arch_f = pool.submit(
                reviewers.architect, event, [actions[i] for i in arch_idx]
            )
            code_v = dict(zip(code_idx, code_f.result()))
            arch_v = dict(zip(arch_idx, arch_f.result()))
    elif code_idx:
        code_v = dict(
            zip(code_idx, reviewers.code(event, [actions[i] for i in code_idx]))
        )
    elif arch_idx:
        arch_v = dict(
            zip(arch_idx, reviewers.architect(event, [actions[i] for i in arch_idx]))
        )

    for i in range(len(actions)):
        if verdicts[i] is None:
            verdicts[i] = merge(
                [v for v in (code_v.get(i), arch_v.get(i)) if v is not None]
            )
    return cast(list[Verdict], verdicts)


def _apply_ops(event: InterventionEvent, verdicts: list[Verdict]) -> None:
    """The ledger writer — the architect only STATES ops (it is read-only); this
    applies them to decisions.md, promoting human-accepted adds to spec.md. The
    discipline of when to emit what (add/revise only when an allowing verdict
    settles something; housekeeping any time) is the architect's own, held by its
    prompt — the harness applies what the supervisor states without second-guessing
    it; a mis-logged entry is the architect's to delete at a later gate."""
    ops = [o for v in verdicts for o in v.ops]
    if not ops:
        return
    ledger = DecisionLedger(Path(event.workspace) / "decisions.md")
    ledger.apply(ops, spec_path=Path(event.workspace) / "spec.md")


def review(
    event: InterventionEvent,
    reviewers: Reviewers,
    store: SessionStore,
    route_fn=default_route,
) -> list[Verdict]:
    store.append_convo(event.session, event.messages)
    verdicts = _review_unit(event, reviewers, route_fn, store)
    _apply_ops(event, verdicts)
    store.append_gate(event, verdicts)
    return verdicts
