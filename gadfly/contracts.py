"""Neutral contracts — the agent- and LLM-agnostic spine.

An adapter maps an agent's native tool call into a `NormalizedAction` and bundles
the unit being reviewed into an `InterventionEvent`. The core returns a `Verdict`
per action. Nothing agent-specific (hook JSON, tool names, transcript formats)
lives here — adapters translate to and from these types.

These are plain dataclasses with str-valued enums, so `dataclasses.asdict` +
`json.dumps` serialize cleanly into the per-session event log.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ActionType(str, Enum):
    """Effect-class an adapter maps each agent tool into (not a 1:1 tool map).

    Routing-relevant buckets: read/search/fetch are non-mutating (auto-allow);
    edit/create/exec are consequential (→ supervisors); meta is agent-internal.
    delete/move are surfaced inside `exec` via command analysis for v1.
    """

    READ = "read"        # local file read (Read, NotebookRead)
    SEARCH = "search"    # local code search (Grep, Glob)
    FETCH = "fetch"      # web/network read (WebSearch, WebFetch) — external content
    EDIT = "edit"        # modify existing file (Edit, MultiEdit, Write-to-existing)
    CREATE = "create"    # new file (Write-to-new-path)
    EXEC = "exec"        # run a command (Bash)
    META = "meta"        # agent-internal (TodoWrite, Task, ExitPlanMode, ...)


class Decision(str, Enum):
    ALLOW = "allow"                    # silent pass
    ALLOW_WITH_NOTE = "allow_with_note"  # pass, but surface a note to the builder
    DENY = "deny"                      # block; reason fed back to the builder
    ASK = "ask"                        # surface to the human
    ABSTAIN = "abstain"                # the harness couldn't get a verdict → step aside,
                                       # the host's own permission flow decides (never vouch)


@dataclass
class NormalizedAction:
    """One agent tool call, normalized to its effect-class."""

    type: ActionType
    target: Optional[str] = None              # path / url / None
    payload: dict[str, Any] = field(default_factory=dict)  # {old,new} | {content} | {command} | ...
    raw: dict[str, Any] = field(default_factory=dict)      # original {tool_name, tool_input}, for the adapter/debug


@dataclass
class ConvoEntry:
    """One whole conversation message from a turn — never truncated. The adapter
    sources these from the agent's transcript; the core stores them in the session
    file and slices the convo a supervisor sees from whole entries like these."""

    role: str   # "user" | "assistant"
    kind: str   # "text" | "thinking"
    text: str


@dataclass
class InterventionEvent:
    """What an adapter hands the core: the unit under review + minimal context.

    The core enriches this with its own state (spec/codemap/decisions/trajectory);
    those are NOT carried here. `unit` is one action or a parallel batch.
    """

    unit: list[NormalizedAction]
    workspace: str
    session: str
    messages: list[ConvoEntry] = field(default_factory=list)  # the turn's conversation, complete; the core stores it and slices the convo a supervisor sees


@dataclass
class UndiscussedDecision:
    """A consequential decision the spec doesn't settle — log it or surface it.
    The question carries its own stakes (why it matters, reversibility) as prose;
    `options` are the architect's neutral phrasings of the real alternatives (the
    builder relays them verbatim — it never frames its own choice's menu)."""

    question: str
    options: list[str] = field(default_factory=list)


@dataclass
class ScopeRef:
    """Where a decision lives in the code: a file path (the stable retrieval key)
    plus an optional symbol — function/class/section name. A symbol that disappears
    is the staleness signal (re-examine), never an automatic action."""

    file: str
    symbol: str = ""


@dataclass
class DecisionOp:
    """One architect-directed change to decisions.md. The architect (read-only)
    states ops in its verdict; the harness applies them as stated. The discipline —
    `add`/`revise` only when an allowing verdict settles something; `retire`/
    `delete` as housekeeping any time — is the architect's own, held by its prompt,
    not coerced in code. On `add`, entries named in `supersedes` are flipped
    deterministically by the harness."""

    op: str                                   # add | revise | retire | delete
    id: int = 0                               # target entry (revise/retire/delete)
    what: str = ""
    why: str = ""
    scope: list[ScopeRef] = field(default_factory=list)
    supersedes: list[int] = field(default_factory=list)  # add: entries this replaces
    human_accepted: bool = False              # add: user visibly decided → promote to spec.md
    reason: str = ""                          # retire: kept in the tombstone; delete: log only


@dataclass
class Verdict:
    """The core's decision for a single action."""

    decision: Decision
    note: Optional[str] = None
    undiscussed: Optional[UndiscussedDecision] = None
    ops: list[DecisionOp] = field(default_factory=list)  # architect-only ledger maintenance
