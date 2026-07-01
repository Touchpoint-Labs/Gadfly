"""The decisions ledger — the living record of the project's load-bearing decisions.

decisions.md is architect-managed: the architect states ops (add / revise / retire /
delete) in its verdict and the harness applies them here — supervisors never write.
Lean format, one entry per decision:

    D12 · active · one-line what  [spec]
      why: one line
      scope: gadfly/core.py#_review_unit, gadfly/router.py

    D7 · superseded by D8 · one-line what
    D3 · retired — reason · one-line what

Active entries carry why + scope (file, or file#symbol — the anchor); non-active
entries collapse to a single tombstone line. `[spec]` marks a human-accepted
decision promoted into spec.md. The file is uncapped — the injected slice (recent N
plus entries whose scope files overlap the current change) is the bound, and
reconciliation is lazy: the architect fixes what its slice surfaces.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..contracts import DecisionOp

_HEAD_RE = re.compile(r"^D(\d+) · ")


def _one_line(s: str) -> str:
    return " ".join(s.split())


@dataclass
class Decision:
    id: int
    what: str
    why: str = ""
    scope: list[str] = field(default_factory=list)  # "file" or "file#symbol"
    status: str = "active"        # active | superseded by D<n> | retired — <reason>
    promoted: bool = False        # human-accepted, written into spec.md → [spec]

    @property
    def active(self) -> bool:
        return self.status == "active"


def _render(d: Decision) -> str:
    head = f"D{d.id} · {d.status} · {_one_line(d.what)}"
    if d.promoted:
        head += "  [spec]"
    if not d.active:
        return head  # tombstone: one line, no why/scope
    lines = [head]
    if d.why:
        lines.append(f"  why: {_one_line(d.why)}")
    if d.scope:
        lines.append(f"  scope: {', '.join(d.scope)}")
    return "\n".join(lines)


def _parse(text: str) -> list[Decision]:
    decisions: list[Decision] = []
    cur: Optional[Decision] = None
    for line in text.splitlines():
        m = _HEAD_RE.match(line)
        if m:
            parts = line.split(" · ", 2)
            if len(parts) != 3:
                continue
            what = parts[2]
            promoted = what.rstrip().endswith("[spec]")
            if promoted:
                what = what.rstrip()[: -len("[spec]")].rstrip()
            cur = Decision(id=int(m.group(1)), what=what, status=parts[1].strip(),
                           promoted=promoted)
            decisions.append(cur)
        elif cur is not None and cur.active:
            s = line.strip()
            if s.startswith("why:"):
                cur.why = s[len("why:"):].strip()
            elif s.startswith("scope:"):
                cur.scope = [t.strip() for t in s[len("scope:"):].split(",") if t.strip()]
    return decisions


def _promote_to_spec(spec_path: Path, d: Decision) -> None:
    """Append a human-accepted decision into spec.md as part of the standard.
    spec.md is human-owned; this is the one thing the AI writes to it."""
    header = "## Accepted decisions"
    bullet = f"- D{d.id}: {_one_line(d.what)} — {_one_line(d.why)}"
    text = spec_path.read_text() if spec_path.is_file() else ""
    if header in text:
        body = text.rstrip() + f"\n{bullet}\n"
    else:
        body = text.rstrip() + f"\n\n{header}\n{bullet}\n"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(body.lstrip("\n"))


def _scope_str(refs) -> list[str]:
    return [f"{r.file}#{r.symbol}" if r.symbol else r.file for r in refs]


class DecisionLedger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def all(self) -> list[Decision]:
        return _parse(self.path.read_text()) if self.path.is_file() else []

    def _write(self, decisions: list[Decision]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("\n\n".join(_render(d) for d in decisions)
                             + ("\n" if decisions else ""))

    def apply(self, ops: list[DecisionOp], spec_path: Path) -> None:
        """Apply the architect's ops. Unknown target ids are skipped (a stale or
        hallucinated id must not crash the gate); an `add` whose `what` matches an
        active entry is skipped (dedupe across the batch's reviewers and retries)."""
        decisions = self.all()
        by_id = {d.id: d for d in decisions}
        for op in ops:
            if op.op == "add":
                if any(d.active and d.what == _one_line(op.what) for d in decisions):
                    continue
                new_id = max(by_id, default=0) + 1
                d = Decision(new_id, op.what, op.why, _scope_str(op.scope))
                decisions.append(d)
                by_id[new_id] = d
                for sid in op.supersedes:
                    old = by_id.get(sid)
                    if old is not None and old.active:
                        old.status, old.why, old.scope = f"superseded by D{new_id}", "", []
                if op.human_accepted:
                    _promote_to_spec(Path(spec_path), d)
                    d.promoted = True
            elif op.op == "revise":
                d = by_id.get(op.id)
                if d is not None:
                    d.what, d.why, d.scope = op.what, op.why, _scope_str(op.scope)
            elif op.op == "retire":
                d = by_id.get(op.id)
                if d is not None and d.active:
                    reason = _one_line(op.reason)
                    d.status = f"retired — {reason}" if reason else "retired"
                    d.why, d.scope = "", []
            elif op.op == "delete":
                d = by_id.pop(op.id, None)
                if d is not None:
                    decisions.remove(d)
        self._write(decisions)

    def slice(self, files: Optional[list[str]] = None, n: int = 12) -> list[Decision]:
        """Whole active entries: the most recent `n`, plus any whose scope files
        overlap the files being changed (suffix match either way, so absolute hook
        paths meet repo-relative anchors)."""
        active = [d for d in self.all() if d.active]
        chosen = {d.id: d for d in active[-n:]}
        targets = [t.replace("\\", "/") for t in (files or []) if t]
        for d in active:
            for s in d.scope:
                f = s.partition("#")[0]
                if any(t.endswith(f) or f.endswith(t) for t in targets):
                    chosen[d.id] = d
                    break
        return sorted(chosen.values(), key=lambda d: d.id)
