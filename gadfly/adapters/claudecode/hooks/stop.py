#!/usr/bin/env python3
"""Claude Code Stop hook — Gadfly's idle-time feedback extraction and compaction.

When the builder finishes a turn:
1. Runs a feedback pass inline (reconcile human out-of-band edits into the corrections
   queue, then extract any that generalize into a durable rule in claude.md or
   memory.md) — the "learn before the next turn" backstop to the async PreToolUse/
   SessionStart nudges, sharing one per-session lock so they never collide.
2. Compacts any memory file over budget (auto-applies AI-owned, writes proposals for
   human-owned). Also registers the compactor in learned.py so per-write compaction
   fires during the next turn.

Best-effort: never blocks the builder. Errors swallowed, exit 0. The reviewers' own
`claude -p` subprocesses set GADFLY_HOOK_DISABLED so this never recurses.

Configure as a Stop hook:
    <venv>/bin/python -m gadfly.adapters.claudecode.hooks.stop
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from gadfly.config import load
from gadfly.factory import (
    build_compactor,
    build_extractor,
    build_provider,
    memory_budgets_dict,
)
from gadfly.state import compaction, learned
from gadfly.worker import feedback_pass


def main() -> None:
    if os.environ.get("GADFLY_HOOK_DISABLED"):
        return
    try:
        data = json.load(sys.stdin)
        cwd = Path(data.get("cwd") or ".")
        session = data.get("session_id", "unknown")
        config = load(cwd / "gadfly.toml")
        provider = build_provider(config)
        gadfly_dir = cwd / ".gadfly"
        budgets = memory_budgets_dict(config)
        compactor = build_compactor(config, provider)

        learned.set_compactor(compactor, gadfly_dir, budgets)

        extractor = build_extractor(config, provider)
        feedback_pass(cwd, session, extractor)

        compaction.check_all(cwd, gadfly_dir, budgets, compactor)
    except Exception:
        pass


if __name__ == "__main__":
    main()
