"""Configuration — knobs with built-in defaults, overridable via gadfly.toml.

Read-only at runtime (tomllib). A missing or partial file just uses defaults, so
the file only needs the knobs you want to change. It lives at the workspace root
(committed), not in the gitignored .gadfly/ runtime dir.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Optional

PROVIDERS = ("claude_cli", "anthropic_api")  # subscription CLI | metered Messages API
AUTONOMY = ("autonomous", "balanced", "collaborative")  # class-3 surfacing dial
TEST_REVIEW = ("both", "code", "off")  # how test-file edits are reviewed


@dataclass(frozen=True)
class Models:
    architect: str = "claude-opus-4-8"
    code: str = "claude-sonnet-5"
    triage: str = "claude-haiku-4-5"
    feedback: str = "claude-sonnet-5"  # idle-time correction extractor


@dataclass(frozen=True)
class Providers:
    """Per-supervisor provider override. None → the global `provider`. Lets each
    reviewer run on a different backend (e.g. architect on the API, code on the CLI)."""

    architect: Optional[str] = None
    code: Optional[str] = None
    triage: Optional[str] = None


@dataclass(frozen=True)
class AnthropicAPI:
    api_key_env: str = "ANTHROPIC_API_KEY"  # env var holding the key (never the key itself)


@dataclass(frozen=True)
class MemoryBudgets:
    spec: int = 30000
    claude: int = 24000
    memory: int = 20000
    codemap: int = 24000


@dataclass(frozen=True)
class Config:
    provider: str = "claude_cli"
    autonomy: str = "balanced"
    models: Models = field(default_factory=Models)
    providers: Providers = field(default_factory=Providers)  # per-supervisor overrides
    anthropic_api: AnthropicAPI = field(default_factory=AnthropicAPI)
    memory: MemoryBudgets = field(default_factory=MemoryBudgets)
    llm_timeout: int = 240  # seconds per LLM call (measured architect reviews reach ~240s on big models)
    llm_retries: int = 2  # attempts on transient errors
    tool_budget: int = 5  # read/search tools the code reviewer / solo reviewers may use before they must produce a verdict (regular architect always runs tool-less); 0 = none
    poll_timeout: float = 3.0  # seconds to wait for the transcript to flush at the gate
    convo_tail_budget: int = 24000  # chars of recent conversation a supervisor sees
    disable_code_reviewer: bool = False  # architect alone, covering code via architect_solo.md
    disable_architect: bool = False  # code reviewer alone, covering design via code_solo.md
    auto_allow_docs: bool = True  # False → docs reviewed by the architect (never the code lens)
    test_review: str = "code"  # "both" | "code" | "off" — who reviews test-file edits


def _subset(data: dict, cls) -> dict:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


def load(path: Optional[Path] = None) -> Config:
    data: dict = {}
    if path is not None and Path(path).exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)
    models = replace(Models(), **_subset(data.get("models", {}) or {}, Models))
    providers = replace(
        Providers(), **_subset(data.get("providers", {}) or {}, Providers)
    )
    anthropic_api = replace(
        AnthropicAPI(), **_subset(data.get("anthropic_api", {}) or {}, AnthropicAPI)
    )
    memory = replace(
        MemoryBudgets(), **_subset(data.get("memory", {}) or {}, MemoryBudgets)
    )
    top = _subset(data, Config)
    for k in ("models", "providers", "anthropic_api", "memory"):
        top.pop(k, None)
    cfg = replace(
        Config(
            models=models,
            providers=providers,
            anthropic_api=anthropic_api,
            memory=memory,
        ),
        **top,
    )
    if cfg.provider not in PROVIDERS:
        raise ValueError(
            f"unknown provider {cfg.provider!r}; expected one of {PROVIDERS}"
        )
    for f in fields(Providers):
        name = getattr(cfg.providers, f.name)
        if name is not None and name not in PROVIDERS:
            raise ValueError(
                f"unknown provider {name!r} for providers.{f.name}; "
                f"expected one of {PROVIDERS}"
            )
    if cfg.autonomy not in AUTONOMY:
        raise ValueError(
            f"unknown autonomy {cfg.autonomy!r}; expected one of {AUTONOMY}"
        )
    if cfg.test_review not in TEST_REVIEW:
        raise ValueError(
            f"unknown test_review {cfg.test_review!r}; expected one of {TEST_REVIEW}"
        )
    if cfg.tool_budget < 0:
        raise ValueError(f"tool_budget must be >= 0, got {cfg.tool_budget}")
    if cfg.disable_code_reviewer and cfg.disable_architect:
        raise ValueError(
            "disable_code_reviewer and disable_architect are both set — that turns off "
            "all LLM review, leaving only Tier-0. Enable at least one supervisor."
        )
    return cfg
