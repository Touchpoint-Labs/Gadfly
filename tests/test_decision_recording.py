"""Core applies the architect's ledger ops from verdicts. The emission discipline
(add only on an allowing verdict, etc.) lives in the architect's PROMPT, not in
code coercion — the harness applies what the supervisor states."""
from gadfly.contracts import (ActionType, Decision, DecisionOp, InterventionEvent,
                              NormalizedAction, ScopeRef, Verdict)
from gadfly.core import Reviewers, merge, review
from gadfly.state.session import SessionStore


def _reviewers(architect_verdict):
    return Reviewers(
        code=lambda e, actions: [Verdict(decision=Decision.ALLOW) for _ in actions],
        architect=lambda e, actions: [architect_verdict for _ in actions],
        safety_triage=lambda e, a: False,
    )


def _event(tmp_path):
    action = NormalizedAction(ActionType.EDIT, target="src/auth.py",
                              payload={"old": "a", "new": "b"})
    return InterventionEvent(unit=[action], workspace=str(tmp_path), session="s1")


def _add(what, **kw):
    return DecisionOp(op="add", what=what, why="because",
                      scope=[ScopeRef(file="src/auth.py", symbol="login")], **kw)


def test_add_on_allow_writes_ledger(tmp_path):
    store = SessionStore(tmp_path / ".gadfly")
    v = Verdict(decision=Decision.ALLOW, ops=[_add("JWT is the auth scheme")])
    review(_event(tmp_path), _reviewers(v), store)
    text = (tmp_path / "decisions.md").read_text()
    assert "D1 · active · JWT is the auth scheme" in text
    assert "scope: src/auth.py#login" in text


def test_human_accepted_promotes_to_spec(tmp_path):
    store = SessionStore(tmp_path / ".gadfly")
    v = Verdict(decision=Decision.ALLOW, ops=[_add("sessions over JWT", human_accepted=True)])
    review(_event(tmp_path), _reviewers(v), store)
    assert "sessions over JWT" in (tmp_path / "spec.md").read_text()
    assert "[spec]" in (tmp_path / "decisions.md").read_text()


def test_housekeeping_applies_even_on_deny(tmp_path):
    store = SessionStore(tmp_path / ".gadfly")
    allow = Verdict(decision=Decision.ALLOW, ops=[_add("stale entry")])
    review(_event(tmp_path), _reviewers(allow), store)
    deny = Verdict(decision=Decision.DENY, note="no",
                   ops=[DecisionOp(op="retire", id=1, reason="overturned")])
    review(_event(tmp_path), _reviewers(deny), store)
    assert "retired — overturned" in (tmp_path / "decisions.md").read_text()


def test_add_dedupes_across_repeat_reviews(tmp_path):
    store = SessionStore(tmp_path / ".gadfly")
    v = Verdict(decision=Decision.ALLOW, ops=[_add("one decision")])
    review(_event(tmp_path), _reviewers(v), store)
    review(_event(tmp_path), _reviewers(v), store)
    assert (tmp_path / "decisions.md").read_text().count("one decision") == 1


def test_no_ops_writes_nothing(tmp_path):
    store = SessionStore(tmp_path / ".gadfly")
    review(_event(tmp_path), _reviewers(Verdict(decision=Decision.ALLOW)), store)
    assert not (tmp_path / "decisions.md").exists()


def test_merge_carries_ops_from_either_reviewer():
    op = _add("X")
    m = merge([Verdict(decision=Decision.ALLOW),
               Verdict(decision=Decision.ALLOW_WITH_NOTE, note="n", ops=[op])])
    assert m.ops == [op]
