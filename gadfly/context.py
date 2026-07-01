"""Assembles a supervisor's context as (system, user): a stable system block the
provider can cache, and a dynamic user block. Order by volatility — the more stable
the context, the earlier it goes — so a caching provider (and the CLI's automatic
system-prompt caching) reuses the longest prefix. The often-changing codemap is kept
OUT of the cached prefix.

A reviewer judges a UNIT — one action, or all the gated actions of a parallel batch
together (one call, one verdict per change). The change/file sections render either
shape; the schema returns a verdict array the caller aligns by index.

The conversation a supervisor sees is the rolling digest of earlier turns plus the
verbatim recent tail; the architect also gets its recent rulings (its prior notes).
"""

from __future__ import annotations

from pathlib import Path

from .contracts import InterventionEvent, NormalizedAction
from .state import digest
from .state.decisions import Decision, DecisionLedger
from .state.memory import ProjectMemory
from .state.session import SessionStore

_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
_CONVO_TAIL_BUDGET = 24000


def load_prompt(name: str) -> str:
    return (_PROMPTS / name).read_text()


# --- change + surrounding-file rendering (single action or a batch) ----------


def _change_head(action: NormalizedAction) -> str:
    return f"{action.type.value} {action.target or ''}".strip()


def _change_body(action: NormalizedAction) -> str:
    p = action.payload
    if "old" in p or "new" in p:
        return f"--- before ---\n{p.get('old', '')}\n--- after ---\n{p.get('new', '')}"
    if "content" in p:
        return f"--- new file ---\n{p.get('content', '')}"
    if "command" in p:
        return f"$ {p['command']}"
    return ""


def _changes_section(actions: list[NormalizedAction]) -> str:
    if len(actions) == 1:
        a = actions[0]
        return f"# CHANGE\n{_change_head(a)}\n{_change_body(a)}".rstrip()
    lines = [
        f"# CHANGES ({len(actions)}) — return one verdict per change, in this order"
    ]
    for i, a in enumerate(actions, 1):
        lines.append(f"## change {i}: {_change_head(a)}\n{_change_body(a)}".rstrip())
    return "\n\n".join(lines)


def _files_section(mem: ProjectMemory, actions: list[NormalizedAction]) -> list[str]:
    out: list[str] = []
    for i, a in enumerate(actions, 1):
        surrounding = mem.file_around(a.target, anchor=a.payload.get("old"))
        if not surrounding.strip():
            continue
        label = (
            f"# CURRENT FILE ({a.target})"
            if len(actions) == 1
            else f"# CURRENT FILE — change {i} ({a.target})"
        )
        out.append(f"{label}\n{surrounding}")
    return out


# --- session tail (convo, and the architect's own prior rulings) -------------

_CONVO_LABEL = {
    ("user", "text"): "user",
    ("assistant", "text"): "assistant",
}


def _render_tail(records: list[dict]) -> str:
    """Render the session tail — convo entries, and (for the architect) prior gate
    rulings — chronologically, deduping repeated convo messages on read."""
    lines: list[str] = []
    seen: set = set()
    for r in records:
        if r.get("t") == "convo":
            key = (r.get("role"), r.get("kind"), r.get("text"))
            if key in seen:
                continue
            seen.add(key)
            tag = _CONVO_LABEL.get((r.get("role"), r.get("kind")), r.get("role", ""))
            lines.append(f"[{tag}] {r.get('text', '')}")
        elif r.get("t") == "gate":
            for a, v in zip(r.get("actions", []), r.get("verdicts", [])):
                if not (
                    v.get("note")
                    or v.get("undiscussed")
                    or v.get("decision") not in (None, "allow")
                ):
                    continue
                tgt = a.get("target") or a.get("payload", {}).get("command", "")
                piece = f"[you: {v.get('decision')}] {a.get('type')} {tgt}".rstrip()
                if v.get("note"):
                    piece += f" — {v['note']}"
                u = v.get("undiscussed")
                if u:
                    piece += f" (asked: {u.get('question')})"
                lines.append(piece)
    return "\n".join(lines)


def _convo_parts(store: SessionStore, session: str,
                 budget: int = _CONVO_TAIL_BUDGET) -> list[str]:
    """The conversation a supervisor reads: the rolling digest of earlier turns plus
    a bounded verbatim recent tail since it."""
    parts = []
    summary = digest.read(store.gadfly_dir, session)
    if summary.strip():
        parts.append("# SESSION SO FAR (summary of earlier turns)\n" + summary)
    recent = _render_tail(
        digest.tail(store, session, store.gadfly_dir, max_chars=budget)
    )
    if recent:
        parts.append("# RECENT MESSAGES\n" + recent)
    return parts


