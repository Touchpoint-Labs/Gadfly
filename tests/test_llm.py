"""Retry logic and CLI-envelope parsing (no network; subprocess mocked, except the two
streaming tests that spawn a real child to exercise the timeout / stderr-drain fix)."""

import json
import subprocess
import sys
import time

import pytest

from gadfly.providers import llm
from gadfly.providers.llm import (
    ClaudeCliProvider,
    LLMError,
    LLMTransientError,
    complete_with_retry,
)


class _Flaky:
    def __init__(self, fail_times, result="ok"):
        self.calls = 0
        self.fail_times = fail_times
        self.result = result

    def complete(self, **kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise LLMTransientError("blip")
        return self.result


def test_retry_succeeds_after_transient():
    p = _Flaky(fail_times=2)
    assert complete_with_retry(p, system="s", prompt="p", model="m") == "ok"
    assert p.calls == 3


def test_retry_exhausts_then_raises():
    p = _Flaky(fail_times=9)
    with pytest.raises(LLMTransientError):
        complete_with_retry(p, system="s", prompt="p", model="m", attempts=3)
    assert p.calls == 3


def test_permanent_error_is_not_retried():
    class P:
        calls = 0

        def complete(self, **kw):
            P.calls += 1
            raise LLMError("bad flag")

    with pytest.raises(LLMError):
        complete_with_retry(P(), system="s", prompt="p", model="m")
    assert P.calls == 1


def _fake_run(stdout="", returncode=0):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(
            cmd, returncode, stdout=stdout, stderr="boom"
        )

    return run


class _FakePipe:
    def __init__(self, lines=None):
        self.lines = lines or []
        self.written = ""

    def __iter__(self):
        return iter(self.lines)

    def write(self, text):
        self.written += text

    def close(self):
        pass

    def read(self):
        return ""


class _FakePopen:
    last = None

    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.kw = kw
        self.stdin = _FakePipe()
        self.stdout = _FakePipe(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "StructuredOutput",
                                    "input": {"dangerous": True},
                                }
                            ]
                        },
                    }
                )
                + "\n",
                json.dumps(
                    {"type": "result", "structured_output": {"dangerous": False}}
                )
                + "\n",
            ]
        )
        self.stderr = _FakePipe()
        self.terminated = False
        self.killed = False
        _FakePopen.last = self

    def poll(self):
        return 0 if self.terminated or self.killed else None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.terminated = True
        return 0


def _last_popen():
    assert _FakePopen.last is not None
    return _FakePopen.last


def test_cli_returns_result_text(monkeypatch):
    monkeypatch.setattr(
        llm.subprocess,
        "run",
        _fake_run(json.dumps({"is_error": False, "result": "pong"})),
    )
    assert ClaudeCliProvider().complete(system="s", prompt="p", model="m") == "pong"


def test_cli_returns_structured_output_for_schema(monkeypatch):
    monkeypatch.setattr(llm.subprocess, "Popen", _FakePopen)
    out = ClaudeCliProvider().complete(
        system="s", prompt="p", model="m", schema={"type": "object"}
    )
    assert json.loads(out) == {"dangerous": True}
    assert _last_popen().terminated


def test_schema_cli_uses_streaming_output(monkeypatch):
    monkeypatch.setattr(llm.subprocess, "Popen", _FakePopen)
    ClaudeCliProvider().complete(
        system="s", prompt="p", model="m", schema={"type": "object"}
    )
    assert "--output-format" in _last_popen().cmd
    assert "stream-json" in _last_popen().cmd
    assert "--verbose" in _last_popen().cmd


def test_cli_disallows_delegation_tools_by_default(monkeypatch):
    commands = []

    def run(cmd, **kw):
        commands.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"is_error": False, "result": "pong"}), stderr=""
        )

    monkeypatch.setattr(llm.subprocess, "run", run)
    ClaudeCliProvider().complete(system="s", prompt="p", model="m")
    disallowed = commands[0][commands[0].index("--disallowedTools") + 1]
    assert "Agent" in disallowed
    assert "TaskCreate" in disallowed


def test_cli_nonzero_exit_is_transient(monkeypatch):
    monkeypatch.setattr(llm.subprocess, "run", _fake_run("", returncode=1))
    with pytest.raises(LLMTransientError):
        ClaudeCliProvider().complete(system="s", prompt="p", model="m")


def test_cli_is_error_flag_is_transient(monkeypatch):
    monkeypatch.setattr(
        llm.subprocess,
        "run",
        _fake_run(json.dumps({"is_error": True, "subtype": "error_max_turns"})),
    )
    with pytest.raises(LLMTransientError):
        ClaudeCliProvider().complete(system="s", prompt="p", model="m")


def test_cli_non_json_is_permanent(monkeypatch):
    monkeypatch.setattr(llm.subprocess, "run", _fake_run("not json"))
    with pytest.raises(LLMError):
        ClaudeCliProvider().complete(system="s", prompt="p", model="m")


# --- streaming path: timeout on a silent hang, no stderr-pipe deadlock (real subprocess) ---

