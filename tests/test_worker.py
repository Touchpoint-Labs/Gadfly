from argparse import Namespace

from gadfly.state import digest
from gadfly.state.session import SessionStore
from gadfly.providers.llm import LLMTransientError
from gadfly.worker import _run_digest, compact_session


def _convo(text: str) -> dict:
    return {"t": "convo", "role": "user", "kind": "text", "text": text}


def test_worker_compacts_one_session_only(tmp_path):
    store = SessionStore(tmp_path / ".gadfly")
    for text in ("a" * 60, "b" * 60):
        store._append("s1", _convo(text))
    for text in ("c" * 60, "d" * 60):
        store._append("s2", _convo(text))

    assert compact_session(tmp_path, "s1", lambda prev, ov: "S1", budget=100) is True

    assert digest.read(store.gadfly_dir, "s1") == "S1"
    assert digest.folded(store.gadfly_dir, "s1") == 1
    assert digest.read(store.gadfly_dir, "s2") == ""
    assert digest.folded(store.gadfly_dir, "s2") == 0


def test_worker_lock_prevents_duplicate_compaction(tmp_path):
    import fcntl
    import os
    store = SessionStore(tmp_path / ".gadfly")
    for text in ("a" * 60, "b" * 60):
        store._append("s", _convo(text))
    lock = store.gadfly_dir / "locks" / f"digest-{digest.session_slug('s')}.lock"
    lock.parent.mkdir(parents=True)
    held = os.open(lock, os.O_CREAT | os.O_WRONLY)   # a live worker genuinely holding the flock
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert (
            compact_session(tmp_path, "s", lambda prev, ov: "DIGEST", budget=100) is False
        )
        assert digest.read(store.gadfly_dir, "s") == ""
    finally:
        os.close(held)


def test_worker_reclaims_dead_workers_lock(tmp_path):
    # a worker SIGKILLed past its cleanup leaves the lock FILE, but the kernel drops its
    # flock on death — so the file is unlocked and compaction must proceed, not wedge the
    # session's maintenance forever
    store = SessionStore(tmp_path / ".gadfly")
    for text in ("a" * 60, "b" * 60):
        store._append("s", _convo(text))
    lock = store.gadfly_dir / "locks" / f"digest-{digest.session_slug('s')}.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("4242")  # leftover file from a dead worker; no flock held

    assert compact_session(tmp_path, "s", lambda prev, ov: "DIGEST", budget=100) is True
    assert digest.read(store.gadfly_dir, "s") == "DIGEST"


def test_worker_leaves_recent_quarter_unfolded(tmp_path):
    store = SessionStore(tmp_path / ".gadfly")
    for i in range(11):
        store._append("s", _convo(chr(ord("a") + i) * 10))

    assert compact_session(tmp_path, "s", lambda prev, ov: "DIGEST", budget=100) is True

    assert digest.folded(store.gadfly_dir, "s") == 9
    assert len(digest.tail(store, "s", store.gadfly_dir)) == 2


def test_worker_retry_recomputes_current_tail(monkeypatch, tmp_path):
    store = SessionStore(tmp_path / ".gadfly")
    for _ in range(700):
        store._append("s", _convo("a" * 60))
    prompts = []

    class Provider:
        def complete(self, **kwargs):
            prompts.append(kwargs["prompt"])
            if len(prompts) == 1:
                for _ in range(200):
                    store._append("s", _convo("c" * 60))
                raise LLMTransientError("timeout")
            return "DIGEST"

    monkeypatch.setattr("gadfly.worker.build_provider", lambda config: Provider())

    assert _run_digest(Namespace(workspace=str(tmp_path), session="s")) == 0
    assert len(prompts) == 2
    assert "c" * 60 in prompts[1]