def _recent_rulings(store: SessionStore, session: str, budget: int = 4000) -> str:
    """The architect's recent informative gate rulings, newest-first under a budget;
    older ones drop, since the decisions they produced live in decisions.md."""
    out, used = [], 0
    for r in reversed([g for g in store.records(session) if g.get("t") == "gate"]):
        rendered = _render_tail([r])
        if not rendered:
            continue
        out.append(rendered)
        used += len(rendered)
        if used >= budget:
            break
    return "\n".join(reversed(out))


def code_context(
    mem: ProjectMemory,
    store: SessionStore,
    event: InterventionEvent,
    actions: list[NormalizedAction],
    solo: bool = False,
    convo_tail_budget: int = _CONVO_TAIL_BUDGET,
) -> tuple[str, str]:
    """Code reviewer: cached[role + claude] + dynamic[convo + change(s) + file(s)].
    solo loads code_solo.md — design also in scope when no architect runs."""
    system = load_prompt("code_solo.md" if solo else "code.md")
    if mem.claude.strip():
        system += "\n\n# PROJECT RULES (claude.md)\n" + mem.claude

    parts = []
    parts.extend(_convo_parts(store, event.session, convo_tail_budget))
    parts.append(_changes_section(actions))
    parts.extend(_files_section(mem, actions))
    verb = "this change" if len(actions) == 1 else "these changes"
    parts.append(f"Review {verb} and return your verdict(s).")
    return system, "\n\n".join(parts)


# --- architect ---------------------------------------------------------------


def _mode_block(mode: str) -> str:
    sections: dict[str, list[str]] = {}
    cur = None
    for line in load_prompt("architect_modes.md").splitlines():
        if line.startswith("## "):
            cur = line[3:].strip()
            sections[cur] = []
        elif cur is not None:
            sections[cur].append(line)
    body = "\n".join(sections.get(mode, [])).strip()
    if not body:
        raise ValueError(f"unknown architect mode: {mode!r}")
    return body


def architect_system(mode: str, solo: bool = False) -> str:
    base = "architect_solo.md" if solo else "architect.md"
    return load_prompt(base).replace("{{MODE}}", _mode_block(mode))


def _anchor_note(root: Path, ref: str) -> str:
    """Deterministic staleness check on one scope anchor ("file" or "file#symbol"),
    run only on the injected slice — a couple of bounded greps, no LLM. The
    annotation flags; the architect decides (revise / retire / delete / leave)."""
    f, _, symbol = ref.partition("#")
    p = root / f
    if not p.is_file():
        return f" [anchor missing: {f} not found]"
    if symbol:
        try:
            if symbol not in p.read_text(errors="ignore"):
                return f" [anchor missing: {symbol} no longer in {f}]"
        except OSError:
            return ""
    return ""


def _render_decisions(root: Path, decisions: list[Decision]) -> str:
    lines = []
    for d in decisions:
        lines.append(f"D{d.id}: {d.what}")
        if d.why:
            lines.append(f"  why: {d.why}")
        if d.scope:
            lines.append(
                "  scope: " + ", ".join(s + _anchor_note(root, s) for s in d.scope)
            )
    return "\n".join(lines)


def architect_context(
    mem: ProjectMemory,
    ledger: DecisionLedger,
    store: SessionStore,
    event: InterventionEvent,
    actions: list[NormalizedAction],
    mode: str,
    cross_project: str = "",
    solo: bool = False,
    convo_tail_budget: int = _CONVO_TAIL_BUDGET,
) -> tuple[str, str]:
    """Architect: cached[role+mode + spec + claude + cross-project calibration] +
    dynamic[codemap + decisions slice + recent convo & rulings + change(s) + file(s)].
    cross_project is the global memory.md — stable, so cached.
    solo loads architect_solo.md — critical code correctness also in scope."""
    system = architect_system(mode, solo)
    if mem.spec.strip():
        system += "\n\n# SPEC — the ideal you enforce against\n" + mem.spec
    if mem.claude.strip():
        system += "\n\n# PROJECT RULES (claude.md)\n" + mem.claude
    if cross_project.strip():
        system += "\n\n# PERSONAL CALIBRATION (cross-project style)\n" + cross_project

    parts = []
    if mem.codemap.strip():
        parts.append(
            "# CURRENT STRUCTURE (codemap.md — current state, may reflect drift)\n"
            + mem.codemap
        )
    files = [a.target for a in actions if a.target]
    decisions = ledger.slice(files=files)
    if decisions:
        parts.append(
            "# RELEVANT DECISIONS (decisions.md — yours to maintain via ops)\n"
            + _render_decisions(mem.root, decisions)
        )
    parts.extend(_convo_parts(store, event.session, convo_tail_budget))
    rulings = _recent_rulings(store, event.session)
    if rulings:
        parts.append("# YOUR RECENT RULINGS\n" + rulings)
    parts.append(_changes_section(actions))
    parts.extend(_files_section(mem, actions))
    verb = "this action" if len(actions) == 1 else "these actions"
    parts.append(f"Review {verb} and return your verdict(s).")
    return system, "\n\n".join(parts)
