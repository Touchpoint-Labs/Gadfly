"""Builds runtime pieces from config: the LLM provider, the session store, and the
Reviewers bundle that core.review() consumes. The store is shared — the reviewers
read their context slice from it and core.review() writes the session to it."""

from __future__ import annotations

from pathlib import Path

from .config import Config
from .core import Reviewers
from .providers.llm import ClaudeCliProvider, LLMProvider
from .router import route
from .state.session import SessionStore
from .supervisors import (
    make_architect,
    make_code_reviewer,
    make_extractor,
    make_memory_compactor,
    make_midwife,
    make_safety_triage,
)


def build_provider(config: Config) -> LLMProvider:
    if config.provider == "claude_cli":
        return ClaudeCliProvider(timeout=config.llm_timeout)
    raise ValueError(f"provider {config.provider!r} is not implemented in v1")


def build_store(workspace: Path) -> SessionStore:
    return SessionStore(Path(workspace) / ".gadfly")


def build_reviewers(
    config: Config, provider: LLMProvider, workspace: Path, store: SessionStore
) -> Reviewers:
    # A disabled reviewer is None; the survivor runs its solo prompt to cover the gap.
    code = None if config.disable_code_reviewer else make_code_reviewer(
        provider, config.models.code, workspace, store,
        attempts=config.llm_retries, solo=config.disable_architect,
        convo_tail_budget=config.convo_tail_budget,
    )
    architect = None if config.disable_architect else make_architect(
        provider, config.models.architect, workspace, store, config.autonomy,
        attempts=config.llm_retries, solo=config.disable_code_reviewer,
        convo_tail_budget=config.convo_tail_budget,
    )
    return Reviewers(
        code=code,
        architect=architect,
        safety_triage=make_safety_triage(
            provider, config.models.triage, store, attempts=config.llm_retries
        ),
    )


def build_route_fn(config: Config):
    """A config-aware route() for core.review and the adapter's terminal check —
    binds the doc/test knobs and which supervisors are enabled."""
    return lambda action: route(
        action,
        auto_allow_docs=config.auto_allow_docs,
        test_review=config.test_review,
        code_enabled=not config.disable_code_reviewer,
        architect_enabled=not config.disable_architect,
    )


def build_extractor(config: Config, provider: LLMProvider):
    """The idle-time feedback extractor: reconciles human corrections into durable
    rules, off the hot review path, behind a high-bar prompt. Its own feedback model."""
    return make_extractor(provider, config.models.feedback, attempts=config.llm_retries)


def build_midwife(config: Config, provider: LLMProvider):
    """One-time spec interrogator. Uses the architect model — architectural analysis."""
    return make_midwife(provider, config.models.architect, attempts=config.llm_retries)


def build_compactor(config: Config, provider: LLMProvider):
    """Memory-file compactor — condenses spec/claude/codemap/memory when over budget."""
    return make_memory_compactor(
        provider, config.models.code, attempts=config.llm_retries
    )


def memory_budgets_dict(config: Config) -> dict[str, int]:
    m = config.memory
    return {
        "spec": m.spec,
        "claude": m.claude,
        "memory": m.memory,
        "codemap": m.codemap,
    }
