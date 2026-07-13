#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook — Gadfly's one-time midwife pass.

Fires on the first user prompt after spec.md exists and the midwife has never run
(.gadfly/midwife_done absent). Reads spec.md, interrogates it for unmade decisions,
vagueness, and underspecification, and injects the questions as additionalContext so
the builder asks the user and writes answers into spec.md.

Marker is written unconditionally once spec.md is found and the analysis runs — even
on an empty/whitespace response — so the LLM is never called again. If spec.md doesn't
exist yet the hook returns without writing the marker, allowing a later prompt to retry.

Never blocks: errors swallowed, exit 0. Recursion guard via GADFLY_HOOK_DISABLED.

Configure as a UserPromptSubmit hook:
    <venv>/bin/python -m gadfly.adapters.claudecode.hooks.userpromptsubmit
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from gadfly.adapters.claudecode.install import find_workspace, is_disabled
from gadfly.config import load
from gadfly.factory import build_midwife, build_provider
from gadfly.state.memory import ProjectMemory

_MARKER = "midwife_done"


def main() -> None:
    if os.environ.get("GADFLY_HOOK_DISABLED"):
        return
    try:
        data = json.load(sys.stdin)
        cwd = find_workspace(data.get("cwd"))
        if is_disabled(cwd):
            return
        gadfly_dir = cwd / ".gadfly"

        if (gadfly_dir / _MARKER).is_file():
            return  # already ran — fast path on every subsequent prompt

        spec = ProjectMemory(cwd).spec
        if not spec.strip():
            return  # spec doesn't exist yet; retry on a later prompt

        config = load(cwd / "gadfly.toml")
        questions = build_midwife(config, build_provider(config))(spec).strip()

        gadfly_dir.mkdir(parents=True, exist_ok=True)
        (gadfly_dir / _MARKER).write_text("")  # write unconditionally — never re-run

        if not questions:
            return  # spec was complete; nothing to surface

        context = (
            "Gadfly's architect has opening questions about the spec before building "
            "begins. Ask the user each one and write their answers into spec.md:\n\n"
            + questions
        )
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }))
    except Exception:
        pass


if __name__ == "__main__":
    main()
