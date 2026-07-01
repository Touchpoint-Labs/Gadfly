"""The session store: builds the unified file (convo + gate) and slices the tail."""
from gadfly.contracts import (ActionType, ConvoEntry, Decision, InterventionEvent,
                              NormalizedAction, Verdict)
from gadfly.state.session import SessionStore


def _event(session="s1"):
    return InterventionEvent(
        unit=[NormalizedAction(type=ActionType.EDIT, target="/a.py", payload={"old": "x", "new": "y"})],
        workspace="/repo", session=session,
    )


def test_append_convo_dedups(tmp_path):
    s = SessionStore(tmp_path / ".gadfly")
    msgs = [ConvoEntry("user", "text", "do it"), ConvoEntry("assistant", "text", "ok")]
    s.append_convo("s1", msgs)
    s.append_convo("s1", msgs)  # same turn re-seen across gates → no duplication
    convo = [r for r in s.records("s1") if r["t"] == "convo"]
    assert len(convo) == 2 and convo[0]["text"] == "do it"


def test_append_gate_records_actions_and_verdicts(tmp_path):
    s = SessionStore(tmp_path / ".gadfly")
    s.append_gate(_event(), [Verdict(decision=Decision.DENY, note="bug")], ts="2026-06-09T00:00:00+00:00")
    gates = [r for r in s.records("s1") if r["t"] == "gate"]
    assert len(gates) == 1
    g = gates[0]
    assert g["ts"] == "2026-06-09T00:00:00+00:00"
    assert g["actions"][0]["type"] == "edit"
    assert g["verdicts"][0]["decision"] == "deny" and g["verdicts"][0]["note"] == "bug"


def test_tail_is_convo_only_by_default(tmp_path):
    s = SessionStore(tmp_path / ".gadfly")
    s.append_convo("s1", [ConvoEntry("user", "text", "build it")])
    s.append_gate(_event(), [Verdict(decision=Decision.DENY, note="drift")])
    tail = s.tail("s1")  # code reviewer view: convo only
    assert all(r["t"] == "convo" for r in tail) and tail[0]["text"] == "build it"


def test_tail_with_rulings_includes_notes_skips_silent_allows(tmp_path):
    s = SessionStore(tmp_path / ".gadfly")
    s.append_convo("s1", [ConvoEntry("user", "text", "build it")])
    s.append_gate(_event(), [Verdict(decision=Decision.ALLOW)])               # silent — omitted
    s.append_gate(_event(), [Verdict(decision=Decision.DENY, note="drift")])  # a ruling — included
    gates = [r for r in s.tail("s1", include_rulings=True) if r["t"] == "gate"]
    assert len(gates) == 1 and gates[0]["verdicts"][0]["note"] == "drift"


def test_tail_budget_drops_oldest_keeps_recent_whole(tmp_path):
    s = SessionStore(tmp_path / ".gadfly")
    s.append_convo("s1", [ConvoEntry("assistant", "text", "A" * 50),
                          ConvoEntry("assistant", "text", "B" * 50),
                          ConvoEntry("assistant", "text", "C")])
    texts = [r["text"] for r in s.tail("s1", max_chars=10)]
    assert "C" in texts and ("A" * 50) not in texts  # oldest dropped; recent kept whole


def test_missing_session_is_empty(tmp_path):
    s = SessionStore(tmp_path / ".gadfly")
    assert s.records("never") == [] and s.tail("never") == []
