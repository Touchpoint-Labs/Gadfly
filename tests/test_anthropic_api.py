"""AnthropicAPIProvider: request shape + SSE stream parsing + error handling, urlopen
mocked (no network). The provider streams, so the mock yields `data:` event lines."""
import io
import json
import urllib.error

import pytest

from gadfly.providers.anthropic_api import AnthropicAPIProvider
from gadfly.providers.llm import LLMError, LLMTransientError


class _Stream:
    """A fake urlopen response: iterating yields SSE byte-lines for the given events."""

    def __init__(self, events):
        self._chunks = []
        for ev in events:
            self._chunks.append(f"event: {ev.get('type', '')}\n".encode())
            self._chunks.append(f"data: {json.dumps(ev)}\n".encode())
            self._chunks.append(b"\n")

    def __iter__(self):
        return iter(self._chunks)

    def close(self):
        pass


def _mock(monkeypatch, events=None, *, error=None):
    sent = {}

    def fake_urlopen(req, timeout=None):
        sent["req"] = req
        if error is not None:
            raise error
        return _Stream(events or [])

    monkeypatch.setattr(
        "gadfly.providers.anthropic_api.urllib.request.urlopen", fake_urlopen
    )
    return sent


def _text(t):
    return {"type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": t}}


def _stop(reason="end_turn"):
    return {"type": "message_delta", "delta": {"stop_reason": reason}}


_MSG_STOP = {"type": "message_stop"}


def test_text_completion(monkeypatch):
    sent = _mock(monkeypatch, [_text("hel"), _text("lo"), _stop(), _MSG_STOP])
    out = AnthropicAPIProvider("k").complete(system="sys", prompt="usr", model="claude-opus-4-8")
    assert out == "hello"
    req = sent["req"]
    assert req.full_url == "https://api.anthropic.com/v1/messages"
    assert req.get_header("X-api-key") == "k"
    assert req.get_header("Anthropic-version") == "2023-06-01"
    body = json.loads(req.data)
    assert body["model"] == "claude-opus-4-8"
    assert body["stream"] is True
    assert body["system"] == [
        {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    ]
    assert body["messages"] == [{"role": "user", "content": "usr"}]
    assert "output_config" not in body  # no schema → plain completion


def test_thinking_deltas_are_ignored(monkeypatch):
    # A thinking-on model streams thinking_delta before the answer; only text counts.
    _mock(monkeypatch, [
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "hmm..."}},
        _text("answer"), _stop(), _MSG_STOP,
    ])
    out = AnthropicAPIProvider("k").complete(system="s", prompt="u", model="m")
    assert out == "answer"


def test_schema_uses_output_config_and_returns_json(monkeypatch):
    sent = _mock(monkeypatch, [_text('{"decision": "allow"}'), _stop(), _MSG_STOP])
    schema = {"type": "object", "properties": {"decision": {"type": "string"}},
              "required": ["decision"], "additionalProperties": False}
    out = AnthropicAPIProvider("k").complete(
        system="s", prompt="u", model="m", schema=schema
    )
    assert json.loads(out) == {"decision": "allow"}
    body = json.loads(sent["req"].data)
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": schema}}
    assert "tools" not in body and "tool_choice" not in body  # not a forced tool call


def test_schema_invalid_json_raises(monkeypatch):
    _mock(monkeypatch, [_text("not json {"), _stop(), _MSG_STOP])
    with pytest.raises(LLMError):
        AnthropicAPIProvider("k").complete(system="s", prompt="u", model="m", schema={})


def test_refusal_raises(monkeypatch):
    _mock(monkeypatch, [_stop("refusal"), _MSG_STOP])
    with pytest.raises(LLMError):
        AnthropicAPIProvider("k").complete(system="s", prompt="u", model="m")


def test_max_tokens_truncation_raises(monkeypatch):
    # Reasoning burned the budget: partial JSON + stop_reason max_tokens must not slip through.
    _mock(monkeypatch, [_text('{"deci'), _stop("max_tokens"), _MSG_STOP])
    with pytest.raises(LLMError):
        AnthropicAPIProvider("k").complete(system="s", prompt="u", model="m", schema={})


def test_stream_dropped_before_terminal_is_transient(monkeypatch):
    # Stream closes with no message_delta (mid-stream drop) → retryable, not a silent success.
    _mock(monkeypatch, [_text("partial")])
    with pytest.raises(LLMTransientError):
        AnthropicAPIProvider("k").complete(system="s", prompt="u", model="m")


def test_stream_error_event_is_transient(monkeypatch):
    _mock(monkeypatch, [{"type": "error", "error": {"type": "overloaded_error",
                                                     "message": "overloaded"}}])
    with pytest.raises(LLMTransientError):
        AnthropicAPIProvider("k").complete(system="s", prompt="u", model="m")


def test_rate_limit_is_transient(monkeypatch):
    err = urllib.error.HTTPError("u", 429, "rate", {}, io.BytesIO(b'{"error":"slow down"}'))
    _mock(monkeypatch, error=err)
    with pytest.raises(LLMTransientError):
        AnthropicAPIProvider("k").complete(system="s", prompt="u", model="m")


def test_server_error_is_transient(monkeypatch):
    err = urllib.error.HTTPError("u", 503, "down", {}, io.BytesIO(b"{}"))
    _mock(monkeypatch, error=err)
    with pytest.raises(LLMTransientError):
        AnthropicAPIProvider("k").complete(system="s", prompt="u", model="m")


def test_bad_request_is_permanent(monkeypatch):
    err = urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b'{"error":"nope"}'))
    _mock(monkeypatch, error=err)
    with pytest.raises(LLMError) as ei:
        AnthropicAPIProvider("k").complete(system="s", prompt="u", model="m")
    assert not isinstance(ei.value, LLMTransientError)


def test_network_failure_is_transient(monkeypatch):
    _mock(monkeypatch, error=urllib.error.URLError("connection refused"))
    with pytest.raises(LLMTransientError):
        AnthropicAPIProvider("k").complete(system="s", prompt="u", model="m")
