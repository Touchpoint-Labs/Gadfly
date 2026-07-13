"""Config loads defaults, applies partial overrides, and validates."""

import pytest

from gadfly.config import load


def test_defaults_when_no_path():
    c = load(None)
    assert c.provider == "claude_cli"
    assert c.autonomy == "balanced"
    assert c.models.architect == "claude-opus-4-8"
    assert c.llm_retries == 2
    assert c.memory.spec == 30000
    assert c.memory.claude == 24000


def test_missing_file_uses_defaults(tmp_path):
    assert load(tmp_path / "nope.toml").provider == "claude_cli"


def test_partial_override(tmp_path):
    p = tmp_path / "gadfly.toml"
    p.write_text(
        'provider = "anthropic_api"\n'
        "llm_timeout = 90\n\n"
        "[models]\n"
        'architect = "x-model"\n'
    )
    c = load(p)
    assert c.provider == "anthropic_api"
    assert c.llm_timeout == 90
    assert c.models.architect == "x-model"
    assert c.models.code == "claude-sonnet-5"  # untouched default


def test_per_supervisor_providers(tmp_path):
    p = tmp_path / "gadfly.toml"
    p.write_text(
        'provider = "claude_cli"\n\n'
        "[providers]\n"
        'architect = "anthropic_api"\n\n'
        "[anthropic_api]\n"
        'api_key_env = "MY_KEY"\n'
    )
    c = load(p)
    assert c.providers.architect == "anthropic_api"
    assert c.providers.code is None  # unset → falls back to the global provider
    assert c.anthropic_api.api_key_env == "MY_KEY"


def test_unknown_per_supervisor_provider_raises(tmp_path):
    p = tmp_path / "gadfly.toml"
    p.write_text("[providers]\ncode = \"gpt5\"\n")
    with pytest.raises(ValueError):
        load(p)


def test_unknown_provider_raises(tmp_path):
    p = tmp_path / "gadfly.toml"
    p.write_text('provider = "gpt5"\n')
    with pytest.raises(ValueError):
        load(p)


def test_unknown_autonomy_raises(tmp_path):
    p = tmp_path / "gadfly.toml"
    p.write_text('autonomy = "yolo"\n')
    with pytest.raises(ValueError):
        load(p)


def test_unknown_keys_are_ignored(tmp_path):
    p = tmp_path / "gadfly.toml"
    p.write_text('bogus_knob = 1\nprovider = "claude_cli"\n')
    assert load(p).provider == "claude_cli"


def test_new_knob_defaults():
    c = load(None)
    assert c.convo_tail_budget == 24000
    assert c.disable_code_reviewer is False and c.disable_architect is False
    assert c.auto_allow_docs is True and c.test_review == "code"


def test_unknown_test_review_raises(tmp_path):
    p = tmp_path / "gadfly.toml"
    p.write_text('test_review = "sometimes"\n')
    with pytest.raises(ValueError):
        load(p)


def test_both_supervisors_disabled_raises(tmp_path):
    p = tmp_path / "gadfly.toml"
    p.write_text("disable_code_reviewer = true\ndisable_architect = true\n")
    with pytest.raises(ValueError):
        load(p)
