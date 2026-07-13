"""Claude Code installer — wires Gadfly's hooks into a settings file, conflict-safely.

`gadfly init` merges the five hooks into `.claude/settings.local.json` (local, auto-
gitignored by Claude Code) or `~/.claude/settings.json` (global). Claude Code merges hook
arrays ADDITIVELY across settings files and runs every matching group, so our entries — keyed
by their command string — sit beside a user's own hooks. We append, never replace; re-init
updates only ours; the write is atomic (temp → validate → move) over a `.bak`. Cross-file
duplicates can't be deduped from one file, so we detect and warn on them instead.

Claude-Code-specific by design: it speaks CC's settings format and dispatches CC's hooks. The
neutral CLI shell (gadfly/cli.py) calls in here; the core never sees any of it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# The gate's registered ceiling; the pretooluse clamp keeps retries x timeout under it.
PRETOOLUSE_TIMEOUT = 600


def find_workspace(cwd) -> Path:
    """Nearest ancestor (including cwd) containing gadfly.toml — the workspace root.
    Hooks receive the session's CURRENT directory, which follows the builder's `cd`;
    trusting it verbatim fractures state into nested .gadfly dirs and drops spec.md
    from review context. Falls back to cwd when no gadfly.toml is found."""
    start = Path(cwd or ".").resolve()
    for p in (start, *start.parents):
        if (p / "gadfly.toml").is_file():
            return p
    return start

# CC event -> (matcher | None, timeout seconds | None). Ceilings are sized to each
# hook's legitimate work: Stop and SessionStart run inline LLM passes (feedback
# extraction, memory compaction) that can take up to llm_timeout each.
_HOOKS: dict[str, tuple[str | None, int | None]] = {
    "UserPromptSubmit": (None, 600),  # one-time midwife pass is an inline LLM call
    "PreToolUse": ("Write|Edit|MultiEdit|Bash", PRETOOLUSE_TIMEOUT),
    "PostToolUse": ("Write|Edit|MultiEdit|NotebookEdit", 120),
    "SessionStart": (None, 600),
    "Stop": (None, 600),
}
# CC event -> the `gadfly hook <name>` subcommand that runs it
_EVENT_CMD: dict[str, str] = {
    "UserPromptSubmit": "userpromptsubmit",
    "PreToolUse": "pretooluse",
    "PostToolUse": "posttooluse",
    "SessionStart": "sessionstart",
    "Stop": "stop",
}
# A group is ours if a hook command runs Gadfly — the new CLI form (`gadfly hook X`) or the
# legacy module form (`python -m gadfly.adapters.claudecode.hooks.X`) — so init/status
# recognize and migrate old installs instead of duplicating them.
_MARKERS = ("gadfly hook ", "gadfly.adapters.claudecode.hooks")


# --- hook command + settings-group construction ------------------------------

def _gadfly_bin() -> str:
    """Absolute path to this interpreter's `gadfly` script, so the hook resolves without
    an activated venv. Falls back to a bare `gadfly` if the script isn't beside python yet."""
    cand = Path(sys.executable).parent / "gadfly"
    return str(cand) if cand.exists() else "gadfly"


def _hook_command(event: str) -> str:
    return f"{_gadfly_bin()} hook {_EVENT_CMD[event]}"


def _group(event: str) -> dict:
    matcher, timeout = _HOOKS[event]
    hook: dict = {"type": "command", "command": _hook_command(event)}
    if timeout is not None:
        hook["timeout"] = timeout
    return ({"matcher": matcher, "hooks": [hook]} if matcher else {"hooks": [hook]})


def _is_ours(group: dict) -> bool:
    return any(
        any(m in (h.get("command") or "") for m in _MARKERS)
        for h in group.get("hooks", []) if isinstance(h, dict)
    )


def settings_path(scope: str, cwd: Path) -> Path:
    """global → ~/.claude/settings.json; local → <cwd>/.claude/settings.local.json."""
    if scope == "global":
        return Path.home() / ".claude" / "settings.json"
    return Path(cwd) / ".claude" / "settings.local.json"


def _all_settings(cwd: Path) -> list[Path]:
    """Every file CC merges hooks from, most local first."""
    return [
        settings_path("local", cwd),
        Path(cwd) / ".claude" / "settings.json",
        settings_path("global", cwd),
    ]


