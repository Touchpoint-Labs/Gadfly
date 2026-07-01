"""Claude Code adapter — normalize native tool calls into NormalizedActions.

The ONLY place that knows Claude Code's tool names and input shapes. Each native
tool maps to its neutral effect-class (the core never sees a tool name). Unknown
tools — custom or MCP — return None, so the hook passes them straight through to
Claude Code's own permission prompt rather than guessing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ...contracts import ActionType, NormalizedAction

_READ = {"Read", "NotebookRead"}
_SEARCH = {"Grep", "Glob"}
_FETCH = {"WebFetch", "WebSearch"}
_META = {"TodoWrite", "TodoRead", "Task", "ExitPlanMode"}


def normalize(tool_name: str, tool_input: dict[str, Any]) -> Optional[NormalizedAction]:
    """Map one Claude Code tool call to a NormalizedAction, or None if it's a tool
    we don't supervise (the hook then defers to Claude Code's own gating)."""
    raw = {"tool_name": tool_name, "tool_input": tool_input}

    if tool_name in _READ:
        return NormalizedAction(ActionType.READ, target=tool_input.get("file_path"), raw=raw)
    if tool_name in _SEARCH:
        return NormalizedAction(ActionType.SEARCH, target=tool_input.get("path"), raw=raw)
    if tool_name in _FETCH:
        return NormalizedAction(ActionType.FETCH, target=tool_input.get("url"), raw=raw)
    if tool_name in _META:
        return NormalizedAction(ActionType.META, raw=raw)

    if tool_name == "Bash":
        return NormalizedAction(ActionType.EXEC, payload={"command": tool_input.get("command", "")}, raw=raw)

    if tool_name == "Edit":
        return NormalizedAction(
            ActionType.EDIT, target=tool_input.get("file_path"),
            payload={"old": tool_input.get("old_string", ""), "new": tool_input.get("new_string", "")},
            raw=raw,
        )
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits") or []
        return NormalizedAction(
            ActionType.EDIT, target=tool_input.get("file_path"),
            payload={"old": "\n".join(e.get("old_string", "") for e in edits),
                     "new": "\n".join(e.get("new_string", "") for e in edits)},
            raw=raw,
        )
    if tool_name == "NotebookEdit":
        return NormalizedAction(
            ActionType.EDIT, target=tool_input.get("notebook_path"),
            payload={"new": tool_input.get("new_source", "")}, raw=raw,
        )
    if tool_name == "Write":
        target = tool_input.get("file_path")
        # Write to an existing path is an edit; to a new path, a creation.
        action_type = ActionType.EDIT if (target and Path(target).exists()) else ActionType.CREATE
        return NormalizedAction(action_type, target=target,
                                payload={"content": tool_input.get("content", "")}, raw=raw)

    return None  # unknown / custom / MCP tool — not ours to gate
