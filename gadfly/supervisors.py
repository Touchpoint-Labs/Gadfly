"""Turns a prompt + the LLM client into a reviewer callable
`(InterventionEvent, list[NormalizedAction]) -> list[Verdict]`: assemble context
(sliced from the session store) for the whole unit, ask the provider for a verdict
ARRAY (one per change, via --json-schema), parse it.

Alignment is a hard contract, because a misaligned verdict is worse than a crash —
an allow landing on the change that should be denied. A reviewer therefore ALWAYS
returns exactly len(actions) verdicts, index-aligned: it trusts a count-matched
batch, but on ANY count mismatch it degrades to reviewing each action in its own
call (each inherently aligned), and a lone action it still can't parse ABSTAINS —
the adapter steps aside to the host's own permission flow rather than vouch with a
silent allow. It never zips a mismatched count onto actions.

Reviewers are read-only (mutating tools disallowed at the CLI). Logging a decision
to the ledger is the harness's job from the verdict. The safety triage is the
exception — a cheap yes/no command filter returning a bool (needs architect review?).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .context import _CONVO_TAIL_BUDGET, architect_context, code_context, load_prompt
from .contracts import (
    Decision, DecisionOp, InterventionEvent, NormalizedAction, ScopeRef,
    UndiscussedDecision, Verdict,
)
from .providers.llm import LLMProvider, complete_with_retry
from .state import learned
from .state.decisions import DecisionLedger
from .state.memory import ProjectMemory
from .state.session import SessionStore

_VERDICT_ITEM = {
    "decision": {"type": "string", "enum": ["allow", "allow_with_note", "deny"]},
    "note": {"type": ["string", "null"]},
}


def _array_schema(item_props: dict) -> dict:
    return {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {"type": "object", "properties": item_props,
                          "required": ["decision"], "additionalProperties": False},
            }
        },
        "required": ["verdicts"],
        "additionalProperties": False,
    }


# Code reviewer never surfaces (no ask) and never touches the decisions ledger.
CODE_VERDICT_SCHEMA = _array_schema(dict(_VERDICT_ITEM))

# Architect can also ask the user, flag an undiscussed decision (+options), and
# maintain the decisions ledger via ops — it states, the harness writes.
ARCHITECT_VERDICT_SCHEMA = _array_schema({
    "decision": {"type": "string", "enum": ["allow", "allow_with_note", "deny", "ask"]},
    "note": {"type": ["string", "null"]},
    "undiscussed": {
        "type": ["object", "null"],
        "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["question"],
        "additionalProperties": False,
    },
    "ops": {
        "type": ["array", "null"],
        "items": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["add", "revise", "retire", "delete"]},
                "id": {"type": ["string", "integer", "null"]},   # "D7" or 7
                "what": {"type": ["string", "null"]},
                "why": {"type": ["string", "null"]},
                "scope": {
                    "type": ["array", "null"],
                    "items": {
                        "type": "object",
                        "properties": {"file": {"type": "string"},
                                       "symbol": {"type": ["string", "null"]}},
                        "required": ["file"],
                        "additionalProperties": False,
                    },
                },
                "supersedes": {"type": ["array", "null"],
                               "items": {"type": ["string", "integer"]}},
                "human_accepted": {"type": ["boolean", "null"]},
                "reason": {"type": ["string", "null"]},
            },
            "required": ["op"],
            "additionalProperties": False,
        },
    },
})


def _decision_id(x) -> int:
    return int(str(x).strip().lstrip("Dd") or 0)


def _op_from_obj(o: dict) -> DecisionOp:
    return DecisionOp(
        op=o["op"],
        id=_decision_id(o.get("id") or 0),
        what=o.get("what") or "",
        why=o.get("why") or "",
        scope=[ScopeRef(file=s["file"], symbol=s.get("symbol") or "")
               for s in o.get("scope") or []],
        supersedes=[_decision_id(x) for x in o.get("supersedes") or []],
        human_accepted=bool(o.get("human_accepted")),
        reason=o.get("reason") or "",
    )


def _verdict_from_obj(d: dict) -> Verdict:
    u = d.get("undiscussed")
    undiscussed = (
        UndiscussedDecision(question=u["question"], options=list(u.get("options", [])))
        if u else None
    )
    return Verdict(decision=Decision(d["decision"]), note=d.get("note"),
                   undiscussed=undiscussed,
                   ops=[_op_from_obj(o) for o in d.get("ops") or []])


def parse_verdicts(raw: str) -> list[Verdict]:
    """Parse the verdict array. Returns exactly as many as the model produced — the
    caller checks the count against the actions and degrades on mismatch, so this
    never pads or truncates to force a fit. Unparseable output → [] (→ degrade)."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    items = data.get("verdicts", []) if isinstance(data, dict) else []
    out = []
    for it in items:
        try:
            out.append(_verdict_from_obj(it))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue  # drop a malformed item — count mismatch then forces the degrade
    return out


