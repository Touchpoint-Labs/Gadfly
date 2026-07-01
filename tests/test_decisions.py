"""The decisions ledger: lean-format round-trip, apply(ops), and the slice
(recent N ∪ scope-file overlap with the change)."""
from gadfly.contracts import DecisionOp, ScopeRef
from gadfly.state.decisions import DecisionLedger


def _ref(s: str) -> ScopeRef:
    f, _, sym = s.partition("#")
    return ScopeRef(file=f, symbol=sym)


def _add(what, why="w", scope=(), **kw):
    return DecisionOp(op="add", what=what, why=why, scope=[_ref(s) for s in scope], **kw)


def _led(tmp_path):
    return DecisionLedger(tmp_path / "decisions.md"), tmp_path / "spec.md"


def test_add_assigns_ids_and_roundtrips(tmp_path):
    led, spec = _led(tmp_path)
    led.apply([_add("use JWT", "stateless", ["auth.py#login"]),
               _add("cache 5min", "perf", ["cache.py", "api.py"])], spec)
    back = led.all()
    assert [d.id for d in back] == [1, 2]
    assert back[0].what == "use JWT" and back[0].why == "stateless"
    assert back[0].scope == ["auth.py#login"]
    assert back[1].scope == ["cache.py", "api.py"]
    assert all(d.active for d in back)


def test_multiline_text_flattens_to_one_line(tmp_path):
    led, spec = _led(tmp_path)
    led.apply([_add("x", "line one\nline two")], spec)
    assert led.all()[0].why == "line one line two"   # lean format: one line per field


def test_add_dedupes_by_what(tmp_path):
    led, spec = _led(tmp_path)
    led.apply([_add("one decision")], spec)
    led.apply([_add("one decision")], spec)
    assert len(led.all()) == 1


def test_supersedes_flips_to_tombstone(tmp_path):
    led, spec = _led(tmp_path)
    led.apply([_add("old way", "w", ["a.py"])], spec)
    led.apply([_add("new way", "w", ["a.py"], supersedes=[1])], spec)
    old, new = led.all()
    assert old.status == "superseded by D2" and not old.active
    assert old.why == "" and old.scope == []          # collapsed to one line
    assert new.active
    text = led.path.read_text()
    assert "D1 · superseded by D2 · old way" in text
    assert [d.id for d in led.slice(n=10)] == [2]     # tombstones leave the slice


def test_human_accepted_promotes_to_spec(tmp_path):
    led, spec = _led(tmp_path)
    led.apply([_add("sessions over JWT", "user said so", human_accepted=True)], spec)
    assert led.all()[0].promoted
    assert "sessions over JWT" in spec.read_text() and "Accepted decisions" in spec.read_text()
    assert "[spec]" in led.path.read_text()
    assert led.all()[0].what == "sessions over JWT"   # marker isn't part of the text


def test_revise_replaces_content(tmp_path):
    led, spec = _led(tmp_path)
    led.apply([_add("retry capped", "old why", ["a.py#f"])], spec)
    led.apply([DecisionOp(op="revise", id=1, what="retry capped at 3", why="new why",
                          scope=[_ref("b.py#g")])], spec)
    d = led.all()[0]
    assert d.what == "retry capped at 3" and d.why == "new why" and d.scope == ["b.py#g"]
    assert d.active


def test_retire_keeps_what_and_reason_only(tmp_path):
    led, spec = _led(tmp_path)
    led.apply([_add("defer is valid", "docs said so", ["verdict.py"])], spec)
    led.apply([DecisionOp(op="retire", id=1, reason="grounded check showed otherwise")], spec)
    d = led.all()[0]
    assert d.status == "retired — grounded check showed otherwise"
    assert d.what == "defer is valid" and d.why == "" and d.scope == []
    assert not d.active and led.slice(n=10) == []


def test_delete_removes_outright(tmp_path):
    led, spec = _led(tmp_path)
    led.apply([_add("noise"), _add("keeper")], spec)
    led.apply([DecisionOp(op="delete", id=1, reason="mechanics, not a decision")], spec)
    assert [d.what for d in led.all()] == ["keeper"]
    assert "noise" not in led.path.read_text()


def test_unknown_target_ids_are_skipped(tmp_path):
    led, spec = _led(tmp_path)
    led.apply([_add("real")], spec)
    led.apply([DecisionOp(op="retire", id=99, reason="r"),
               DecisionOp(op="revise", id=42, what="x"),
               DecisionOp(op="delete", id=7)], spec)   # stale/hallucinated ids: no crash
    assert led.all()[0].what == "real" and led.all()[0].active


def test_slice_recent_n(tmp_path):
    led, spec = _led(tmp_path)
    led.apply([_add(f"d{i}") for i in range(5)], spec)
    assert [d.id for d in led.slice(n=2)] == [4, 5]


def test_slice_pulls_scope_file_overlap_beyond_n(tmp_path):
    led, spec = _led(tmp_path)
    led.apply([_add("auth choice", "w", ["src/auth.py#login"])], spec)   # D1 (old)
    led.apply([_add(f"d{i}", "w", ["other.py"]) for i in range(5)], spec)
    ids = [d.id for d in led.slice(files=["/abs/repo/src/auth.py"], n=2)]
    assert ids == [1, 5, 6]   # file-matched D1 + recent 2 (absolute path meets relative anchor)
