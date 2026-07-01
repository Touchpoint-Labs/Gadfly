#!/usr/bin/env python3
"""Claude Code PostToolUse hook — Gadfly's edit-ledger writer.

Fires after a Write/Edit/MultiEdit executes. Records the file plus a post-edit
content hash and snapshot to the edit-ledger — the authorship record and the basis
for diffing a later human correction. It NEVER affects the build: PostToolUse cannot
block (the tool already ran), and any error here is swallowed; a bookkeeping miss
must not disrupt the builder.

Configure as a PostToolUse hook (matcher Write|Edit|MultiEdit|NotebookEdit):
    <venv>/bin/python -m gadfly.adapters.claudecode.hooks.posttooluse
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from gadfly.adapters.claudecode.normalize import normalize
from gadfly.contracts import ActionType
from gadfly.state.edits import EditLedger


def _succeeded(tool_response) -> bool:
    """PostToolUse fires on failed edits too — and a failed edit leaves the file
    UNCHANGED, so recording it would snapshot pre-edit content and wrongly stamp it
    builder-authored. Skip on any explicit failure signal; record only when the tool
    response doesn't say it failed."""
    if isinstance(tool_response, dict):
        if tool_response.get("success") is False:
            return False
        if tool_response.get("error") or tool_response.get("is_error"):
            return False
    return True


def main() -> None:
    # The reviewers shell out to `claude -p` with this set; never ledger their work.
    if os.environ.get("GADFLY_HOOK_DISABLED"):
        return
    try:
        data = json.load(sys.stdin)
        if not _succeeded(data.get("tool_response")):
            return
        action = normalize(data.get("tool_name", ""), data.get("tool_input") or {})
        if action is None or action.type not in (ActionType.EDIT, ActionType.CREATE) or not action.target:
            return
        cwd = data.get("cwd") or "."
        session = data.get("session_id", "unknown")
        EditLedger(Path(cwd) / ".gadfly").record(session, data.get("tool_name", ""), action.target)
    except Exception:
        pass  # bookkeeping must never disrupt the build; PostToolUse can't block anyway


if __name__ == "__main__":
    main()
