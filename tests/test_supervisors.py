"""Supervisors: unit context assembly + verdict-array parsing + the alignment
guarantee (provider mocked)."""

import json

import pytest

from gadfly.context import (
    architect_context,
    architect_system,
    code_context,
    load_prompt,
)
from gadfly.contracts import (
    ActionType,
    ConvoEntry,
    Decision,
    DecisionOp,
    InterventionEvent,
    NormalizedAction,
    ScopeRef,
)
from gadfly.state import digest
from gadfly.state.decisions import DecisionLedger
from gadfly.state.memory import ProjectMemory
from gadfly.state.session import SessionStore
from gadfly.supervisors import (
    ARCHITECT_VERDICT_SCHEMA,
    CODE_VERDICT_SCHEMA,
    make_architect,
    make_code_reviewer,
    make_memory_compactor,
    make_safety_triage,
    parse_verdicts,
)


class _Mock:
    """Returns queued results in order; repeats the last one when exhausted."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def complete(self, *, system, prompt, model, schema=None):
        self.calls.append(
            dict(system=system, prompt=prompt, model=model, schema=schema)
        )
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def _wrap(*verdicts):
    return json.dumps({"verdicts": list(verdicts)})


def _edit(target, old="a", new="b"):
    return NormalizedAction(
        type=ActionType.EDIT, target=target, payload={"old": old, "new": new}
    )


def _event(actions, ws="/w"):
    return InterventionEvent(unit=list(actions), workspace=ws, session="s")


# --- parsing -------------------------------------------------------------------


def test_prompts_load():
    assert "CODE REVIEWER" in load_prompt("code.md")
    assert "ARCHITECT" in load_prompt("architect.md")
    assert "CHARACTER BUDGET" in load_prompt("compaction.md")


def test_memory_compactor_passes_budget_in_prompt():
    mock = _Mock("condensed")
    condense = make_memory_compactor(mock, "m")
    out = condense("hello world", 5000)
    assert out == "condensed"
    assert "CHARACTER LIMIT: 5000" in mock.calls[0]["prompt"]
    assert "current file: 11 characters" in mock.calls[0]["prompt"]


def test_parse_verdicts_full_fields():
    raw = _wrap(
        {
            "decision": "ask",
            "note": "spec silent",
            "undiscussed": {
                "question": "JWT or sessions?",
                "options": ["JWT", "sessions"],
            },
        },
        {
            "decision": "allow",
            "ops": [
                {
                    "op": "add",
                    "what": "X",
                    "why": "y",
                    "scope": [{"file": "a.py", "symbol": "f"}],
                    "supersedes": ["D3", 4],
                    "human_accepted": True,
                },
                {"op": "retire", "id": "D7", "reason": "overturned"},
            ],
        },
    )
    vs = parse_verdicts(raw)
    assert len(vs) == 2
    assert vs[0].decision is Decision.ASK and vs[0].undiscussed.options == [
        "JWT",
        "sessions",
    ]
    add, retire = vs[1].ops
    assert (
        add.what == "X" and add.scope[0].file == "a.py" and add.scope[0].symbol == "f"
    )
    assert add.supersedes == [3, 4] and add.human_accepted  # "D3"/4 both → int
    assert retire.op == "retire" and retire.id == 7 and retire.reason == "overturned"


def test_parse_verdicts_unparseable_and_malformed():
    assert parse_verdicts("not json") == []
    assert parse_verdicts(json.dumps({"nope": 1})) == []
    vs = parse_verdicts(_wrap({"decision": "allow"}, {"decision": "bogus-value"}))
    assert len(vs) == 1  # malformed item dropped → caller sees the count mismatch


# --- alignment guarantee ---------------------------------------------------------


def test_count_matched_batch_is_one_call(tmp_path):
    store = SessionStore(tmp_path / ".gadfly")
    p = _Mock(_wrap({"decision": "allow"}, {"decision": "deny", "note": "bug"}))
    review = make_code_reviewer(p, "m", tmp_path, store)
    actions = [_edit("a.py"), _edit("b.py")]
    vs = review(_event(actions), actions)
    assert len(p.calls) == 1
    assert [v.decision for v in vs] == [Decision.ALLOW, Decision.DENY]


def test_count_mismatch_degrades_to_per_action(tmp_path):
    store = SessionStore(tmp_path / ".gadfly")
    p = _Mock(
        _wrap({"decision": "deny", "note": "only one"}),  # 1 verdict for 2 actions
        _wrap({"decision": "allow"}),  # re-review a.py
        _wrap({"decision": "deny", "note": "bug in b"}),
    )  # re-review b.py
    review = make_code_reviewer(p, "m", tmp_path, store)
    actions = [_edit("a.py"), _edit("b.py")]
    vs = review(_event(actions), actions)
    assert len(p.calls) == 3  # batch call + one per action
    assert vs[0].decision is Decision.ALLOW
    assert vs[1].decision is Decision.DENY and "b" in vs[1].note


def test_lone_action_unparseable_abstains(tmp_path):
    store = SessionStore(tmp_path / ".gadfly")
    review = make_code_reviewer(_Mock("garbage"), "m", tmp_path, store)
    a = _edit("a.py")
    vs = review(_event([a]), [a])
    assert [v.decision for v in vs] == [Decision.ABSTAIN]  # step aside, never vouch


# --- context -------------------------------------------------------------------


def test_code_context_single_has_role_rules_convo_and_file(tmp_path):
    (tmp_path / "claude.md").write_text("RULE: prefer early returns.")
    f = tmp_path / "util.py"
    f.write_text("def avg(xs):\n    return sum(xs)/len(xs)\n")
    mem = ProjectMemory(tmp_path)
    store = SessionStore(tmp_path / ".gadfly")
    store.append_convo("s", [ConvoEntry("user", "text", "simplify avg()")])
    action = _edit(str(f))
    system, user = code_context(mem, store, _event([action], str(tmp_path)), [action])
    assert "CODE REVIEWER" in system and "early returns" in system
    assert "simplify avg()" in user and "# CHANGE" in user and "def avg" in user


def test_code_context_batch_numbers_changes_and_files(tmp_path):
    (tmp_path / "a.py").write_text("A = 1\n")
    (tmp_path / "b.py").write_text("B = 2\n")
    mem = ProjectMemory(tmp_path)
    store = SessionStore(tmp_path / ".gadfly")
    actions = [_edit(str(tmp_path / "a.py")), _edit(str(tmp_path / "b.py"))]
    _, user = code_context(mem, store, _event(actions, str(tmp_path)), actions)
    assert "# CHANGES (2)" in user and "## change 1:" in user and "## change 2:" in user
    assert "# CURRENT FILE — change 1" in user and "# CURRENT FILE — change 2" in user


def test_architect_system_injects_one_mode():
    s = architect_system("balanced")
    assert "{{MODE}}" not in s and "BALANCED mode" in s and "AUTONOMOUS mode" not in s


def test_architect_system_unknown_mode_raises():
    with pytest.raises(ValueError):
        architect_system("nope")


def test_architect_context_caches_stable_keeps_codemap_dynamic(tmp_path):
    (tmp_path / "spec.md").write_text("Goal: a calculator.")
    (tmp_path / "claude.md").write_text("Rule: no globals.")
    (tmp_path / "codemap.md").write_text("calc.py: core ops.")
    (tmp_path / "calc.py").write_text("OPS = {}\n")
    led = DecisionLedger(tmp_path / "decisions.md")
    led.apply(
        [
            DecisionOp(
                op="add",
                what="use Decimal",
                why="precision",
                scope=[ScopeRef(file="calc.py")],
            )
        ],
        tmp_path / "spec.md",
    )
    mem = ProjectMemory(tmp_path)
    store = SessionStore(tmp_path / ".gadfly")
    store.append_convo("s", [ConvoEntry("user", "text", "adding multiply")])
    action = _edit(str(tmp_path / "calc.py"))
    system, user = architect_context(
        mem, led, store, _event([action], str(tmp_path)), [action], "autonomous"
    )
    assert (
        "AUTONOMOUS mode" in system
        and "a calculator" in system
        and "no globals" in system
    )
    assert "core ops" not in system and "core ops" in user  # codemap stays dynamic
    assert (
        "use Decimal" in user and "adding multiply" in user
    )  # decisions (area=calc) + convo
    assert f"# CURRENT FILE ({action.target})" in user  # architect sees the real file


def test_architect_decision_slice_covers_all_batch_targets(tmp_path):
    led = DecisionLedger(tmp_path / "decisions.md")
    led.apply(
        [
            DecisionOp(
                op="add",
                what="auth decision",
                why="w",
                scope=[ScopeRef(file="auth/x.py")],
            ),
            DecisionOp(
                op="add", what="db decision", why="w", scope=[ScopeRef(file="db/y.py")]
            ),
        ],
        tmp_path / "spec.md",
    )
    mem = ProjectMemory(tmp_path)
    store = SessionStore(tmp_path / ".gadfly")
    actions = [
        _edit(str(tmp_path / "auth" / "x.py")),
        _edit(str(tmp_path / "db" / "y.py")),
    ]
    _, user = architect_context(
        mem, led, store, _event(actions, str(tmp_path)), actions, "balanced"
    )
    assert "auth decision" in user and "db decision" in user


def test_architect_context_includes_cross_project_calibration(tmp_path):
    mem = ProjectMemory(tmp_path)
    led = DecisionLedger(tmp_path / "decisions.md")
    store = SessionStore(tmp_path / ".gadfly")
    action = _edit(str(tmp_path / "x.py"))
    system, user = architect_context(
        mem,
        led,
        store,
        _event([action], str(tmp_path)),
        [action],
        "balanced",
        cross_project="prefer logging over print",
    )
    assert (
        "PERSONAL CALIBRATION" in system and "prefer logging over print" in system
    )  # cached
    assert "prefer logging over print" not in user


def test_context_shows_digest_and_recent_tail(tmp_path):
    (tmp_path / "claude.md").write_text("rules")
    mem = ProjectMemory(tmp_path)
    store = SessionStore(tmp_path / ".gadfly")
    store.append_convo(
        "s",
        [
            ConvoEntry("user", "text", "older turn " * 20),
            ConvoEntry("user", "text", "the recent turn"),
        ],
    )
    digest.compact(
        store, "s", store.gadfly_dir, lambda prev, ov: "SUMMARY OF EARLIER", budget=100
    )
    action = _edit(str(tmp_path / "x.py"))
    _, user = code_context(mem, store, _event([action], str(tmp_path)), [action])
    assert "SESSION SO FAR" in user and "SUMMARY OF EARLIER" in user  # digest surfaced
    assert "the recent turn" in user  # verbatim tail kept
    assert "older turn older turn" not in user  # folded, not verbatim


def test_context_bounds_uncompacted_tail(tmp_path):
    (tmp_path / "claude.md").write_text("rules")
    mem = ProjectMemory(tmp_path)
    store = SessionStore(tmp_path / ".gadfly")
    msgs = [ConvoEntry("user", "text", f"old-{i}-" + "x" * 1000) for i in range(25)]
    msgs.append(ConvoEntry("user", "text", "latest turn"))
    store.append_convo("s", msgs)
    action = _edit(str(tmp_path / "x.py"))

    _, user = code_context(mem, store, _event([action], str(tmp_path)), [action])

    assert "latest turn" in user
    assert "old-0-" not in user


def test_convo_tail_budget_param_bounds_tail(tmp_path):
    (tmp_path / "claude.md").write_text("rules")
    mem = ProjectMemory(tmp_path)
    store = SessionStore(tmp_path / ".gadfly")
    msgs = [ConvoEntry("user", "text", f"m{i}-" + "x" * 1000) for i in range(10)]
    msgs.append(ConvoEntry("user", "text", "latest turn"))
    store.append_convo("s", msgs)
    action = _edit(str(tmp_path / "x.py"))
    _, user = code_context(
        mem, store, _event([action], str(tmp_path)), [action], convo_tail_budget=1500
    )
    assert "latest turn" in user and "m0-" not in user


# --- solo (cover-for-other) prompt variants ----------------------------------


def test_solo_prompts_load():
    assert "ARCHITECT" in load_prompt("architect_solo.md")


def test_architect_system_solo_uses_variant():
    s = architect_system("balanced", solo=True)
    assert "catch critical code defects" in s and "{{MODE}}" not in s


def test_code_context_includes_codemap(tmp_path):
    mem = ProjectMemory(tmp_path)
    mem.path_for("codemap.md").write_text("auth.py: login + token refresh")
    store = SessionStore(tmp_path / ".gadfly")
    action = _edit(str(tmp_path / "x.py"))
    _, user = code_context(mem, store, _event([action], str(tmp_path)), [action])
    assert "STRUCTURE INDEX" in user and "auth.py: login + token refresh" in user


def test_make_architect_parses_ask_with_undiscussed(tmp_path):
    store = SessionStore(tmp_path / ".gadfly")
    p = _Mock(
        _wrap(
            {
                "decision": "ask",
                "note": "spec silent on auth",
                "undiscussed": {"question": "JWT or sessions?"},
            }
        )
    )
    review = make_architect(p, "m", tmp_path, store, "collaborative")
    a = NormalizedAction(
        type=ActionType.CREATE,
        target=str(tmp_path / "auth.py"),
        payload={"content": "..."},
    )
    vs = review(_event([a], str(tmp_path)), [a])
    assert vs[0].decision is Decision.ASK and vs[0].undiscussed.question.startswith(
        "JWT"
    )
    assert p.calls[0]["schema"] is ARCHITECT_VERDICT_SCHEMA


def test_make_architect_reads_cross_project_memory(tmp_path):
    mem_file = tmp_path / "global_memory.md"
    mem_file.write_text("- prefer logging over print\n")
    store = SessionStore(tmp_path / ".gadfly")
    p = _Mock(_wrap({"decision": "allow"}))
    review = make_architect(
        p, "m", tmp_path, store, "balanced", cross_project_path=mem_file
    )
    a = _edit(str(tmp_path / "x.py"))
    review(_event([a], str(tmp_path)), [a])
    assert (
        "prefer logging over print" in p.calls[0]["system"]
    )  # global memory read into context


def test_code_reviewer_uses_code_schema(tmp_path):
    store = SessionStore(tmp_path / ".gadfly")
    p = _Mock(_wrap({"decision": "allow"}))
    a = _edit("x.py")
    make_code_reviewer(p, "m", tmp_path, store)(_event([a]), [a])
    assert p.calls[0]["schema"] is CODE_VERDICT_SCHEMA


# --- safety triage -----------------------------------------------------------


def test_safety_triage_review_allow_and_default(tmp_path):
    store = SessionStore(tmp_path / ".gadfly")
    cmd = NormalizedAction(type=ActionType.EXEC, payload={"command": "rm -rf build"})
    event = InterventionEvent(unit=[cmd], workspace="/w", session="s")
    assert make_safety_triage(_Mock("REVIEW"), "haiku", store)(event, cmd) is True
    assert make_safety_triage(_Mock("ALLOW"), "haiku", store)(event, cmd) is False
    assert (
        make_safety_triage(_Mock("not sure honestly"), "haiku", store)(event, cmd)
        is True
    )
