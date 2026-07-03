"""Gadfly command-line interface — the neutral shell.

`pip install gadfly` exposes the `gadfly` command (see pyproject `[project.scripts]`).
Agent-agnostic commands (config, disable/enable, version) live here; agent-specific
install / status / hook logic lives in the Claude Code adapter
(adapters/claudecode/install.py), so supporting another host agent is a new adapter, not a
CLI rewrite.

    gadfly init [global]      wire the hooks into this folder (or ~/.claude with `global`)
    gadfly uninstall [global] remove them again
    gadfly status             check the install is live (actually runs a hook)
    gadfly disable | enable   pause / resume without touching settings
    gadfly config [key [val]] show the config, get one key, or set one
    gadfly version
    gadfly hook <event>       internal: run a hook (what settings.json calls)
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .adapters.claudecode import install as cc

try:
    _VERSION = version("gadfly")
except PackageNotFoundError:
    _VERSION = "0.1.0+dev"

_HOOK_EVENTS = ["pretooluse", "posttooluse", "sessionstart", "stop", "userpromptsubmit"]

_TOML_TEMPLATE = '''\
# Gadfly configuration. Every key is optional — omit any to keep its default.

provider = "claude_cli"        # rides your Claude Code subscription; no API key

# How often Gadfly surfaces consequential-but-undiscussed decisions to you:
#   autonomous | balanced | collaborative   (irreversible ops always ask)
autonomy = "balanced"

[models]
architect = "claude-opus-4-8"  # the Socratic architect: drift, design, vision
code = "claude-sonnet-5"       # the code skeptic: bugs, edges, hallucinated APIs
triage = "claude-haiku-4-5"    # fast command-safety triage
'''


# --- scaffolding -------------------------------------------------------------

def _scaffold(cwd: Path) -> list[str]:
    """Create a default gadfly.toml and gitignore .gadfly/. Deliberately does NOT create
    spec.md: a placeholder would spend the midwife's one interrogation on template text —
    absent, the midwife waits for a real spec (and `status` flags it)."""
    created = []
    toml = cwd / "gadfly.toml"
    if not toml.exists():
        toml.write_text(_TOML_TEMPLATE)
        created.append("gadfly.toml")
    gi = cwd / ".gitignore"
    existing = gi.read_text() if gi.exists() else ""
    if ".gadfly/" not in existing.split():
        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        with gi.open("a") as f:
            f.write(f"{prefix}.gadfly/\n")
        created.append(".gitignore (+.gadfly/)")
    return created


# --- TOML get/set (zero-dep, comment-preserving line editor) -----------------

def _fmt_toml(value: str) -> str:
    if value.lower() in ("true", "false"):
        return value.lower()
    for cast in (int, float):
        try:
            cast(value)
            return value
        except ValueError:
            pass
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _is_header(line: str) -> bool:
    s = line.strip()
    return s.startswith("[") and s.endswith("]")


def _toml_set(path: Path, key: str, value: str) -> None:
    """Set one key in a TOML file, preserving comments/formatting. Supports top-level keys
    and one-level `section.key`, inserting within the correct table — never after a later
    header (which TOML would silently reparent)."""
    section, _, name = key.rpartition(".")
    val = _fmt_toml(value)
    lines = path.read_text().splitlines() if path.exists() else []
    headers = [i for i, l in enumerate(lines) if _is_header(l)]

    start = end = None
    if section == "":
        start, end = 0, (headers[0] if headers else len(lines))
    else:
        for idx, i in enumerate(headers):
            if lines[i].strip()[1:-1] == section:
                start = i + 1
                end = headers[idx + 1] if idx + 1 < len(headers) else len(lines)
                break

    if start is not None:
        for i in range(start, end):
            s = lines[i].strip()
            if "=" in s and s.split("=", 1)[0].strip() == name:
                # keep the line's trailing inline comment (config values never contain '#')
                h = lines[i].find("#")
                comment = f"  {lines[i][h:].rstrip()}" if h > lines[i].find("=") else ""
                lines[i] = f"{name} = {val}{comment}"
                break
        else:
            at = end
            while at - 1 >= start and not lines[at - 1].strip():
                at -= 1  # keep the key inside the table, above trailing blank lines
            lines.insert(at, f"{name} = {val}")
    else:  # section doesn't exist yet
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"[{section}]", f"{name} = {val}"]
    path.write_text("\n".join(lines) + "\n")


def _flatten(cfg) -> dict:
    out = {}
    for k, v in asdict(cfg).items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                out[f"{k}.{k2}"] = v2
        else:
            out[k] = v
    return out


# --- command handlers --------------------------------------------------------

_NO_SPEC = (
    "No spec.md found — the architect anchors to it, so Gadfly needs one first.\n"
    "Sketch the project with your AI assistant, save it as spec.md, then re-run `gadfly init`."
)


def _cmd_init(args) -> int:
    from .state.memory import ProjectMemory
    cwd = Path.cwd()
    # spec.md is mandatory for a local (per-project) install — without it the architect has
    # no vision to enforce. Global installs can't check (no single project), so they advise.
    if args.scope == "local" and not ProjectMemory(cwd).spec.strip():
        print("✗ " + _NO_SPEC, file=sys.stderr)
        return 1
    res = cc.install(args.scope, cwd)
    print(f"✓ Gadfly hooks → {res.path}")
    if res.added:
        print(f"  added:   {', '.join(res.added)}")
    if res.updated:
        print(f"  updated: {', '.join(res.updated)}")
    if res.backup:
        print(f"  backup:  {res.backup.name}")
    if args.scope == "local":
        created = _scaffold(cwd)
        if created:
            print(f"  created: {', '.join(created)}")
    for w in res.warnings:
        print(f"  ⚠ {w}")
    if args.scope == "global":
        print("\nGlobal install — Gadfly supervises every folder you run Claude Code in.\n"
              "Each project needs its own spec.md (the architect anchors to it); claude.md optional.")
    else:
        print("\nNext: `gadfly status` to verify, then run Claude Code in this folder.")
    return 0


def _cmd_uninstall(args) -> int:
    res = cc.uninstall(args.scope, Path.cwd())
    if res.removed:
        print(f"✓ Removed Gadfly hooks ({', '.join(res.removed)}) from {res.path}")
        if res.backup:
            print(f"  backup: {res.backup.name}")
    else:
        print(f"No Gadfly hooks in {res.path} — nothing to remove.")
    for w in res.warnings:
        print(f"  ⚠ {w}")
    return 0


def _cmd_status(args) -> int:
    checks = cc.status(Path.cwd())
    healthy = True
    for c in checks:
        mark = "✓" if c.ok else ("✗" if c.critical else "•")
        if not c.ok and c.critical:
            healthy = False
        print(f"  {mark} {c.name}: {c.detail}")
    print("\nGadfly looks healthy." if healthy else "\nSome checks failed — see above.")
    return 0 if healthy else 1


def _cmd_disable(args) -> int:
    p = cc.disabled_path(Path.cwd())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("disabled by `gadfly disable`\n")
    print("Gadfly disabled here — hooks stay wired but no-op. Resume with `gadfly enable`.")
    return 0


def _cmd_enable(args) -> int:
    from .state.edits import EditLedger
    cwd = Path.cwd()
    p = cc.disabled_path(cwd)
    if not p.exists():
        print("Gadfly was already enabled.")
        return 0
    # Re-baseline BEFORE lifting the sentinel: if reset throws, Gadfly stays disabled (safe)
    # rather than resuming un-baselined and misreading window edits as human corrections.
    EditLedger(cwd / ".gadfly").reset()
    p.unlink()
    print("Gadfly enabled — ledger re-baselined.")
    return 0


def _cmd_config(args) -> int:
    from .config import load
    cwd = Path.cwd()
    toml = cwd / "gadfly.toml"
    try:
        flat = _flatten(load(toml if toml.exists() else None))
    except Exception as e:
        print(f"gadfly.toml invalid: {e}", file=sys.stderr)
        return 1

    if args.value is not None:  # set — reject unknown keys, then write / validate / revert
        if args.key not in flat:
            print(f"unknown key {args.key!r} — run `gadfly config` for the valid keys", file=sys.stderr)
            return 1
        before = toml.read_text() if toml.exists() else None
        _toml_set(toml, args.key, args.value)
        try:
            load(toml)
        except Exception as e:
            if before is None:
                toml.unlink()
            else:
                toml.write_text(before)
            print(f"rejected: {args.key}={args.value} makes gadfly.toml invalid ({e})", file=sys.stderr)
            return 1
        print(f"set {args.key} = {args.value}  ({toml.name})")
        return 0

    if args.key:  # get
        if args.key in flat:
            print(flat[args.key])
            return 0
        print(f"unknown key {args.key!r}", file=sys.stderr)
        return 1

    for k, v in flat.items():  # show all
        print(f"{k} = {v}")
    return 0


def _cmd_hook(args) -> int:
    return cc.run_hook(args.event)


def _cmd_version(args) -> int:
    print(f"gadfly {_VERSION}")
    return 0


# --- entry point -------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="gadfly",
                                description="Socratic supervision for AI coding agents.")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    pi = sub.add_parser("init", help="wire Gadfly's hooks into this folder (or `init global`)")
    pi.add_argument("scope", nargs="?", choices=["local", "global"], default="local")
    pi.set_defaults(func=_cmd_init)

    pu = sub.add_parser("uninstall", help="remove Gadfly's hooks (leaves your own)")
    pu.add_argument("scope", nargs="?", choices=["local", "global"], default="local")
    pu.set_defaults(func=_cmd_uninstall)

    sub.add_parser("status", help="check the install is live").set_defaults(func=_cmd_status)
    sub.add_parser("disable", help="pause Gadfly here (hooks stay wired)").set_defaults(func=_cmd_disable)
    sub.add_parser("enable", help="resume Gadfly here").set_defaults(func=_cmd_enable)

    pc = sub.add_parser("config", help="show the config, get one key, or set `key value`")
    pc.add_argument("key", nargs="?")
    pc.add_argument("value", nargs="?")
    pc.set_defaults(func=_cmd_config)

    sub.add_parser("version", help="print the version").set_defaults(func=_cmd_version)

    ph = sub.add_parser("hook", help=argparse.SUPPRESS)  # internal: run a hook
    ph.add_argument("event", choices=_HOOK_EVENTS)
    ph.set_defaults(func=_cmd_hook)

    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