def test_streaming_times_out_on_silent_hang():
    # a child that emits NO output must trip the deadline (the reader-thread timeout), not
    # block forever on a silent readline
    p = ClaudeCliProvider(timeout=0.5)
    start = time.monotonic()
    with pytest.raises(LLMTransientError):
        p._complete_schema_streaming([sys.executable, "-c", "import time; time.sleep(30)"], "x", {"type": "object"})
    assert time.monotonic() - start < 5   # tripped by the 0.5s deadline, not the 30s sleep


def test_streaming_survives_large_stderr_without_deadlock():
    # a child that floods stderr past the 64KB pipe buffer before writing stdout must not
    # deadlock — stderr is a temp file, so the child never blocks on an undrained pipe
    result = json.dumps({"type": "result", "structured_output": {"ok": True}})
    script = (
        "import sys; sys.stderr.write('e' * 300000); sys.stderr.flush(); "
        f"sys.stdout.write({result!r} + chr(10)); sys.stdout.flush()"
    )
    p = ClaudeCliProvider(timeout=10)
    out = p._complete_schema_streaming([sys.executable, "-c", script], "x", {"type": "object"})
    assert json.loads(out) == {"ok": True}


def test_cli_tools_off_uses_tools_empty(monkeypatch):
    # tools=False must genuinely disable tools via `--tools ""` — NOT `--allowedTools ""`
    # (which claude -p treats as no filter, verified in-session).
    commands = []

    def run(cmd, **kw):
        commands.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"is_error": False, "result": "ok"}), stderr=""
        )

    monkeypatch.setattr(llm.subprocess, "run", run)
    ClaudeCliProvider().complete(system="s", prompt="p", model="m", tools=False)
    cmd = commands[0]
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--allowedTools" not in cmd


def test_review_exceeds_tool_cap_is_permanent():
    # a reviewer that keeps calling tools past the 5-cap → permanent LLMError (→ step-aside),
    # never retried and never a silent allow.
    script = (
        "import sys, json; "
        "m = json.dumps({'message': {'content': [{'type': 'tool_use', 'name': 'Read', 'input': {}}]}}); "
        "[sys.stdout.write(m + chr(10)) or sys.stdout.flush() for _ in range(6)]"
    )
    p = ClaudeCliProvider(timeout=10)
    with pytest.raises(LLMError) as ei:
        p._complete_schema_streaming([sys.executable, "-c", script], "x", {"type": "object"})
    assert not isinstance(ei.value, LLMTransientError)


# --- schema-fumble resilience: unwrap wrappers, skip garbage, retry on empty ---

_VERDICTS_SCHEMA = {
    "type": "object",
    "required": ["verdicts"],
    "properties": {"verdicts": {"type": "array"}},
    "additionalProperties": False,
}


def test_nested_wrapper_payload_is_unwrapped():
    # models sometimes wrap the payload in the tool name; the provider must unwrap it
    line = json.dumps({"message": {"content": [{"type": "tool_use", "name": "StructuredOutput",
                                                "input": {"StructuredOutput": {"verdicts": []}}}]}})
    script = f"import sys; sys.stdout.write({line!r} + chr(10)); sys.stdout.flush()"
    p = ClaudeCliProvider(timeout=10)
    out = p._complete_schema_streaming([sys.executable, "-c", script], "x", _VERDICTS_SCHEMA)
    assert json.loads(out) == {"verdicts": []}


def test_nonconforming_payload_is_skipped_for_valid_result():
    # a payload CC rejected must not be accepted; the valid result event that follows wins
    bad = json.dumps({"message": {"content": [{"type": "tool_use", "name": "StructuredOutput",
                                               "input": {"decision": "allow"}}]}})
    good = json.dumps({"type": "result", "structured_output": {"verdicts": [{"decision": "allow"}]}})
    script = (f"import sys; sys.stdout.write({bad!r} + chr(10)); "
              f"sys.stdout.write({good!r} + chr(10)); sys.stdout.flush()")
    p = ClaudeCliProvider(timeout=10)
    out = p._complete_schema_streaming([sys.executable, "-c", script], "x", _VERDICTS_SCHEMA)
    assert json.loads(out) == {"verdicts": [{"decision": "allow"}]}


def test_no_structured_output_is_transient():
    # a session that ends without valid output must be retryable, not a permanent failure
    p = ClaudeCliProvider(timeout=10)
    with pytest.raises(LLMTransientError):
        p._complete_schema_streaming([sys.executable, "-c", "pass"], "x", _VERDICTS_SCHEMA)


def test_string_encoded_wrapper_payload_is_unwrapped():
    # the "$PARAMETER_NAME" fumble: single wrapper key whose value is the payload
    # JSON-encoded as a string
    inner = json.dumps({"verdicts": [{"decision": "allow"}]})
    line = json.dumps({"message": {"content": [{"type": "tool_use", "name": "StructuredOutput",
                                                "input": {"$PARAMETER_NAME": inner}}]}})
    script = f"import sys; sys.stdout.write({line!r} + chr(10)); sys.stdout.flush()"
    p = ClaudeCliProvider(timeout=10)
    out = p._complete_schema_streaming([sys.executable, "-c", script], "x", _VERDICTS_SCHEMA)
    assert json.loads(out) == {"verdicts": [{"decision": "allow"}]}