def _scan_file(path: Path) -> tuple[list[str], str | None]:
    """Events this file has Gadfly hooks for, plus the PreToolUse command actually wired
    (so status can test exactly what CC will run, not a re-derived guess)."""
    if not path.exists():
        return [], None
    try:
        root = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError:
        return [], None
    hooks = root.get("hooks", {}) if isinstance(root, dict) else {}
    if not isinstance(hooks, dict):
        return [], None
    events, pretool = [], None
    for event, arr in hooks.items():
        if not isinstance(arr, list):
            continue
        ours = [g for g in arr if isinstance(g, dict) and _is_ours(g)]
        if not ours:
            continue
        events.append(event)
        if event == "PreToolUse":
            for g in ours:
                for h in g.get("hooks", []):
                    cmd = h.get("command") if isinstance(h, dict) else None
                    if cmd and any(m in cmd for m in _MARKERS):
                        pretool = pretool or cmd
    return events, pretool


# --- the conflict-safe merge -------------------------------------------------

@dataclass
class InstallResult:
    path: Path
    added: list[str]
    updated: list[str]
    backup: Path | None
    warnings: list[str] = field(default_factory=list)


def install(scope: str, cwd: Path) -> InstallResult:
    """Merge Gadfly's hooks into the settings file, additively and idempotently."""
    cwd = Path(cwd)
    path = settings_path(scope, cwd)
    path.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    backup: Path | None = None
    if path.exists():
        raw = path.read_text()
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as e:
            raise SystemExit(
                f"{path} is not valid JSON ({e}). Fix or move it, then re-run `gadfly init` "
                "— refusing to overwrite a file I can't parse."
            )
        if not isinstance(data, dict):
            raise SystemExit(f"{path} is not a JSON object; aborting to avoid clobbering it.")
        backup = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup)

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit(f'"hooks" in {path} is not an object; aborting.')

    added, updated = [], []
    for event in _HOOKS:
        arr = hooks.setdefault(event, [])
        if not isinstance(arr, list):
            raise SystemExit(f'"hooks.{event}" in {path} is not a list; aborting.')
        mine = [i for i, g in enumerate(arr) if isinstance(g, dict) and _is_ours(g)]
        group = _group(event)
        if mine:
            arr[mine[0]] = group  # refresh in place (path/timeout may have changed)
            for j in reversed(mine[1:]):
                arr.pop(j)  # collapse any accidental duplicates
            updated.append(event)
        else:
            arr.append(group)  # append beside the user's own groups — never replace
            added.append(event)

    _atomic_write(path, data)
    warnings = _other_installs(cwd, path)  # Gadfly hooks in another settings file → double review
    return InstallResult(path=path, added=added, updated=updated, backup=backup, warnings=warnings)


def _other_installs(cwd: Path, exclude: Path) -> list[str]:
    """Gadfly hooks in the OTHER settings files CC merges — can't be deduped from a single
    file, so surface them (install: double review; uninstall: leftovers you still have)."""
    out: list[str] = []
    for other in _all_settings(cwd):
        if other == exclude:
            continue
        ev, _ = _scan_file(other)
        if ev:
            out.append(f"Gadfly hooks also present in {other} ({len(ev)} events) — CC merges all "
                       "settings files, so they still run; remove them there.")
    return out


@dataclass
class UninstallResult:
    path: Path
    removed: list[str]
    backup: Path | None
    warnings: list[str] = field(default_factory=list)


def uninstall(scope: str, cwd: Path) -> UninstallResult:
    """Remove ONLY Gadfly's hook groups from the settings file, leaving the user's untouched.
    Inverse of install(); the same command marker identifies ours. Mirrors the scopes install()
    writes (local/global) — hooks in the committed .claude/settings.json were placed by hand and
    are surfaced, never auto-edited. The cross-file warning is unconditional (fires even when the
    target scope is empty); the file is touched only if we actually remove something."""
    cwd = Path(cwd)
    path = settings_path(scope, cwd)
    warnings = _other_installs(cwd, path)
    if not path.exists():
        return UninstallResult(path=path, removed=[], backup=None, warnings=warnings)
    try:
        data = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"{path} is not valid JSON ({e}); aborting.")
    if not isinstance(data, dict):
        raise SystemExit(f"{path} is not a JSON object; aborting.")
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return UninstallResult(path=path, removed=[], backup=None, warnings=warnings)

    removed = []
    for event, arr in list(hooks.items()):
        if not isinstance(arr, list):
            continue
        kept = [g for g in arr if not (isinstance(g, dict) and _is_ours(g))]
        if len(kept) == len(arr):
            continue  # nothing of ours in this event
        removed.append(event)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]  # we emptied it — don't leave a dangling []
    if not removed:
        return UninstallResult(path=path, removed=[], backup=None, warnings=warnings)  # true no-op
    if not hooks:
        data.pop("hooks", None)
    backup = path.with_name(path.name + ".bak")
    shutil.copy2(path, backup)
    _atomic_write(path, data)
    return UninstallResult(path=path, removed=removed, backup=backup, warnings=warnings)


