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
MISSING_THRESHOLD = 3  # lower bar when codemap.md doesn't exist — the architect reviews blind


def pending(workspace: Path) -> tuple[int, bool]:
    """(builder code-edits since codemap.md was last updated, codemap exists)."""
    workspace = Path(workspace)
    codemap = ProjectMemory(workspace).path_for("codemap.md")
    try:
        since = codemap.stat().st_mtime
        exists = True
    except OSError:
        since = 0.0  # no codemap yet → every edit counts
        exists = False
    return EditLedger(workspace / ".gadfly").edits_since(since), exists


def nudge(workspace: Path) -> str | None:
    """A one-line reminder when codemap.md is missing or stale enough, else None."""
    n, exists = pending(workspace)
    if not exists:
        if n < MISSING_THRESHOLD:
            return None
        return ("codemap.md doesn't exist yet — write one so the architect reviews against "
                "the real structure (brief, descriptive: modules and their responsibilities).")
    if n < THRESHOLD:
        return None
    return (f"codemap.md is {n} code edits behind — refresh it to reflect the current "
            "structure (brief, descriptive: modules and their responsibilities, not exhaustive).")
