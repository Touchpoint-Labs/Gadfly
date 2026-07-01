"""Factory wires config -> provider + store + the three reviewers."""
from dataclasses import replace

import pytest

from gadfly.config import Config
from gadfly.core import Reviewers
from gadfly.factory import build_provider, build_reviewers, build_store
from gadfly.providers.llm import ClaudeCliProvider
from gadfly.state.session import SessionStore


def test_build_provider_claude_cli():
    assert isinstance(build_provider(Config()), ClaudeCliProvider)


def test_build_provider_unimplemented_raises():
    with pytest.raises(ValueError):
        build_provider(replace(Config(), provider="anthropic_api"))


def test_build_store(tmp_path):
    assert isinstance(build_store(tmp_path), SessionStore)


def test_build_reviewers_wires_three(tmp_path):
    store = build_store(tmp_path)
    revs = build_reviewers(Config(), build_provider(Config()), tmp_path, store)
    assert isinstance(revs, Reviewers)
    assert callable(revs.code) and callable(revs.architect) and callable(revs.safety_triage)
