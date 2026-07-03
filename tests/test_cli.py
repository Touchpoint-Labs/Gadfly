"""The neutral CLI shell — zero-dep TOML editor, config validation, scaffolding, init."""
import tomllib
from pathlib import Path

import pytest

from gadfly import cli
from gadfly.config import Config


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Point Path.home() at a temp dir so global-scope init and the cross-file settings
    scan never read or write the real ~/.claude."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)


# --- value formatting --------------------------------------------------------

def test_fmt_toml_types():
    assert cli._fmt_toml("true") == "true"
    assert cli._fmt_toml("false") == "false"
    assert cli._fmt_toml("42") == "42"
    assert cli._fmt_toml("3.5") == "3.5"
    assert cli._fmt_toml("balanced") == '"balanced"'


# --- TOML line editor --------------------------------------------------------

def test_set_toplevel_overwrite(tmp_path):
    p = tmp_path / "gadfly.toml"
    p.write_text('autonomy = "balanced"\n\n[models]\narchitect = "opus"\n')
    cli._toml_set(p, "autonomy", "autonomous")
    d = tomllib.loads(p.read_text())
    assert d["autonomy"] == "autonomous" and d["models"]["architect"] == "opus"


def test_set_section_overwrite_keeps_inline_comment(tmp_path):
    p = tmp_path / "gadfly.toml"
    p.write_text('[models]\narchitect = "opus"  # the architect\n')
    cli._toml_set(p, "models.architect", "claude-fable-5")
    line = next(l for l in p.read_text().splitlines() if "architect" in l)
    assert 'architect = "claude-fable-5"' in line and "# the architect" in line


def test_set_new_key_in_existing_section(tmp_path):
    p = tmp_path / "gadfly.toml"
    p.write_text('[models]\narchitect = "opus"\n')
    cli._toml_set(p, "models.code", "sonnet")
    d = tomllib.loads(p.read_text())
    assert d["models"] == {"architect": "opus", "code": "sonnet"}


def test_set_toplevel_key_lands_before_first_table(tmp_path):
    # the reparenting trap: a new top-level key must NOT be inserted after [models]
    p = tmp_path / "gadfly.toml"
    p.write_text('autonomy = "balanced"\n[models]\narchitect = "opus"\n')
    cli._toml_set(p, "provider", "claude_cli")
    d = tomllib.loads(p.read_text())
    assert d["provider"] == "claude_cli"
    assert "provider" not in d["models"]        # not reparented into the table


def test_set_creates_missing_section(tmp_path):
    p = tmp_path / "gadfly.toml"
    p.write_text('autonomy = "balanced"\n')
    cli._toml_set(p, "memory.spec", "9000")
    d = tomllib.loads(p.read_text())
    assert d["memory"]["spec"] == 9000 and d["autonomy"] == "balanced"


def test_set_into_empty_file(tmp_path):
    p = tmp_path / "gadfly.toml"
    cli._toml_set(p, "models.architect", "claude-fable-5")
    assert tomllib.loads(p.read_text())["models"]["architect"] == "claude-fable-5"


# --- config command ----------------------------------------------------------

def test_flatten_config():
    flat = cli._flatten(Config())
    assert flat["autonomy"] == "balanced"
    assert flat["models.architect"] == "claude-opus-4-8"


def test_config_set_rejects_unknown_key(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["config", "achitect", "fable"])   # typo, missing models. prefix
    assert rc == 1
    assert not (tmp_path / "gadfly.toml").exists()   # nothing written


def test_config_set_and_get_roundtrip(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["config", "models.architect", "claude-fable-5"]) == 0
    capsys.readouterr()
    assert cli.main(["config", "models.architect"]) == 0
    assert capsys.readouterr().out.strip() == "claude-fable-5"


def test_config_set_reverts_invalid_value(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "gadfly.toml").write_text('autonomy = "balanced"\n')
    rc = cli.main(["config", "autonomy", "wobbly"])  # not in AUTONOMY → load() raises
    assert rc == 1
    assert 'autonomy = "balanced"' in (tmp_path / "gadfly.toml").read_text()  # reverted


# --- scaffolding -------------------------------------------------------------

# --- init: spec.md is mandatory locally, advised globally ---------------------

def test_init_local_requires_spec(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)                       # no spec.md here
    assert cli.main(["init"]) == 1
    assert "spec.md" in capsys.readouterr().err
    assert not (tmp_path / ".claude" / "settings.local.json").exists()  # nothing installed
    assert not (tmp_path / "gadfly.toml").exists()                      # nothing scaffolded


def test_init_local_succeeds_with_spec(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "spec.md").write_text("# Spec\nBuild a thing.\n")
    assert cli.main(["init"]) == 0
    assert (tmp_path / ".claude" / "settings.local.json").exists()
    assert (tmp_path / "gadfly.toml").exists()        # scaffolded


def test_init_global_skips_spec_check_and_does_not_scaffold(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)                       # no spec.md, but global doesn't require it
    assert cli.main(["init", "global"]) == 0
    assert "Global install" in capsys.readouterr().out
    assert (Path.home() / ".claude" / "settings.json").exists()  # installed to ~/.claude
    assert not (tmp_path / "gadfly.toml").exists()    # global doesn't scaffold the cwd


def test_scaffold_creates_config_and_gitignore_not_spec(tmp_path):
    created = cli._scaffold(tmp_path)
    assert (tmp_path / "gadfly.toml").exists()
    assert not (tmp_path / "spec.md").exists()        # deliberately absent (midwife's one shot)
    assert ".gadfly/" in (tmp_path / ".gitignore").read_text()
    assert "gadfly.toml" in created


def test_scaffold_idempotent_and_preserves_gitignore(tmp_path):
    (tmp_path / ".gitignore").write_text("node_modules/\n")
    cli._scaffold(tmp_path)
    gi = (tmp_path / ".gitignore").read_text()
    assert "node_modules/" in gi and ".gadfly/" in gi
    assert cli._scaffold(tmp_path) == []              # nothing new the second time
