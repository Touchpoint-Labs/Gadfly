"""Anthropic Messages API backend — raw HTTP over urllib, no SDK, no dependencies.

Same `LLMProvider.complete()` contract as the CLI backend, so a supervisor can run on the
metered API instead of the Claude Code subscription (config picks per role).

Requests stream (SSE) — the subscription CLI streams too, and it keeps a long, thinking-heavy
review (fable/sonnet reason for minutes) from an idle-connection drop. `max_tokens` is required
by the API, so rather than omit it we send it high enough (62000 — under every model's output
cap, incl. haiku's 64k) that it never binds and reasoning tokens can't truncate the verdict.
Structured output uses the native structured-outputs field (`output_config.format` = json_schema):
the response text is JSON for the schema. A forced tool_choice would 400 whenever thinking is on
(sonnet-5/fable-5 think by default), so it isn't used.

The API key is read from an env var by the factory and passed in; never stored in gadfly.toml.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

from .llm import LLMError, LLMTransientError

_ENDPOINT = "https://api.anthropic.com/v1/messages"
_VERSION = "2023-06-01"
_MAX_TOKENS = 62000  # non-binding ceiling: under every model's output cap, above any verdict


class AnthropicAPIProvider:
    def __init__(self, api_key: str, *, timeout: int = 60, max_tokens: int = _MAX_TOKENS):
        self._key = api_key
        self._timeout = timeout
        self._max_tokens = max_tokens

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        model: str,
        schema: Optional[dict] = None,
    ) -> str:
        # This backend grants no filesystem access — reviewers are read-only by construction.
        # The system block is the stable prefix (role + spec + claude + cross-project),
        # identical across a session's gates — cache it (1h TTL, the max, so it survives the
        # think-time gaps between gates) so repeat gates bill it at ~0.1x. Volatile context
        # (codemap, convo, change) rides the user prompt, past the breakpoint.
        body: dict = {
            "model": model,
            "max_tokens": self._max_tokens,
            "stream": True,
            "system": [
                {"type": "text", "text": system,
                 "cache_control": {"type": "ephemeral", "ttl": "1h"}}
            ],
            "messages": [{"role": "user", "content": prompt}],
        }
        if schema is not None:
            body["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        text, stop_reason = self._stream(body)
        if stop_reason == "refusal":
            raise LLMError("anthropic api declined the request (stop_reason=refusal)")
        if stop_reason == "max_tokens":
            raise LLMError(
                f"anthropic api response hit the {self._max_tokens}-token cap (truncated)"
            )
        if schema is None:
            return text
        try:  # structured outputs guarantees valid JSON; guard a truncated/empty body
            json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError("anthropic api returned no valid structured output") from e
        return text

    def _stream(self, body: dict) -> tuple[str, str]:
        """POST and consume the SSE stream → (accumulated text, stop_reason). A reader thread
        feeds a queue so self._timeout bounds the whole call even on a silent hang — the same
        deadline shape as the CLI backend's streaming path."""
        req = urllib.request.Request(
            _ENDPOINT,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "x-api-key": self._key,
                "anthropic-version": _VERSION,
                "content-type": "application/json",
            },
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=self._timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace").strip()[:200]
            # 429 (rate limit) and 5xx (server) are transient → retried; other 4xx are permanent.
            if e.code == 429 or e.code >= 500:
                raise LLMTransientError(f"anthropic api {e.code}: {detail}") from e
            raise LLMError(f"anthropic api {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise LLMTransientError(f"anthropic api request failed: {e}") from e

        lines: "queue.Queue[Optional[bytes]]" = queue.Queue()

        def _pump() -> None:
            try:
                for raw in resp:
                    lines.put(raw)
            except Exception:
                pass  # a main-thread resp.close() (timeout) interrupts the read; drop → sentinel
            finally:
                lines.put(None)  # sentinel: stream closed (cleanly or via a mid-stream drop)

        threading.Thread(target=_pump, daemon=True).start()
        parts: list[str] = []
        stop_reason: Optional[str] = None
        deadline = time.monotonic() + self._timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LLMTransientError(
                        f"anthropic api stream timed out after {self._timeout}s"
                    )
                try:
                    raw = lines.get(timeout=remaining)
                except queue.Empty:
                    raise LLMTransientError(
                        f"anthropic api stream timed out after {self._timeout}s"
                    )
                if raw is None:  # stream closed
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                kind = ev.get("type")
                if kind == "content_block_delta":
                    delta = ev.get("delta") or {}
                    if delta.get("type") == "text_delta":  # skips thinking_delta
                        parts.append(delta.get("text", ""))
                elif kind == "message_delta":
                    sr = (ev.get("delta") or {}).get("stop_reason")
                    if sr:
                        stop_reason = sr
                elif kind == "error":  # mid-stream server error (e.g. overloaded)
                    msg = (ev.get("error") or {}).get("message", "")
                    raise LLMTransientError(f"anthropic api stream error: {msg[:200]}")
            if stop_reason is None:
                # closed before a terminal message_delta → dropped/truncated mid-stream. A drop
                # is retryable, and returning the partial text would look like a clean success.
                raise LLMTransientError("anthropic api stream ended before a terminal event")
        finally:
            resp.close()
        return "".join(parts), stop_reason
