"""Factory wires config -> provider(s) + store + the three reviewers."""
from dataclasses import replace

import pytest

from gadfly.config import AnthropicAPI, Config, Providers
from gadfly.core import Reviewers
from gadfly.factory import build_provider, build_reviewers, build_store, provider_for
from gadfly.providers.anthropic_api import AnthropicAPIProvider
from gadfly.providers.llm import ClaudeCliProvider, LLMError
from gadfly.state.session import SessionStore


def test_build_provider_claude_cli():
    assert isinstance(build_provider(Config()), ClaudeCliProvider)


def test_build_provider_unknown_raises():
    with pytest.raises(ValueError):
        build_provider(replace(Config(), provider="nope"))


def test_anthropic_api_needs_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMError):
        build_provider(replace(Config(), provider="anthropic_api"))


def test_anthropic_api_built_when_key_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert isinstance(build_provider(replace(Config(), provider="anthropic_api")),
                      AnthropicAPIProvider)


def test_provider_for_per_supervisor(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    cfg = replace(Config(), provider="claude_cli",
                  providers=Providers(architect="anthropic_api"))
    assert isinstance(provider_for(cfg, "architect"), AnthropicAPIProvider)
    assert isinstance(provider_for(cfg, "code"), ClaudeCliProvider)  # falls back to default


def test_anthropic_api_custom_key_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("MY_KEY", "sk-test")
    cfg = replace(Config(), provider="anthropic_api",
                  anthropic_api=AnthropicAPI(api_key_env="MY_KEY"))
    assert isinstance(build_provider(cfg), AnthropicAPIProvider)


def test_build_store(tmp_path):
    assert isinstance(build_store(tmp_path), SessionStore)


def test_build_reviewers_wires_three(tmp_path):
    store = build_store(tmp_path)
    revs = build_reviewers(Config(), tmp_path, store)
    assert isinstance(revs, Reviewers)
    assert callable(revs.code) and callable(revs.architect) and callable(revs.safety_triage)
