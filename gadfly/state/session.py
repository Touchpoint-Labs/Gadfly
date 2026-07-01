"""Gadfly-owned unified session file, per session — the single normalized record
the core builds and reads from.

Append-only JSONL at .gadfly/sessions/<session>.jsonl, two record kinds:
  - "convo": one whole conversation message (the builder's reasoning), deduped so
    re-seeing a turn across gates doesn't duplicate it.
  - "gate":  one reviewed unit — the normalized actions + their index-aligned
    verdicts. The builder's tool calls live here, paired with Gadfly's rulings.

The supervisors never read the native transcript; they read the recent `tail` of
THIS file under one budget. The architect includes its prior rulings (so it sees
its own notes naturally, interleaved with the conversation); the code reviewer gets
convo only. The tail is whole records — nothing is ever cut mid-message.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..contracts import ConvoEntry, InterventionEvent, Verdict

# One decent budget for the recent slice a supervisor sees. Config later.
BUDGET_CHARS = 24000


def _informative(gate: dict) -> bool:
    """A gate worth showing as a ruling — it said something (note / block / asked).
    Silent allows are not rulings, so they don't clutter the architect's trajectory."""
    return any(v.get("note") or v.get("undiscussed") or v.get("decision") not in (None, "allow")
               for v in gate.get("verdicts", []))


def _weight(record: dict) -> int:
    if record.get("t") == "convo":
        return len(record.get("text", ""))
    return sum(len(v.get("note") or "") for v in record.get("verdicts", [])) + 40


class SessionStore:
    def __init__(self, gadfly_dir: Path):
        self.gadfly_dir = Path(gadfly_dir)
        self._sessions = self.gadfly_dir / "sessions"

    def _path(self, session: str) -> Path:
        return self._sessions / f"{session}.jsonl"

    def _read(self, session: str) -> list[dict]:
        path = self._path(session)
        if not path.exists():
            return []
        out: list[dict] = []
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn line (concurrent append / killed hook) loses one record, not the store
        return out

    def _append(self, session: str, record: dict) -> None:
        self._sessions.mkdir(parents=True, exist_ok=True)
        with self._path(session).open("a") as f:
            f.write(json.dumps(record) + "\n")

    # --- writes (the core builds the session) --------------------------------

    def append_convo(self, session: str, entries: list[ConvoEntry]) -> None:
        """Append the turn's conversation, skipping any message already stored —
        gates within one turn re-see the same reasoning, but it lands only once."""
        seen = {(r["role"], r["kind"], r["text"]) for r in self._read(session) if r.get("t") == "convo"}
        for e in entries:
            key = (e.role, e.kind, e.text)
            if key in seen:
                continue
            seen.add(key)
            self._append(session, {"t": "convo", "role": e.role, "kind": e.kind, "text": e.text})

    def append_gate(self, event: InterventionEvent, verdicts: list[Verdict],
                    ts: Optional[str] = None) -> None:
        """Append a reviewed unit: the actions + their index-aligned verdicts."""
        self._append(event.session, {
            "t": "gate",
            "ts": ts or datetime.now(timezone.utc).isoformat(),
            "session": event.session,
            "workspace": event.workspace,
            "actions": [asdict(a) for a in event.unit],
            "verdicts": [asdict(v) for v in verdicts],
        })

    # --- read (the recent slice a supervisor sees) ---------------------------

    def tail(self, session: str, *, include_rulings: bool = False,
             max_chars: int = BUDGET_CHARS) -> list[dict]:
        """Recent whole records, chronological, under one budget. Convo always; the
        architect's prior rulings too when include_rulings (silent allows omitted).
        The code reviewer calls it convo-only; nothing is cut mid-record."""
        stream = [r for r in self._read(session)
                  if (r.get("t") == "convo" and r.get("kind") != "thinking")
                  or (include_rulings and r.get("t") == "gate" and _informative(r))]
        out: list[dict] = []
        used = 0
        for r in reversed(stream):
            out.append(r)
            used += _weight(r)
            if used >= max_chars:
                break
        return list(reversed(out))

    def records(self, session: str) -> list[dict]:
        return self._read(session)
