"""Core review(): routing → batched reviewer calls → merge → record (reviewers
are fakes). The key plan-B property: a parallel batch is ONE code call + ONE
architect call over the grouped actions, not N of each."""

from gadfly.contracts import (
    ActionType,
    ConvoEntry,
    Decision,
    InterventionEvent,
    NormalizedAction,
    UndiscussedDecision,
    Verdict,
)
from gadfly.core import Reviewers, merge, review
from gadfly.state.session import SessionStore


def ev(*actions):
    return InterventionEvent(unit=list(actions), workspace="/r", session="s")


def read():
    return NormalizedAction(type=ActionType.READ, target="/r/x")


def edit(path="/r/app.py"):
    return NormalizedAction(
        type=ActionType.EDIT, target=path, payload={"old": "a", "new": "b"}
    )


def cmd(c):
    return NormalizedAction(type=ActionType.EXEC, payload={"command": c})


def reviewers(
    code=Verdict(Decision.ALLOW), arch=Verdict(Decision.ALLOW), dangerous=False
):
    calls = {"code": 0, "arch": 0, "triage": 0}

    def c(e, actions):  # one call returns one verdict PER action
        calls["code"] += 1
        return [code] * len(actions)

    def ar(e, actions):
        calls["arch"] += 1
        return [arch] * len(actions)

    def tr(e, a):  # triage stays per-command
        calls["triage"] += 1
        return dangerous

    return Reviewers(code=c, architect=ar, safety_triage=tr), calls


# --- merge -------------------------------------------------------------------


def test_merge_disagreement_wins():
    assert (
        merge([Verdict(Decision.ALLOW), Verdict(Decision.DENY, note="no")]).decision
        is Decision.DENY
    )


def test_merge_precedence_note_then_ask():
    assert (
        merge(
            [Verdict(Decision.ALLOW), Verdict(Decision.ALLOW_WITH_NOTE, note="fyi")]
        ).decision
        is Decision.ALLOW_WITH_NOTE
    )
    assert (
        merge(
            [Verdict(Decision.ALLOW_WITH_NOTE, note="fyi"), Verdict(Decision.ASK)]
        ).decision
        is Decision.ASK
    )


def test_merge_abstain_is_lowest():
    assert (
        merge([Verdict(Decision.ALLOW), Verdict(Decision.ABSTAIN)]).decision
        is Decision.ALLOW
    )
    assert (
        merge([Verdict(Decision.DENY, note="x"), Verdict(Decision.ABSTAIN)]).decision
        is Decision.DENY
    )
    assert (
        merge([Verdict(Decision.ABSTAIN), Verdict(Decision.ABSTAIN)]).decision
        is Decision.ABSTAIN
    )
    assert (
        merge([]).decision is Decision.ABSTAIN
    )  # nothing reviewed → step aside, not silent-allow


def test_merge_combines_notes_and_keeps_undiscussed():
    u = UndiscussedDecision(question="?")
    m = merge(
        [
            Verdict(Decision.DENY, note="a"),
            Verdict(Decision.ALLOW_WITH_NOTE, note="b", undiscussed=u),
        ]
    )
    assert m.decision is Decision.DENY and m.note == "a | b" and m.undiscussed is u


# --- review: single action ---------------------------------------------------


def test_read_terminal_allow_calls_no_reviewers(tmp_path):
    revs, calls = reviewers()
    out = review(ev(read()), revs, SessionStore(tmp_path / ".g"))
    assert out[0].decision is Decision.ALLOW
    assert calls == {"code": 0, "arch": 0, "triage": 0}


def test_code_edit_runs_both_and_merges(tmp_path):
    revs, calls = reviewers(
        code=Verdict(Decision.ALLOW), arch=Verdict(Decision.DENY, note="drift")
    )
    out = review(ev(edit()), revs, SessionStore(tmp_path / ".g"))
    assert out[0].decision is Decision.DENY
    assert calls["code"] == 1 and calls["arch"] == 1


def test_routine_command_skips_triage(tmp_path):
    revs, calls = reviewers()
    out = review(ev(cmd("ls -la")), revs, SessionStore(tmp_path / ".g"))
    assert out[0].decision is Decision.ALLOW and calls["triage"] == 0


def test_dangerous_command_triage_clears(tmp_path):
    revs, calls = reviewers(dangerous=False)
    out = review(ev(cmd("rm foo")), revs, SessionStore(tmp_path / ".g"))
    assert out[0].decision is Decision.ALLOW
    assert calls["triage"] == 1 and calls["arch"] == 0  # cleared; architect not woken


def test_dangerous_command_triage_escalates_to_architect(tmp_path):
    revs, calls = reviewers(
        dangerous=True, arch=Verdict(Decision.ASK, note="confirm rm")
    )
    out = review(ev(cmd("rm foo")), revs, SessionStore(tmp_path / ".g"))
    assert out[0].decision is Decision.ASK
    assert calls["triage"] == 1 and calls["arch"] == 1


# --- cover-for-other: a disabled reviewer is None ----------------------------


