"""The gate's retry clamp: attempts x timeout must fit the hook ceiling, because an
overrun is killed by Claude Code and fails OPEN (unreviewed, unlogged)."""

from dataclasses import replace

from gadfly.adapters.claudecode.hooks.pretooluse import _clamp_retries
from gadfly.adapters.claudecode.install import PRETOOLUSE_TIMEOUT
from gadfly.config import Config


def test_retries_under_the_ceiling_are_untouched():
    c = replace(Config(), llm_timeout=60, llm_retries=2)  # 2 x 60 fits easily
    assert _clamp_retries(c).llm_retries == 2


def test_retries_over_the_ceiling_are_clamped():
    c = replace(Config(), llm_timeout=240, llm_retries=5)
    out = _clamp_retries(c)
    assert out.llm_retries == (PRETOOLUSE_TIMEOUT - 40) // 240  # == 2
    assert out.llm_retries * 240 < PRETOOLUSE_TIMEOUT


def test_timeout_larger_than_the_ceiling_still_leaves_one_attempt():
    c = replace(Config(), llm_timeout=PRETOOLUSE_TIMEOUT * 2, llm_retries=3)
    assert _clamp_retries(c).llm_retries == 1  # never zero — the gate must still try


def test_float_timeout_keeps_retries_an_int():
    # a float llm_timeout in gadfly.toml is valid TOML; if the cap leaks that float into
    # llm_retries, complete_with_retry's range(attempts) raises TypeError and every gated
    # action defers "review errored" until the config is edited.
    c = replace(Config(), llm_timeout=240.5, llm_retries=3)
    out = _clamp_retries(c)
    assert isinstance(out.llm_retries, int)
    range(out.llm_retries)  # would raise TypeError on a float


def test_unusable_retries_are_normalized_even_when_under_the_cap():
    # a float/0/negative llm_retries set directly is under the cap, so it never trips the
    # clamp — but range(2.5) / an attempt-less loop breaks EVERY review the same way.
    for bad, want in ((2.5, 2), (0, 1), (-3, 1)):
        out = _clamp_retries(replace(Config(), llm_timeout=60, llm_retries=bad))
        assert out.llm_retries == want and isinstance(out.llm_retries, int)
        assert list(range(out.llm_retries))  # at least one attempt, never zero