def _atomic_write(path: Path, data: dict) -> None:
    text = json.dumps(data, indent=2) + "\n"
    json.loads(text)  # validate before we touch the real file
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)  # atomic on the same filesystem
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- hook dispatch (the `gadfly hook <event>` entrypoint) --------------------

def run_hook(event: str) -> int:
    """Dispatch to a CC hook module's main(). The module reads stdin / writes stdout itself."""
    from .hooks import posttooluse, pretooluse, sessionstart, stop, userpromptsubmit
    mains = {
        "pretooluse": pretooluse.main,
        "posttooluse": posttooluse.main,
        "sessionstart": sessionstart.main,
        "stop": stop.main,
        "userpromptsubmit": userpromptsubmit.main,
    }
    mains[event]()
    return 0


# --- disable/enable sentinel (checked by every hook) -------------------------

def disabled_path(cwd: Path) -> Path:
    return Path(cwd) / ".gadfly" / "disabled"


def is_disabled(cwd: Path) -> bool:
    return disabled_path(cwd).exists()


# --- status / doctor ---------------------------------------------------------

@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    critical: bool = True


def _hook_runs(cwd: Path, command: str | None) -> Check:
    """Smoke-test the ACTUAL wired command (falling back to the derived one if none is wired):
    feed a benign Read event (Tier-0 auto-allow, no LLM) in a throwaway cwd, confirm exit 0
    with parseable output. Testing what CC will really run — not a re-derived guess."""
    cmd = (command or _hook_command("PreToolUse")).split()
    with tempfile.TemporaryDirectory() as tmp:
        event = json.dumps({
            "tool_name": "Read", "tool_input": {"file_path": str(Path(tmp) / "x")},
            "cwd": tmp, "session_id": "gadfly-status-check",
        })
        try:
            p = subprocess.run(cmd, input=event, capture_output=True, text=True, timeout=30, cwd=tmp)
        except FileNotFoundError:
            return Check("hook runs", False, f"`{cmd[0]}` not found — is gadfly installed / the venv path valid?")
        except subprocess.TimeoutExpired:
            return Check("hook runs", False, "pretooluse hook timed out")
    if p.returncode != 0:
        return Check("hook runs", False, f"exit {p.returncode}: {(p.stderr or '').strip()[:200]}")
    out = (p.stdout or "").strip()
    if out:
        try:
            json.loads(out)
        except json.JSONDecodeError:
            return Check("hook runs", False, f"emitted non-JSON: {out[:120]}")
    return Check("hook runs", True, f"`{' '.join(cmd[:3])}…` executed and returned a valid verdict")


def status(cwd: Path) -> list[Check]:
    from ...config import load
    cwd = Path(cwd)
    checks: list[Check] = []

    scanned = [(p, _scan_file(p)) for p in _all_settings(cwd)]
    wired = [(p, ev, cmd) for p, (ev, cmd) in scanned if ev]
    pretool_cmd = None
    if not wired:
        checks.append(Check("hooks installed", False, "no Gadfly hooks found — run `gadfly init`"))
    else:
        events = set().union(*(set(ev) for _, ev, _ in wired))
        where = ", ".join(p.name for p, _, _ in wired)
        checks.append(Check("hooks installed", len(events) == len(_HOOKS),
                            f"{len(events)}/{len(_HOOKS)} events ({where})"))
        if len(wired) > 1:
            checks.append(Check("single install", False,
                                f"Gadfly hooks in {len(wired)} files ({where}) — calls reviewed multiple times",
                                critical=False))
        pretool_cmd = next((c for _, _, c in wired if c), None)

    checks.append(_hook_runs(cwd, pretool_cmd))

    toml = cwd / "gadfly.toml"
    try:
        cfg = load(toml if toml.exists() else None)
        checks.append(Check("config", True,
                            f"autonomy={cfg.autonomy}, architect={cfg.models.architect}, code={cfg.models.code}"))
    except Exception as e:
        checks.append(Check("config", False, f"gadfly.toml invalid: {e}"))

    checks.append(Check("claude CLI", shutil.which("claude") is not None,
                        "found" if shutil.which("claude") else "`claude` not on PATH — the LLM provider needs it"))

    spec = cwd / "spec.md"
    checks.append(Check("spec.md", spec.exists(),
                        "present" if spec.exists() else "missing — the architect has no vision to enforce yet",
                        critical=False))

    if is_disabled(cwd):
        checks.append(Check("enabled", False, "Gadfly is DISABLED here (`gadfly enable` to resume)", critical=False))

    return checks
