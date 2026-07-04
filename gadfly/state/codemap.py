"""Codemap staleness — a deterministic, no-LLM nudge to keep codemap.md current.

Counts builder code-edits (from the edit-ledger) recorded since codemap.md was last written;
once that passes a threshold the gate rides a one-line reminder on its next allow, asking the
builder to refresh codemap.md. Writing codemap.md moves its mtime forward, so every prior edit
falls behind it and the count resets itself — no counter to reset. `.md` edits don't count;
docs aren't structure.
"""
from __future__ import annotations

from pathlib import Path

from .edits import EditLedger
from .memory import ProjectMemory

THRESHOLD = 8  # code edits behind before the builder is nudged to refresh codemap.md


def pending(workspace: Path) -> int:
    """Builder code-edits recorded since codemap.md was last updated (0 when it's fresh)."""
    workspace = Path(workspace)
    codemap = ProjectMemory(workspace).path_for("codemap.md")
    try:
        since = codemap.stat().st_mtime
    except OSError:
        since = 0.0  # no codemap yet → every edit counts
    return EditLedger(workspace / ".gadfly").edits_since(since)


def nudge(workspace: Path) -> str | None:
    """A one-line reminder when codemap.md is at least THRESHOLD edits stale, else None."""
    n = pending(workspace)
    if n < THRESHOLD:
        return None
    return (f"codemap.md is {n} code edits behind — refresh it to reflect the current "
            "structure (brief, descriptive: modules and their responsibilities, not exhaustive).")
