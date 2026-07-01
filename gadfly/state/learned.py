"""Where the feedback extractor's typed memories land — auto-written, deduped.

- cross_project_style → a global, Gadfly-owned memory.md the architect reads in every
  project.
- project → the supervised project's claude.md (human-owned), under a managed
  "Learned by Gadfly" section so human- and Gadfly-authored rules stay legible.

Both are autonomous writes. Which files Gadfly may touch becomes config later (spec.md
stays human-gated — promoted only via an accepted decision); this module just performs
the write it's asked to.

After writing, each calls a compaction hook (if set) to shrink the file if it overflows
its budget. The hook fires once per write, best-effort; errors are swallowed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

_compactor: tuple[Callable[[str, int], str], Path, dict[str, int]] | None = None

GADFLY_SECTION = "## Learned by Gadfly"


def default_global_memory() -> Path:
    return Path.home() / ".gadfly" / "memory.md"


def set_compactor(
    condense: Callable[[str, int], str], gadfly_dir: Path, budgets: dict[str, int] | None = None
) -> None:
    """Register the compaction callable + .gadfly dir + per-file budgets. Called
    once per process from the Stop hook."""
    global _compactor
    _compactor = (condense, Path(gadfly_dir), budgets or {})


def _maybe_compact(path: Path, name: str, budget: int) -> None:
    if budget == 0 or _compactor is None:
        return
    condense, gdir, budgets = _compactor
    effective = budgets.get(name, budget)
    if effective == 0:
        return
    try:
        from .compaction import compact, check

        if check(path, effective):
            compact(path, effective, condense, gdir)
    except Exception:
        pass


def _append_deduped(path: Path, text: str, section: str | None = None) -> None:
    """Append a deduped bullet to a markdown file, creating it (and `section`,
    once) if absent."""
    line = f"- {' '.join(text.split())}"
    existing = path.read_text() if path.is_file() else ""
    if line in existing:
        return
    parts = []
    if section and section not in existing:
        parts.append(("\n" if existing.strip() else "") + section)
    parts.append(line)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((existing.rstrip() + "\n" + "\n".join(parts) + "\n").lstrip("\n"))


# --- cross-project memory (global, Gadfly-owned, architect-read) --------------


def record_cross_project(memory_path: Path, text: str) -> None:
    path = Path(memory_path)
    _append_deduped(path, text)
    _maybe_compact(path, "memory.md", 12000)


def read_cross_project(memory_path: Path) -> str:
    try:
        return Path(memory_path).read_text()
    except OSError:
        return ""


# --- project rules (auto-written into the project's claude.md) ----------------


def record_project_rule(claude_path: Path, text: str) -> None:
    """Append a learned project rule to claude.md, under a managed section so
    human- and Gadfly-authored rules stay distinguishable."""
    path = Path(claude_path)
    _append_deduped(path, text, section=GADFLY_SECTION)
    _maybe_compact(path, "claude.md", 15000)