def _aligned_review(actions: list[NormalizedAction],
                    call_once: Callable[[list[NormalizedAction]], list[Verdict]]) -> list[Verdict]:
    """Guarantee exactly len(actions) verdicts, index-aligned. Trust a count-matched
    batch; on mismatch re-review each action alone (each call inherently aligned); a
    lone action still unparseable ABSTAINS (the adapter steps aside — host decides —
    rather than vouch with a silent allow). Never zip a mismatched count."""
    if not actions:
        return []
    verdicts = call_once(actions)
    if len(verdicts) == len(actions):
        return verdicts
    if len(actions) == 1:
        return verdicts if len(verdicts) == 1 else [Verdict(decision=Decision.ABSTAIN)]
    out: list[Verdict] = []
    for a in actions:
        v = call_once([a])
        out.append(v[0] if len(v) == 1 else Verdict(decision=Decision.ABSTAIN))
    return out


def make_code_reviewer(provider: LLMProvider, model: str, workspace, store: SessionStore,
                       attempts: int = 3, solo: bool = False,
                       convo_tail_budget: int = _CONVO_TAIL_BUDGET):
    mem = ProjectMemory(workspace)

    def review(event: InterventionEvent, actions: list[NormalizedAction]) -> list[Verdict]:
        def call_once(subset):
            system, user = code_context(mem, store, event, subset, solo, convo_tail_budget)
            raw = complete_with_retry(provider, system=system, prompt=user, model=model,
                                      schema=CODE_VERDICT_SCHEMA, attempts=attempts)
            return parse_verdicts(raw)
        return _aligned_review(actions, call_once)

    return review


def make_architect(provider: LLMProvider, model: str, workspace, store: SessionStore,
                   mode: str, attempts: int = 3, cross_project_path=None,
                   solo: bool = False, convo_tail_budget: int = _CONVO_TAIL_BUDGET):
    mem = ProjectMemory(workspace)
    ledger = DecisionLedger(Path(workspace) / "decisions.md")
    cross_path = cross_project_path or learned.default_global_memory()

    def review(event: InterventionEvent, actions: list[NormalizedAction]) -> list[Verdict]:
        def call_once(subset):
            cross = learned.read_cross_project(cross_path)
            system, user = architect_context(mem, ledger, store, event, subset, mode, cross,
                                             solo=solo, convo_tail_budget=convo_tail_budget)
            # Normal architect gets no tools — it reasons at altitude from the spec/codemap/
            # decisions/change it's given (verified: it never reaches for them). The solo
            # architect covers the code lane, so it keeps reads for the rare verification.
            raw = complete_with_retry(provider, system=system, prompt=user, model=model,
                                      schema=ARCHITECT_VERDICT_SCHEMA, attempts=attempts,
                                      tools=solo)
            return parse_verdicts(raw)
        return _aligned_review(actions, call_once)

    return review