def test_disabled_architect_reviews_edit_with_code_only(tmp_path):
    called = {"code": 0}

    def c(e, actions):
        called["code"] += 1
        return [Verdict(Decision.ALLOW)] * len(actions)

    revs = Reviewers(code=c, architect=None, safety_triage=lambda e, a: False)
    out = review(ev(edit()), revs, SessionStore(tmp_path / ".g"))
    assert out[0].decision is Decision.ALLOW and called["code"] == 1  # None never called


def test_safety_escalation_falls_to_code_when_architect_disabled(tmp_path):
    called = {"code": 0}

    def c(e, actions):
        called["code"] += 1
        return [Verdict(Decision.DENY, note="risky")] * len(actions)

    revs = Reviewers(code=c, architect=None, safety_triage=lambda e, a: True)
    out = review(ev(cmd("rm foo")), revs, SessionStore(tmp_path / ".g"))
    assert out[0].decision is Decision.DENY and called["code"] == 1  # survivor covers


# --- review: batch (plan B) --------------------------------------------------


def test_batch_of_edits_is_one_call_per_reviewer(tmp_path):
    revs, calls = reviewers(arch=Verdict(Decision.ALLOW_WITH_NOTE, note="heads up"))
    out = review(
        ev(edit("/r/a.py"), edit("/r/b.py"), edit("/r/c.py")),
        revs,
        SessionStore(tmp_path / ".g"),
    )
    assert len(out) == 3 and all(v.decision is Decision.ALLOW_WITH_NOTE for v in out)
    assert (
        calls["code"] == 1 and calls["arch"] == 1
    )  # ONE call each for the whole batch


def test_batch_mixed_routing_groups_correctly(tmp_path):
    # edit → code+arch; routine cmd → terminal; dangerous cmd → triage → arch
    revs, calls = reviewers(dangerous=True, arch=Verdict(Decision.DENY, note="no"))
    out = review(
        ev(edit(), cmd("ls"), cmd("rm x")), revs, SessionStore(tmp_path / ".g")
    )
    assert out[1].decision is Decision.ALLOW  # routine cmd terminal
    assert out[0].decision is Decision.DENY and out[2].decision is Decision.DENY
    assert calls["code"] == 1  # one code call (the edit)
    assert calls["arch"] == 1  # one arch call (edit + flagged rm)
    assert calls["triage"] == 1  # only the non-routine command


def test_review_records_gate_to_session(tmp_path):
    revs, _ = reviewers()
    store = SessionStore(tmp_path / ".g")
    review(ev(edit(), read()), revs, store)
    gates = [r for r in store.records("s") if r["t"] == "gate"]
    assert len(gates) == 1 and len(gates[0]["verdicts"]) == 2


# --- deadlock escalation -------------------------------------------------------


def test_deadlock_surfaces_after_cap(tmp_path):
    revs, calls = reviewers(
        code=Verdict(Decision.DENY, note="bug"),
        arch=Verdict(Decision.DENY, note="drift"),
    )
    store = SessionStore(tmp_path / ".g")
    for _ in range(3):
        assert review(ev(edit()), revs, store)[0].decision is Decision.DENY
    out = review(ev(edit()), revs, store)  # 4th identical attempt
    assert out[0].decision is Decision.ASK
    assert (
        "bug" in out[0].undiscussed.question and "drift" in out[0].undiscussed.question
    )
    assert calls["code"] == 3  # the 4th never reached a reviewer


def test_deadlock_surfaces_once_then_defers_to_review(tmp_path):
    # after surfacing the deadlock once, Gadfly stops short-circuiting: the retry falls back
    # to normal review, where the supervisors read the user's actual answer in the convo
    # (a blind allow would ignore a 'hold' answer and invert the surface's "stays paused")
    revs, calls = reviewers(code=Verdict(Decision.DENY, note="bug"))
    store = SessionStore(tmp_path / ".g")
    for _ in range(3):
        assert review(ev(edit()), revs, store)[0].decision is Decision.DENY
    assert review(ev(edit()), revs, store)[0].decision is Decision.ASK   # 4th: surface once
    assert calls["code"] == 3                                            # surface didn't review
    assert review(ev(edit()), revs, store)[0].decision is Decision.DENY  # 5th: back to review
    assert calls["code"] == 4                                            # reviewer consulted again


def test_no_deadlock_across_different_actions(tmp_path):
    revs, calls = reviewers(code=Verdict(Decision.DENY, note="bug"))
    store = SessionStore(tmp_path / ".g")
    for i in range(4):
        out = review(ev(edit(f"/r/f{i}.py")), revs, store)
        assert out[0].decision is Decision.DENY  # distinct actions: never escalates
    assert calls["code"] == 4


# --- digest compaction is not on the review hot path -------------------------


def test_review_does_not_compact_convo_on_hot_path(tmp_path):
    base, _ = reviewers()
    revs = Reviewers(
        code=base.code,
        architect=base.architect,
        safety_triage=base.safety_triage,
    )
    store = SessionStore(tmp_path / ".g")
    msgs = [
        ConvoEntry("user", "text", f"turn {i} " + "x" * 12000) for i in range(4)
    ]  # > 40k, distinct
    review(
        InterventionEvent(
            unit=[edit()], workspace=str(tmp_path), session="s", messages=msgs
        ),
        revs,
        store,
    )
    assert not (store.gadfly_dir / "digests" / "s.md").exists()
