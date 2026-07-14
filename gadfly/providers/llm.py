"""Provider-neutral LLM client.

The core calls `LLMProvider.complete()`; it knows nothing about Anthropic vs the
local `claude` CLI vs (later) OpenAI/local models. v1 ships the subscription-CLI
backend (no API key — shells out to `claude -p`); others plug in behind the
interface.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import tempfile
import threading
import time
from typing import Optional, Protocol


class LLMError(Exception):
    """Permanent failure — not worth retrying."""


class LLMTransientError(LLMError):
    """Transient failure (timeout, overload) — a retry may help."""


class LLMProvider(Protocol):
    def complete(
        self,
        *,
        system: str,
        prompt: str,
        model: str,
        schema: Optional[dict] = None,
    ) -> str:
        """Return the model's text, or a JSON string conforming to `schema`."""
        ...


def complete_with_retry(
    provider: LLMProvider,
    *,
    system: str,
    prompt: str,
    model: str,
    schema: Optional[dict] = None,
    attempts: int = 3,
) -> str:
    last: Optional[LLMTransientError] = None
    for _ in range(attempts):
        try:
            return provider.complete(
                system=system, prompt=prompt, model=model, schema=schema
            )
        except LLMTransientError as e:  # permanent LLMError propagates immediately
            last = e
    assert last is not None
    raise last


def _conforms(obj, schema: dict) -> bool:
    """Minimal top-level schema check: required keys present, no extras when the
    schema forbids them, and each present key's value has the schema's declared
    array/object type. Full validation stays CC-side; this guards against accepting
    a payload CC already rejected — including the {"verdicts": {"verdicts": [...]}}
    double-nesting, which passes a keys-only check but has a non-array `verdicts`."""
    if not isinstance(obj, dict):
        return False
    if any(k not in obj for k in schema.get("required", [])):
        return False
    props = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        if any(k not in props for k in obj):
            return False
    for k, v in obj.items():
        t = (props.get(k) or {}).get("type")
        if t == "array" and not isinstance(v, list):
            return False
        if t == "object" and not isinstance(v, dict):
            return False
    return True


class ClaudeCliProvider:
    """Rides the user's Claude Code subscription via `claude -p` — no API key.

    System prompt goes via a temp file (no arg-length limit); the user prompt via
    stdin. With a schema, the result is read from the `structured_output` field.
    """

    def __init__(self, binary: str = "claude", timeout: int = 60):
        self._binary = binary
        self._timeout = timeout

    def _complete_schema_streaming(self, cmd: list[str], prompt: str, schema: dict) -> str:
        # stderr → a temp file, never a PIPE: streaming drains only stdout, so an undrained
        # stderr PIPE would deadlock a chatty child once its 64KB buffer fills.
        stderr_file = tempfile.TemporaryFile()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            env={**os.environ, "GADFLY_HOOK_DISABLED": "1"},
        )
        # Read stdout on a thread so the deadline is enforced even if the child produces NO
        # output and hangs silently — a blocking `for line in proc.stdout` could not be.
        lines: "queue.Queue[Optional[str]]" = queue.Queue()

        def _pump() -> None:
            try:
                for line in proc.stdout:  # type: ignore[union-attr]
                    lines.put(line)
            finally:
                lines.put(None)  # sentinel: stdout closed

        try:
            assert proc.stdin is not None
            # Start draining stdout before writing stdin: a large prompt can outrun the
            # pipe buffer, and a child that emits before we drain would otherwise deadlock.
            threading.Thread(target=_pump, daemon=True).start()
            proc.stdin.write(prompt)
            proc.stdin.close()
            deadline = time.monotonic() + self._timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LLMTransientError(f"claude -p timed out after {self._timeout}s")
                try:
                    line = lines.get(timeout=remaining)
                except queue.Empty:
                    raise LLMTransientError(f"claude -p timed out after {self._timeout}s")
                if line is None:  # stdout closed without a structured result
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = event.get("message") or {}
                for item in msg.get("content") or []:
                    if item.get("type") != "tool_use" or item.get("name") != "StructuredOutput":
                        continue
                    payload = item.get("input") or {}
                    # models sometimes wrap the payload in a single key — occasionally
                    # with the payload itself JSON-encoded as a string; unwrap both
                    if not _conforms(payload, schema) and isinstance(payload, dict) and len(payload) == 1:
                        inner = next(iter(payload.values()))
                        if isinstance(inner, str):
                            try:
                                inner = json.loads(inner)
                            except json.JSONDecodeError:
                                inner = None
                        if _conforms(inner, schema):
                            payload = inner
                    if _conforms(payload, schema):
                        return json.dumps(payload)
                    # non-conforming: CC rejects it too — keep streaming so the
                    # model can retry in-session off CC's schema error
                if event.get("type") == "result":
                    if event.get("is_error"):
                        raise LLMTransientError(
                            f"claude -p reported error: {event.get('subtype')}"
                        )
                    out = event.get("structured_output")
                    if out is not None:
                        return json.dumps(out)
            if proc.wait(timeout=1) != 0:
                try:
                    stderr_file.seek(0)
                    err = stderr_file.read().decode("utf-8", "replace").strip()[:200]
                except OSError:
                    err = ""
                raise LLMTransientError(f"claude -p exited {proc.returncode}: {err}")
            raise LLMTransientError(
                "claude -p returned no structured_output for a schema request"
            )
        except subprocess.TimeoutExpired as e:
            raise LLMTransientError(
                f"claude -p timed out after {self._timeout}s"
            ) from e
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
            stderr_file.close()

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        model: str,
        schema: Optional[dict] = None,
    ) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(system)
            system_file = f.name
        output_format = "stream-json" if schema is not None else "json"
        cmd = [
            self._binary,
            "-p",
            "--model",
            model,
            "--system-prompt-file",
            system_file,
            "--output-format",
            output_format,
        ]
        if schema is not None:
            cmd += ["--verbose"]
        # Reviewers are read-only and tool-less: `--tools ""` disables ALL tools (verified —
        # a --disallowedTools denylist is bypassable via MCP). StructuredOutput (the
        # --json-schema verdict channel) survives it. Uncertainty is delegated to the builder.
        cmd += ["--tools", ""]
        if schema is not None:
            cmd += ["--json-schema", json.dumps(schema)]
        try:
            if schema is not None:
                return self._complete_schema_streaming(cmd, prompt, schema)
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env={**os.environ, "GADFLY_HOOK_DISABLED": "1"},
            )
        except subprocess.TimeoutExpired as e:
            raise LLMTransientError(
                f"claude -p timed out after {self._timeout}s"
            ) from e
        except FileNotFoundError as e:
            raise LLMError(f"claude binary not found: {self._binary!r}") from e
        finally:
            os.unlink(system_file)

        if proc.returncode != 0:
            raise LLMTransientError(
                f"claude -p exited {proc.returncode}: {proc.stderr.strip()[:200]}"
            )
        try:
            env = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise LLMError("claude -p returned non-JSON output") from e
        if env.get("is_error"):
            raise LLMTransientError(f"claude -p reported error: {env.get('subtype')}")
        if schema is not None:
            out = env.get("structured_output")
            if out is None:
                raise LLMTransientError(
                    "claude -p returned no structured_output for a schema request"
                )
            return json.dumps(out)
        return env.get("result", "")
