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
        tools: bool = True,
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
    tools: bool = True,
) -> str:
    last: Optional[LLMTransientError] = None
    for _ in range(attempts):
        try:
            return provider.complete(
                system=system, prompt=prompt, model=model, schema=schema, tools=tools
            )
        except LLMTransientError as e:  # permanent LLMError propagates immediately
            last = e
    assert last is not None
    raise last


_TOOL_CAP = 5  # a review may use at most this many tools before it must produce a verdict


class ClaudeCliProvider:
    """Rides the user's Claude Code subscription via `claude -p` — no API key.

    System prompt goes via a temp file (no arg-length limit); the user prompt via
    stdin. With a schema, the result is read from the `structured_output` field.
    """

    def __init__(self, binary: str = "claude", timeout: int = 60):
        self._binary = binary
        self._timeout = timeout

    def _tool_args(self, tools: bool) -> list[str]:
        if tools:
            # Code reviewer / solo reviewers: mutating & delegation tools disallowed; Read/search
            # stay for the rare "must verify a fact" review (prompt-steered to be rare, capped at 5).
            return [
                "--disallowedTools",
                "Write,Edit,MultiEdit,NotebookEdit,Bash,Agent,TaskCreate",
            ]
        # No-tool calls (normal architect + text helpers): `--tools ""` disables ALL tools
        # (verified). A --disallowedTools denylist is NOT enough — models route around it via MCP
        # tools; and `--allowedTools ""` doesn't restrict at all. StructuredOutput (the --json-schema
        # verdict channel) survives `--tools ""`.
        return ["--tools", ""]

    def _complete_schema_streaming(self, cmd: list[str], prompt: str) -> str:
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
            proc.stdin.write(prompt)
            proc.stdin.close()
            threading.Thread(target=_pump, daemon=True).start()
            deadline = time.monotonic() + self._timeout
            tool_calls = 0
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
                    if item.get("type") != "tool_use":
                        continue
                    if item.get("name") == "StructuredOutput":
                        return json.dumps(item.get("input") or {})
                    tool_calls += 1
                    if tool_calls > _TOOL_CAP:
                        # Runaway backstop (prompt steers tools to be rare). Permanent, not
                        # transient: retrying re-spends the budget on a model already ignoring
                        # the instruction. Propagates → the review steps aside (D2), never allows.
                        raise LLMError(
                            f"review used more than {_TOOL_CAP} tools without a verdict"
                        )
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
            raise LLMError(
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
        tools: bool = True,
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
        cmd += self._tool_args(tools)
        if schema is not None:
            cmd += ["--json-schema", json.dumps(schema)]
        try:
            if schema is not None:
                return self._complete_schema_streaming(cmd, prompt)
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
                raise LLMError(
                    "claude -p returned no structured_output for a schema request"
                )
            return json.dumps(out)
        return env.get("result", "")
