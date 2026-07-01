"""Idle-time feedback: reconcile human corrections, then extract durable rules.

`reconcile` captures files a human changed out-of-band (divergence from the builder's
last snapshot) into the corrections queue — cheap, no LLM, deduped; `has_pending_work`
is the cheap gate that says whether a pass would do anything. `run_extraction` then
pulls the queue, asks the extractor whether any correction generalizes into a durable
rule (usually none), and routes what comes back: project rules into the supervised
project's claude.md, cross-project style into the global memory.md, then marks processed
and clears the queue so the same edit is never reprocessed.

The extractor is read-only; this is the write side. A failed extractor call propagates
with the queue UNTOUCHED — the caller logs and leaves it for the next idle pass; mark/
clear only run once memories are in hand.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from .state import corrections as corr
from .state import learned
from .state.edits import EditLedger
from .state.memory import ProjectMemory


def run_extraction(extractor: Callable[[list[dict], str], list[dict]], *,
                   workspace: Path, gadfly_dir: Path,
                   global_memory: Optional[Path] = None) -> list[dict]:
    """Run one extraction pass over the pending corrections. Returns the typed
    memories written (for the caller to log); [] when nothing was pending or nothing
    generalized."""
    pending = corr.pending(gadfly_dir)
    if not pending:
        return []
    mem = ProjectMemory(workspace)
    claude_path = mem.path_for("claude.md")
    memories = extractor(pending, mem.claude)
    for m in memories:
        if m.get("type") == "project":
            learned.record_project_rule(claude_path, m["text"])
        else:
            learned.record_cross_project(global_memory or learned.default_global_memory(), m["text"])
    corr.mark_processed(gadfly_dir, pending)
    corr.clear(gadfly_dir)
    return memories


def _divergences(ledger: EditLedger):
    """Yield (file, before, after, reason) for each tracked file a human changed
    out-of-band — the single source for both reconcile and the pending-work gate.
    Per-file fault isolation: one unreadable/undecodable file is skipped, never fatal."""
    for d in ledger.diverged():
        try:
            f = d["file"]
            before = ledger.last_content(f)
            if before is None:
                continue
            after = Path(f).read_text() if d["reason"] == "modified" else None
        except Exception:
            continue
        yield f, before, after, d["reason"]


def reconcile(gadfly_dir: Path, session: str) -> None:
    """Capture human out-of-band edits into the corrections queue. Cheap, no LLM;
    capture dedups, and the ledger stays builder-authored (the human edit is never
    written back). Shared by every trigger so there is one reconcile path, not two."""
    ledger = EditLedger(gadfly_dir)
    for f, before, after, reason in _divergences(ledger):
        try:
            corr.capture(gadfly_dir, session, f, before, after, reason, ledger.last_hash(f))
        except Exception:
            continue  # one file's capture failing must not drop the rest of the batch


def has_pending_work(gadfly_dir: Path) -> bool:
    """Cheap hot-path gate for spawning the extractor: True if corrections are already
    queued, or a diverged file carries a correction not yet queued/processed. The dedup
    check matters because a corrected file stays diverged forever (the ledger never
    advances past a human edit) — a naive check would spawn a worker every tool call."""
    if corr.pending(gadfly_dir):
        return True
    ledger = EditLedger(gadfly_dir)
    for f, before, after, _ in _divergences(ledger):
        try:
            if corr.is_new_correction(gadfly_dir, f, ledger.last_hash(f), after):
                return True
        except Exception:
            continue
    return False
