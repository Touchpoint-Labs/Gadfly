"""Claude Code tool calls → NormalizedActions (pure mapping, no transcript/IO)."""
from gadfly.adapters.claudecode.normalize import normalize
from gadfly.contracts import ActionType


def test_read_search_fetch_meta_are_non_mutating():
    assert normalize("Read", {"file_path": "/a.py"}).type is ActionType.READ
    assert normalize("Grep", {"pattern": "x", "path": "/p"}).type is ActionType.SEARCH
    assert normalize("WebFetch", {"url": "http://x"}).type is ActionType.FETCH
    assert normalize("TodoWrite", {"todos": []}).type is ActionType.META


def test_bash_carries_command():
    a = normalize("Bash", {"command": "rm -rf build"})
    assert a.type is ActionType.EXEC and a.payload["command"] == "rm -rf build"


def test_edit_carries_old_and_new():
    a = normalize("Edit", {"file_path": "/u.py", "old_string": "a", "new_string": "b"})
    assert a.type is ActionType.EDIT and a.target == "/u.py"
    assert a.payload == {"old": "a", "new": "b"}


def test_multiedit_joins_hunks():
    a = normalize("MultiEdit", {"file_path": "/u.py",
                                "edits": [{"old_string": "a", "new_string": "b"},
                                          {"old_string": "c", "new_string": "d"}]})
    assert a.type is ActionType.EDIT and a.payload == {"old": "a\nc", "new": "b\nd"}


def test_write_new_path_is_create_existing_is_edit(tmp_path):
    new = tmp_path / "fresh.py"
    a = normalize("Write", {"file_path": str(new), "content": "x"})
    assert a.type is ActionType.CREATE and a.payload["content"] == "x"

    existing = tmp_path / "old.py"
    existing.write_text("old")
    b = normalize("Write", {"file_path": str(existing), "content": "x"})
    assert b.type is ActionType.EDIT


def test_unknown_tool_returns_none():
    assert normalize("SomeMcpTool", {"foo": 1}) is None


def test_raw_preserves_original_call():
    a = normalize("Edit", {"file_path": "/u.py", "old_string": "a", "new_string": "b"})
    assert a.raw == {"tool_name": "Edit", "tool_input": {"file_path": "/u.py", "old_string": "a", "new_string": "b"}}
