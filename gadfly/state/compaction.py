"""Memory-file caps and overflow compaction.

Each supervised-project memory file has a character budget. When a file exceeds it,
an LLM pass condenses it. The model targets 75% of the budget for headroom; the
harness enforces the full budget after each pass (retry, then mechanical truncation).

Two policies:
- Human-owned (spec.md, claude.md): writes a proposed condensed version to
  .gadfly/compaction/<file>.proposed + a .pending marker. The adapter surfaces
  this as a signal; the builder asks the user with 3 options:
    1. Accept — overwrite the target with the proposal, delete .proposed + marker.
    2. Dismiss — delete .proposed + marker. (Re-proposed next time if still over.)
    3. Disable compaction — edit gadfly.toml to increase the budget.
  All three are pure file I/O — no Python function calls needed.
- AI-owned (memory.md, codemap.md): auto-applies the condensed version.

The compactor is pure and stateless — it condenses on demand; callers decide when.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

CondenseFn = Callable[[str, int], str]

DEFAULT_SPEC_BUDGET = 18000
DEFAULT_CLAUDE_BUDGET = 15000
DEFAULT_MEMORY_BUDGET = 12000
DEFAULT_CODEMAP_BUDGET = 15000

HUMAN_OWNED = {"spec.md", "claude.md"}

TARGET_RATIO = 0.75


def _dir(gadfly_dir: Path) -> Path:
    d = Path(gadfly_dir) / "compaction"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _proposal_path(gadfly_dir: Path, name: str) -> Path:
    return _dir(gadfly_dir) / f"{name}.proposed"


def _pending_path(gadfly_dir: Path) -> Path:
    return _dir(gadfly_dir) / ".pending"


def check(path: Path, budget: int) -> bool:
    if budget == 0:
        return False
    try:
        return len(path.read_text()) > budget
    except OSError:
        return False


def _truncate(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    cut = text[:budget]
    for sep in ("\n\n", "\n"):
        at = cut.rfind(sep)
        if at > budget * 0.6:
            return cut[:at].rstrip() + "\n"
    return cut.rstrip()


def enforce_budget(
    text: str, budget: int, condense: CondenseFn, *, retries: int = 1
) -> str:
    """Guarantee len(result) <= budget. Model targets TARGET_RATIO of budget."""
    if len(text) <= budget:
        return text
    current = text
    for attempt in range(retries + 1):
        target = (
            int(budget * TARGET_RATIO)
            if attempt == 0
            else max(budget - 500, int(budget * 0.90))
        )
        current = condense(current, target)
        if len(current) <= budget:
            return current
    return _truncate(current, budget)


def compact(path: Path, budget: int, condense: CondenseFn, gadfly_dir: Path) -> bool:
    """Condense a file that exceeds its budget. Returns True if applied in place
    (AI-owned), False if a proposal was written (human-owned)."""
    try:
        content = path.read_text()
    except OSError:
        return False
    if len(content) <= budget:
        return False

    condensed = enforce_budget(content, budget, condense)
    name = path.name.lower()

    if name in HUMAN_OWNED:
        _proposal_path(gadfly_dir, name).write_text(condensed)
        p = _pending_path(gadfly_dir)
        pending = set(p.read_text().strip().splitlines()) if p.is_file() else set()
        pending.add(name)
        p.write_text("\n".join(sorted(pending)) + "\n")
        return False

    path.write_text(condensed)
    return True


def pending_proposals(gadfly_dir: Path) -> list[str]:
    p = _pending_path(gadfly_dir)
    if not p.is_file():
        return []
    return [
        n
        for n in p.read_text().strip().splitlines()
        if _proposal_path(gadfly_dir, n).is_file()
    ]


def proposal(gadfly_dir: Path, name: str) -> str | None:
    p = _proposal_path(gadfly_dir, name)
    try:
        return p.read_text()
    except OSError:
        return None


def accept(gadfly_dir: Path, name: str, target: Path) -> bool:
    """Apply a pending proposal and clean up. Called by the builder. Returns
    True if the proposal existed and was applied."""
    text = proposal(gadfly_dir, name)
    if text is None:
        return False
    target.write_text(text)
    _proposal_path(gadfly_dir, name).unlink(missing_ok=True)
    _drop_pending(gadfly_dir, name)
    return True


def dismiss(gadfly_dir: Path, name: str) -> None:
    """Drop a pending proposal. Called by the builder. Re-proposed next time if
    the file is still over budget."""
    _proposal_path(gadfly_dir, name).unlink(missing_ok=True)
    _drop_pending(gadfly_dir, name)


def _drop_pending(gadfly_dir: Path, name: str) -> None:
    p = _pending_path(gadfly_dir)
    if not p.is_file():
        return
    pending = [n for n in p.read_text().strip().splitlines() if n != name]
    if pending:
        p.write_text("\n".join(pending) + "\n")
    else:
        p.unlink(missing_ok=True)


def check_all(
    workspace: Path, gadfly_dir: Path, budgets: dict[str, int], condense: CondenseFn
) -> list[str]:
    """Compact any memory file over budget. Returns human-owned files proposed."""
    from .learned import default_global_memory
    from .memory import ProjectMemory

    mem = ProjectMemory(workspace)
    proposed: list[str] = []
    targets = [
        (mem.path_for("spec.md"), "spec.md", budgets.get("spec", DEFAULT_SPEC_BUDGET)),
        (
            mem.path_for("claude.md"),
            "claude.md",
            budgets.get("claude", DEFAULT_CLAUDE_BUDGET),
        ),
        (
            mem.path_for("codemap.md"),
            "codemap.md",
            budgets.get("codemap", DEFAULT_CODEMAP_BUDGET),
        ),
        (
            default_global_memory(),
            "memory.md",
            budgets.get("memory", DEFAULT_MEMORY_BUDGET),
        ),
    ]
    for path, name, budget in targets:
        if budget == 0:
            continue
        if not path.is_file() or not check(path, budget):
            continue
        if name in HUMAN_OWNED and proposal(gadfly_dir, name) is not None:
            proposed.append(name)  # pending proposal already covers it; no new LLM call
            continue
        if not compact(path, budget, condense, gadfly_dir) and name in HUMAN_OWNED:
            proposed.append(name)
        # At most one condense per pass: each is an LLM call, and the calling
        # hook has a wall-clock ceiling. The next pass takes the next file.
        break
    return proposed
