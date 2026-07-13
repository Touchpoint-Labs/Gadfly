#!/usr/bin/env python3
"""Claude Code SessionStart hook — human-edit capture and compaction check.

At session start:
1. Reconciles the edit-ledger against current file content. Diverged files — the
   human corrected the builder out-of-band — are captured into the corrections
   queue for idle-time memory extraction.
2. Compacts memory files over their character budgets. Human-owned (spec.md,
   claude.md) proposals are surfaced by injecting context that tells the builder
   to ask the user. AI-owned (memory.md, codemap.md) are auto-applied.

Surfacing: when proposals are pending, the hook outputs additionalContext. The
builder sees this in its next turn and asks the user with 3 options:
  - Accept — read .gadfly/compaction/<file>.proposed, overwrite the target file.
  - Dismiss — delete the .proposed file and .pending marker.
  - Disable compaction — edit gadfly.toml to increase the budget for that file.
All three are pure file I/O; the builder needs no special tools.

Read-only w.r.t. the build and fail-open: SessionStart can't block, errors swallowed.

Configure as a SessionStart hook:
    <venv>/bin/python -m gadfly.adapters.claudecode.hooks.sessionstart
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from gadfly.adapters.claudecode.install import find_workspace, is_disabled
from gadfly.config import load
from gadfly.factory import build_compactor, build_provider, memory_budgets_dict
from gadfly.state import compaction
from gadfly.worker import maybe_start_feedback_worker


def _run_compaction(workspace: Path, gadfly_dir: Path) -> list[str]:
    try:
        config = load(workspace / "gadfly.toml")
        condense = build_compactor(config, build_provider(config))
        return compaction.check_all(workspace, gadfly_dir, memory_budgets_dict(config), condense)
    except Exception:
        return []


def _surface(proposed: list[str], gadfly_dir: Path) -> dict | None:
    if not proposed:
        return None
    lines = [
        "Gadfly has pending compaction proposals. Ask the user for each file "
        "what to do, giving these options:",
        "1. Accept compaction — read .gadfly/compaction/<file>.proposed, "
        "overwrite the target file with it, then delete the .proposed file and "
        "the .pending marker.",
        "2. Dismiss — delete the .proposed file and .pending marker. (The "
        "proposal will re-appear at the next session start if still over budget.)",
        "3. Disable compaction — edit gadfly.toml to increase the budget for "
        "that file or add it to an exclude list.",
        "",
        "Pending proposals:",
    ]
    for name in proposed:
        prop = compaction.proposal(gadfly_dir, name)
        if prop is None:
            continue
        lines.append(
            f"- {name} ({len(prop)} chars proposed, "
            f"see .gadfly/compaction/{name}.proposed)"
        )
    return {"additionalContext": "\n".join(lines)}


def main() -> None:
    if os.environ.get("GADFLY_HOOK_DISABLED"):
        return
    try:
        data = json.load(sys.stdin)
        cwd = str(find_workspace(data.get("cwd")))
        if is_disabled(cwd):
            return  # disabled: fully off — capture pauses too; `enable` re-baselines the ledger
        session = data.get("session_id", "unknown")
        workspace = Path(cwd)
        gadfly_dir = workspace / ".gadfly"

        # Reconcile human out-of-band edits via the shared feedback worker (one
        # reconcile path, shared with the PreToolUse/Stop triggers) — nudged async.
        maybe_start_feedback_worker(workspace, session)

        proposed = _run_compaction(workspace, gadfly_dir)
        output = _surface(proposed, gadfly_dir)
        if output:
            print(json.dumps(output))
    except Exception:
        pass


if __name__ == "__main__":
    main()