def make_safety_triage(provider: LLMProvider, model: str, store: SessionStore, attempts: int = 3):
    """Cheap command filter: True if the command warrants architect review. Plain
    text (REVIEW/ALLOW), no schema — fast; defaults to review when unclear."""
    system = load_prompt("triage.md")

    def triage(event: InterventionEvent, action: NormalizedAction) -> bool:
        prompt = f"Command:\n$ {action.payload.get('command', '')}"
        convo = store.tail(event.session)
        if convo:
            prompt += "\n\nRecent conversation:\n" + "\n".join(r.get("text", "") for r in convo)
        prompt += "\n\nREVIEW or ALLOW?"
        # Cheap REVIEW/ALLOW classifier — no tools (it decides from the command + convo).
        raw = complete_with_retry(provider, system=system, prompt=prompt, model=model,
                                  attempts=attempts, tools=False)
        u = raw.upper()
        return ("REVIEW" in u) or ("ALLOW" not in u)

    return triage


def make_summarizer(provider: LLMProvider, model: str, attempts: int = 2):
    """The digest compactor: summarize(prev_digest, overflow) -> updated digest."""
    system = load_prompt("digest.md")

    def summarize(prev_digest: str, overflow: str) -> str:
        current = prev_digest.strip() or "(none yet)"
        prompt = f"# CURRENT DIGEST\n{current}\n\n# NEW TRANSCRIPT\n{overflow}"
        return complete_with_retry(provider, system=system, prompt=prompt, model=model,
                                   attempts=attempts, tools=False)

    return summarize


def make_memory_compactor(provider: LLMProvider, model: str, attempts: int = 2):
    """Memory-file compactor: condense(content, budget_chars) -> condensed text."""
    system = load_prompt("compaction.md")

    def condense(content: str, budget: int) -> str:
        prompt = (
            f"CHARACTER LIMIT: {budget} (current file: {len(content)} characters)\n\n"
            f"# FILE\n{content}"
        )
        return complete_with_retry(provider, system=system, prompt=prompt, model=model,
                                   attempts=attempts, tools=False)

    return condense


_MEMORY_SCHEMA = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string",
                             "enum": ["project", "cross_project_style"]},
                    "text": {"type": "string"},
                },
                "required": ["type", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["memories"],
    "additionalProperties": False,
}


def make_midwife(provider: LLMProvider, model: str, attempts: int = 2):
    """One-time spec interrogator: spec text -> gap questions (plain text, often empty)."""
    system = load_prompt("midwife.md")

    def analyze(spec: str) -> str:
        return complete_with_retry(provider, system=system,
                                   prompt=f"# SPEC\n{spec}", model=model, attempts=attempts,
                                   tools=False)

    return analyze


def make_extractor(provider: LLMProvider, model: str, attempts: int = 2):
    """The feedback extractor: human corrections -> typed memory proposals (often
    none). Read-only — it proposes; the harness routes and writes."""
    system = load_prompt("extractor.md")

    def extract(corrections: list[dict], project_rules: str = "") -> list[dict]:
        if not corrections:
            return []
        diffs = "\n\n".join(f"## {c.get('file', '')} ({c.get('reason', '')})\n{c.get('diff', '')}"
                            for c in corrections)
        prompt = f"# EXISTING RULES\n{project_rules.strip() or '(none)'}\n\n# DIFFS\n{diffs}"
        raw = complete_with_retry(provider, system=system, prompt=prompt, model=model,
                                  schema=_MEMORY_SCHEMA, attempts=attempts, tools=False)
        try:
            items = (json.loads(raw) or {}).get("memories", [])
        except (json.JSONDecodeError, TypeError, AttributeError):
            return []
        return [{"type": m["type"], "text": m["text"]} for m in items
                if isinstance(m, dict) and m.get("type") and m.get("text")]

    return extract
