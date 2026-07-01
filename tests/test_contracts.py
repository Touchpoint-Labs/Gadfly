"""Contracts construct correctly and serialize cleanly for the session file."""
import json
from dataclasses import asdict

from gadfly.contracts import (
    ActionType,
    ConvoEntry,
    Decision,
    InterventionEvent,
    NormalizedAction,
    UndiscussedDecision,
    Verdict,
)


def test_str_enums_serialize_to_values():
    assert ActionType.EDIT == "edit"
    assert Decision.ASK == "ask"
    assert json.dumps({"t": ActionType.FETCH}) == '{"t": "fetch"}'


def test_event_roundtrips_through_json():
    ev = InterventionEvent(
        unit=[
            NormalizedAction(type=ActionType.CREATE, target="/repo/n.py", payload={"content": "x"}),
            NormalizedAction(type=ActionType.EXEC, payload={"command": "pytest"}, raw={"tool_name": "Bash"}),
        ],
        workspace="/repo",
        session="s1",
        messages=[ConvoEntry("user", "text", "add module and run tests")],
    )
    d = json.loads(json.dumps(asdict(ev)))
    assert d["unit"][0]["type"] == "create"
    assert d["unit"][1]["type"] == "exec"
    assert d["messages"][0]["text"] == "add module and run tests"
    assert d["session"] == "s1"


def test_verdict_with_undiscussed_serializes():
    v = Verdict(
        decision=Decision.ASK,
        note="spec is silent on auth",
        undiscussed=UndiscussedDecision(question="JWT or sessions? (hard to reverse later)"),
    )
    d = json.loads(json.dumps(asdict(v)))
    assert d["decision"] == "ask"
    assert d["undiscussed"]["question"].startswith("JWT")


def test_minimal_allow_verdict():
    d = json.loads(json.dumps(asdict(Verdict(decision=Decision.ALLOW))))
    assert d == {"decision": "allow", "note": None, "undiscussed": None, "ops": []}
