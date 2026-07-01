"""Deterministic router — free, no LLM.

Routes each action to the supervisors, or auto-allows, using only reliable
signals. Judging significance and safety is the supervisors' job, not regex.

Asymmetric defaults, by base rate:
  - edits    → review by default; skip only docs/notebooks; tests → code only;
               every other code change → code + architect.
  - commands → routine fast-path → allow; everything else → SAFETY triage
               (a cheap check in the core that escalates to the architect).

The safe-command lists are built-in; doc/test routing and the cover-for-other
survivor honor config (auto_allow_docs, test_review, disable_code_reviewer/architect).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .contracts import ActionType, Decision, NormalizedAction, Verdict

CODE = "code"
ARCHITECT = "architect"
SAFETY = "safety"  # Haiku command triage (in the core); escalates to ARCHITECT when flagged


@dataclass
class Route:
    reason: str
    terminal: Optional[Verdict] = None
    supervisors: frozenset[str] = field(default_factory=frozenset)


def _allow(reason: str) -> Route:
    return Route(reason=reason, terminal=Verdict(decision=Decision.ALLOW))


def _deny(note: str) -> Route:
    return Route(reason="managed doc — builder edit blocked",
                 terminal=Verdict(decision=Decision.DENY, note=note))


def _route_to(supervisors: set[str], reason: str) -> Route:
    return Route(reason=reason, supervisors=frozenset(supervisors))


# --- edits: review by default ------------------------------------------------

_DOC_SUFFIXES = (".md", ".rst", ".txt")  # prose docs — gated by auto_allow_docs
_SKIP_SUFFIXES = (".ipynb",)  # notebooks — always skipped (no useful review)
_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]*$"),
    re.compile(r"_test\.[^/]+$"),
    re.compile(r"\.spec\.[^/]+$"),
)


def _is_doc(target: Optional[str]) -> bool:
    return bool(target) and target.lower().endswith(_DOC_SUFFIXES)


def _is_skip(target: Optional[str]) -> bool:
    return bool(target) and target.lower().endswith(_SKIP_SUFFIXES)


# Human-/harness-owned memory the builder never edits directly: spec.md and
# decisions.md are written by the harness (the architect records decisions; human-
# accepted ones promote to spec); claude.md is the human's enforced rules. codemap.md
# (builder-owned) and memory.md (architect-maintained) are deliberately NOT here.
_MANAGED_DOCS = ("spec.md", "claude.md", "decisions.md")


def _is_managed_doc(target: Optional[str]) -> bool:
    return bool(target) and target.replace("\\", "/").rsplit("/", 1)[-1].lower() in _MANAGED_DOCS


def _is_test(target: Optional[str]) -> bool:
    return bool(target) and any(p.search(target) for p in _TEST_PATTERNS)


# --- commands: routine fast-path, else safety triage -------------------------

_SAFE_PROGRAMS = {  # read-only / inert: no write or exec mode under any flag
    "ls", "pwd", "cat", "head", "tail", "echo", "printf", "wc",
    "grep", "egrep", "fgrep", "rg", "cut", "tr", "diff", "comm", "cmp",
    "basename", "dirname", "realpath", "stat", "file", "tree",
    "date", "whoami", "id", "uname", "hostname", "arch", "tty", "groups",
    "which", "type", "df", "du", "ps", "free", "uptime",
    "seq", "sleep", "true", "test", "nl", "tac", "rev", "strings",
    "md5sum", "sha1sum", "sha256sum", "printenv", "less", "more",
}
_SAFE_GIT = {"status", "log", "diff", "show", "branch", "remote"}

# always-triage: substitution, redirect, subshell, var-expansion, line-continuation
_HARD_DISQUALIFY = re.compile(r"[$`<>(){}\\]")
_BACKGROUND = re.compile(r"(?<!&)&(?!&)")  # a lone & (not &&)


def _segment_is_safe(segment: str) -> bool:
    tokens = segment.split()
    if not tokens:
        return False
    if tokens[0] in _SAFE_PROGRAMS:
        return True
    return tokens[0] == "git" and len(tokens) > 1 and tokens[1] in _SAFE_GIT


def _is_routine_command(cmd: str) -> bool:
    if _HARD_DISQUALIFY.search(cmd) or _BACKGROUND.search(cmd):
        return False
    # only && || ; | remain as composition — every segment must be safe
    segments = re.split(r"[;|]", cmd.replace("&&", ";").replace("||", ";"))
    segments = [s.strip() for s in segments if s.strip()]
    return bool(segments) and all(_segment_is_safe(s) for s in segments)


# --- entry point -------------------------------------------------------------

def route(action: NormalizedAction, *, auto_allow_docs: bool = True,
          test_review: str = "code", code_enabled: bool = True,
          architect_enabled: bool = True) -> Route:
    t = action.type

    if t in (ActionType.READ, ActionType.SEARCH, ActionType.FETCH, ActionType.META):
        return _allow(f"non-mutating ({t.value})")

    enabled = {s for s, on in ((CODE, code_enabled), (ARCHITECT, architect_enabled)) if on}

    def _to(desired: set[str], reason: str) -> Route:
        # cover-for-other: when every desired reviewer is disabled, the lone survivor
        # (running its solo prompt) covers the gap; with neither, nothing reviews.
        actual = desired & enabled
        if actual:
            return _route_to(actual, reason)
        return _route_to(enabled, reason + " — survivor covers") if enabled else _allow(reason)

    if t in (ActionType.EDIT, ActionType.CREATE):
        if _is_managed_doc(action.target):  # before _is_doc: Gadfly-owned files stay blocked
            return _deny("spec.md, claude.md, and decisions.md are maintained by Gadfly "
                         "and the human, not the builder — describe the change in the "
                         "conversation instead of editing the file directly.")
        if _is_skip(action.target):
            return _allow("notebook — skipped")
        if _is_doc(action.target):
            # docs go to the architect only; auto-allowed by default or when no architect runs
            if auto_allow_docs or not architect_enabled:
                return _allow("doc — skipped")
            return _route_to({ARCHITECT}, "doc edit — architect review")
        if _is_test(action.target):
            if test_review == "off":
                return _allow("test edit — auto-allowed")
            return _to({CODE} if test_review == "code" else {CODE, ARCHITECT},
                       f"test edit — {test_review}")
        return _to({CODE, ARCHITECT}, "code edit")

    if t is ActionType.EXEC:
        if _is_routine_command(action.payload.get("command", "")):
            return _allow("routine command")
        return _route_to({SAFETY}, "command — safety triage")

    return _to({CODE, ARCHITECT}, f"unknown action type ({t}) — conservative review")
