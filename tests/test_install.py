"""Claude Code installer — the conflict-safe, idempotent, marker-based settings merge."""
import json
from pathlib import Path

import pytest

from gadfly.adapters.claudecode import install as cc


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Point Path.home() at a temp dir so the global-settings scan never reads the real
    ~/.claude on the test machine (hermetic, and safe when HOME is unset)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)


def _local(tmp_path):
    return tmp_path / ".claude" / "settings.local.json"


def _read(p):
    return json.loads(p.read_text())


def _seed(tmp_path, data):
    p = _local(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))
    return p


def test_install_fresh_adds_all_events(tmp_path):
    res = cc.install("local", tmp_path)
    assert res.path == _local(tmp_path)
    assert set(res.added) == set(cc._HOOKS) and not res.updated
    hooks = _read(res.path)["hooks"]
    assert set(hooks) == set(cc._HOOKS)
    g = hooks["PreToolUse"][0]
    assert g["matcher"] == "Write|Edit|MultiEdit|Bash"
    assert "gadfly hook pretooluse" in g["hooks"][0]["command"]


def test_install_appends_beside_user_hook(tmp_path):
    p = _seed(tmp_path, {"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-linter"}]}]}})
    cc.install("local", tmp_path)
    arr = _read(p)["hooks"]["PreToolUse"]
    cmds = [h["command"] for g in arr for h in g["hooks"]]
    assert "my-linter" in cmds                                  # user's preserved
    assert any("gadfly hook pretooluse" in c for c in cmds)     # ours added
    assert len(arr) == 2


def test_install_idempotent(tmp_path):
    cc.install("local", tmp_path)
    res2 = cc.install("local", tmp_path)
    assert set(res2.updated) == set(cc._HOOKS) and not res2.added
    arr = _read(_local(tmp_path))["hooks"]["PreToolUse"]
    ours = [g for g in arr if cc._is_ours(g)]
    assert len(ours) == 1                                       # no duplicate group


def test_install_refuses_malformed_json(tmp_path):
    _seed(tmp_path, {})
    _local(tmp_path).write_text("{not json")
    with pytest.raises(SystemExit):
        cc.install("local", tmp_path)


def test_is_ours_recognizes_legacy_and_new(tmp_path):
    legacy = {"hooks": [{"command": "/x/.venv/bin/python -m gadfly.adapters.claudecode.hooks.pretooluse"}]}
    new = {"hooks": [{"command": "/x/.venv/bin/gadfly hook pretooluse"}]}
    other = {"hooks": [{"command": "prettier --write"}]}
    assert cc._is_ours(legacy) and cc._is_ours(new) and not cc._is_ours(other)


def test_uninstall_removes_only_ours(tmp_path):
    p = _seed(tmp_path, {"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-linter"}]},
        {"matcher": "Write", "hooks": [{"type": "command", "command": "gadfly hook pretooluse"}]}]}})
    res = cc.uninstall("local", tmp_path)
    assert res.removed == ["PreToolUse"]
    arr = _read(p)["hooks"]["PreToolUse"]
    assert len(arr) == 1 and arr[0]["hooks"][0]["command"] == "my-linter"


def test_uninstall_noop_leaves_file_untouched(tmp_path):
    p = _seed(tmp_path, {"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}]}})
    before = p.read_text()
    res = cc.uninstall("local", tmp_path)
    assert res.removed == [] and res.backup is None
    assert p.read_text() == before                             # not rewritten
    assert not p.with_name(p.name + ".bak").exists()           # no backup


def test_install_warns_on_cross_file_duplicate(tmp_path):
    proj = tmp_path / ".claude" / "settings.json"
    proj.parent.mkdir(parents=True)
    proj.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "gadfly hook pretooluse"}]}]}}))
    res = cc.install("local", tmp_path)
    assert any(str(proj) in w for w in res.warnings)


def test_disabled_sentinel(tmp_path):
    assert not cc.is_disabled(tmp_path)
    d = cc.disabled_path(tmp_path)
    d.parent.mkdir(parents=True)
    d.write_text("off")
    assert cc.is_disabled(tmp_path)


# --- workspace discovery: hooks must not trust the session's wandering cwd ---

def test_find_workspace_ascends_to_gadfly_toml(tmp_path):
    root = tmp_path / "proj"
    (root / "app" / "src").mkdir(parents=True)
    (root / "gadfly.toml").write_text("")
    assert cc.find_workspace(root / "app" / "src") == root.resolve()


def test_find_workspace_falls_back_to_cwd_without_toml(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    assert cc.find_workspace(d) == d.resolve()


def test_find_workspace_from_inside_state_dir(tmp_path):
    root = tmp_path / "proj"
    (root / ".gadfly").mkdir(parents=True)
    (root / "gadfly.toml").write_text("")
    assert cc.find_workspace(root / ".gadfly") == root.resolve()
