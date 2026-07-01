"""Digest: per-session compaction folds overflow, keeps recent records unfolded,
and is recursive (each fold summarises prev_digest + new overflow)."""

from gadfly.state import digest


class FakeStore:
    def __init__(self):
        self.recs = []

    def add(self, text):
        self.recs.append({"t": "convo", "role": "user", "kind": "text", "text": text})

    def records(self, session):
        return self.recs


def _summarizer():
    calls = []

    def summarize(prev, overflow):
        calls.append((prev, overflow))
        return f"DIGEST<{len(calls)}>"

    return summarize, calls


def test_no_compaction_under_budget(tmp_path):
    store = FakeStore()
    store.add("x" * 60)
    s, calls = _summarizer()
    assert digest.compact(store, "s", tmp_path, s, budget=100) is False
    assert calls == []
    assert digest.read(tmp_path, "s") == "" and digest.folded(tmp_path, "s") == 0
    assert [r["text"] for r in digest.tail(store, "s", tmp_path)] == ["x" * 60]


def test_compaction_folds_overflow_keeps_recent(tmp_path):
    store = FakeStore()
    store.add("a" * 60)
    store.add("b" * 60)
    s, calls = _summarizer()
    assert digest.compact(store, "s", tmp_path, s, budget=100) is True
    assert len(calls) == 1 and calls[0][0] == ""  # first fold: empty prev digest
    assert "a" * 60 in calls[0][1]  # older record folded in
    assert digest.folded(tmp_path, "s") == 1
    assert digest.read(tmp_path, "s") == "DIGEST<1>"
    assert [r["text"] for r in digest.tail(store, "s", tmp_path)] == ["b" * 60]


def test_compaction_keeps_recent_quarter_of_budget(tmp_path):
    store = FakeStore()
    for _ in range(11):
        store.add("x" * 10)
    s, _ = _summarizer()
    assert digest.compact(store, "s", tmp_path, s, budget=100) is True
    assert digest.folded(tmp_path, "s") == 9  # leaves ~25 chars -> 2 records
    assert len(digest.tail(store, "s", tmp_path)) == 2


def test_compaction_is_recursive(tmp_path):
    store = FakeStore()
    for c in "ab":
        store.add(c * 60)
    s, calls = _summarizer()
    digest.compact(store, "s", tmp_path, s, budget=100)  # folds "a", digest = DIGEST<1>
    store.add("c" * 60)
    store.add("d" * 60)
    assert digest.compact(store, "s", tmp_path, s, budget=100) is True
    assert calls[1][0] == "DIGEST<1>"  # prev digest fed back in
    assert digest.folded(tmp_path, "s") == 3
    assert [r["text"] for r in digest.tail(store, "s", tmp_path)] == ["d" * 60]


def test_single_oversized_record_does_not_fold(tmp_path):
    store = FakeStore()
    store.add("x" * 300)
    s, calls = _summarizer()
    assert digest.compact(store, "s", tmp_path, s, budget=100) is False
    assert calls == []  # nothing to fold below it


def test_digest_state_is_per_session(tmp_path):
    a = FakeStore()
    a.add("a" * 60)
    a.add("b" * 60)
    b = FakeStore()
    b.add("c" * 60)
    b.add("d" * 60)
    s, _ = _summarizer()

    assert digest.compact(a, "session-a", tmp_path, s, budget=100) is True

    assert digest.read(tmp_path, "session-a") == "DIGEST<1>"
    assert digest.folded(tmp_path, "session-a") == 1
    assert digest.read(tmp_path, "session-b") == ""
    assert digest.folded(tmp_path, "session-b") == 0
    assert [r["text"] for r in digest.tail(b, "session-b", tmp_path)] == [
        "c" * 60,
        "d" * 60,
    ]


def test_tail_can_be_bounded_without_compaction(tmp_path):
    store = FakeStore()
    for i in range(6):
        store.add(str(i) * 10)

    assert [r["text"] for r in digest.tail(store, "s", tmp_path, max_chars=25)] == [
        "4" * 10,
        "5" * 10,
    ]


def test_bounded_tail_keeps_whole_latest_record(tmp_path):
    store = FakeStore()
    store.add("old" * 10)
    store.add("latest" * 100)

    assert [r["text"] for r in digest.tail(store, "s", tmp_path, max_chars=25)] == [
        "latest" * 100
    ]
