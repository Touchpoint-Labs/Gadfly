"""The feedback loop: corrections capture/dedup (keyed on file + builder hash + human
hash), the extractor turning human diffs into typed memory proposals (provider mocked),
and run_extraction routing those memories and clearing the queue."""
import json

import pytest

from gadfly import feedback
from gadfly.config import load
from gadfly.state import corrections, learned
from gadfly.state.edits import EditLedger
from gadfly.supervisors import make_extractor
from gadfly.worker import _lock_path, feedback_pass


class _Mock:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def complete(self, *, system, prompt, model, schema=None, tools=True):
        self.calls.append(prompt)
        return self.result


class _Extractor:
    """Stub extractor callable: records what it was handed, returns (or raises) a
    canned result."""
    def __init__(self, result):
        self.result = result
        self.corrections = None
        self.rules = None

    def __call__(self, corrections, rules=""):
        self.corrections, self.rules = corrections, rules
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_capture_dedups_same_edit_but_keeps_new_human_version(tmp_path):
    corrections.capture(tmp_path, "s", "a.py", "old\n", "new\n", "modified", "bh1")
    corrections.capture(tmp_path, "s", "a.py", "old\n", "new\n", "modified", "bh1")   # dup
    assert len(corrections.pending(tmp_path)) == 1
    corrections.capture(tmp_path, "s", "a.py", "old\n", "newer\n", "modified", "bh1")  # new human ver
    assert len(corrections.pending(tmp_path)) == 2


def test_processed_blocks_recapture_after_clear(tmp_path):
    corrections.capture(tmp_path, "s", "a.py", "old\n", "new\n", "modified", "bh1")
    corrections.mark_processed(tmp_path, corrections.pending(tmp_path))
    corrections.clear(tmp_path)
    corrections.capture(tmp_path, "s", "a.py", "old\n", "new\n", "modified", "bh1")   # already processed
    assert corrections.pending(tmp_path) == []


def test_recreate_then_redelete_is_a_distinct_correction(tmp_path):
    corrections.capture(tmp_path, "s", "a.py", "v1\n", None, "deleted", "bh1")
    corrections.mark_processed(tmp_path, corrections.pending(tmp_path))
    corrections.clear(tmp_path)
    corrections.capture(tmp_path, "s", "a.py", "v2\n", None, "deleted", "bh2")        # different builder ver
    assert len(corrections.pending(tmp_path)) == 1


def test_extractor_parses_typed_memories():
    p = _Mock(json.dumps({"memories": [
        {"type": "cross_project_style", "text": "prefer logging over print"},
        {"type": "project", "text": "use Decimal for money"}]}))
    out = make_extractor(p, "m")([{"file": "a.py", "reason": "modified", "diff": "- print\n+ log"}])
    assert [m["type"] for m in out] == ["cross_project_style", "project"]
    assert out[0]["text"] == "prefer logging over print"


def test_extractor_empty_on_nothing():
    assert make_extractor(_Mock(json.dumps({"memories": []})), "m")([]) == []          # no corrections
    assert make_extractor(_Mock("not json"), "m")([{"file": "a.py", "diff": "x"}]) == []  # unparseable


# --- run_extraction: the consume/route half ----------------------------------

def _queue_one(tmp_path):
    """A workspace with a human rule already in claude.md and one pending correction."""
    workspace, gadfly_dir = tmp_path, tmp_path / ".gadfly"
    (workspace / "claude.md").write_text("# CLAUDE.md\n\nHuman rule.\n")
    corrections.capture(gadfly_dir, "s", "a.py", "old\n", "new\n", "modified", "bh1")
    return workspace, gadfly_dir


def test_run_extraction_routes_clears_and_blocks_recapture(tmp_path):
    workspace, gadfly_dir = _queue_one(tmp_path)
    glob = tmp_path / "global.md"
    ext = _Extractor([{"type": "project", "text": "use Decimal for money"},
                      {"type": "cross_project_style", "text": "prefer logging over print"}])

    written = feedback.run_extraction(ext, workspace=workspace, gadfly_dir=gadfly_dir,
                                      global_memory=glob)

    assert len(written) == 2
    assert "Human rule." in ext.rules                      # existing rules handed to the extractor
    claude = (workspace / "claude.md").read_text()
    assert "Human rule." in claude                         # human content preserved
    assert learned.GADFLY_SECTION in claude and "use Decimal for money" in claude
    assert "prefer logging over print" in glob.read_text() # cross-project → global
    assert corrections.pending(gadfly_dir) == []           # queue cleared
    corrections.capture(gadfly_dir, "s", "a.py", "old\n", "new\n", "modified", "bh1")
    assert corrections.pending(gadfly_dir) == []           # processed → not re-captured


def test_run_extraction_empty_queue_is_noop(tmp_path):
    ext = _Extractor([{"type": "project", "text": "x"}])
    assert feedback.run_extraction(ext, workspace=tmp_path, gadfly_dir=tmp_path / ".gadfly",
                                   global_memory=tmp_path / "g.md") == []
    assert ext.corrections is None                          # extractor never called
    assert not (tmp_path / "claude.md").exists()


def test_run_extraction_consumes_queue_when_nothing_generalizes(tmp_path):
    workspace, gadfly_dir = _queue_one(tmp_path)
    feedback.run_extraction(_Extractor([]), workspace=workspace, gadfly_dir=gadfly_dir,
                            global_memory=tmp_path / "g.md")
    assert corrections.pending(gadfly_dir) == []           # still consumed (looked at)
    assert learned.GADFLY_SECTION not in (workspace / "claude.md").read_text()
    corrections.capture(gadfly_dir, "s", "a.py", "old\n", "new\n", "modified", "bh1")
    assert corrections.pending(gadfly_dir) == []           # and marked, so not re-captured


def test_run_extraction_keeps_queue_on_extractor_error(tmp_path):
    workspace, gadfly_dir = _queue_one(tmp_path)
    with pytest.raises(RuntimeError):
        feedback.run_extraction(_Extractor(RuntimeError("boom")), workspace=workspace,
                                gadfly_dir=gadfly_dir, global_memory=tmp_path / "g.md")
    assert len(corrections.pending(gadfly_dir)) == 1       # untouched — retried next idle
    assert learned.GADFLY_SECTION not in (workspace / "claude.md").read_text()


# --- reconcile + dedup-aware gate + feedback_pass ----------------------------


def _diverged(tmp_path):
    """A tracked builder file a human then edited out-of-band: the ledger keeps the
    builder version, the file on disk holds the human's."""
    gadfly_dir = tmp_path / ".gadfly"
    f = tmp_path / "code.py"
    f.write_text("builder\n")
    EditLedger(gadfly_dir).record("s", "Write", str(f))
    f.write_text("human\n")
    return gadfly_dir, f


def test_is_new_correction_dedups(tmp_path):
    assert corrections.is_new_correction(tmp_path, "a.py", "bh1", "new\n") is True
    corrections.capture(tmp_path, "s", "a.py", "old\n", "new\n", "modified", "bh1")
    assert corrections.is_new_correction(tmp_path, "a.py", "bh1", "new\n") is False


def test_reconcile_captures_human_divergence(tmp_path):
    gadfly_dir, _ = _diverged(tmp_path)
    feedback.reconcile(gadfly_dir, "s")
    pend = corrections.pending(gadfly_dir)
    assert len(pend) == 1 and pend[0]["file"].endswith("code.py")
    assert "builder" in pend[0]["diff"] and "human" in pend[0]["diff"]


def test_has_pending_work_dedups_permanent_divergence(tmp_path):
    gadfly_dir, _ = _diverged(tmp_path)
    assert feedback.has_pending_work(gadfly_dir) is True      # unprocessed divergence
    feedback.reconcile(gadfly_dir, "s")
    assert feedback.has_pending_work(gadfly_dir) is True      # now queued
    corrections.mark_processed(gadfly_dir, corrections.pending(gadfly_dir))
    corrections.clear(gadfly_dir)
    # the file STILL diverges (the ledger never advances past a human edit), but it is
    # processed — the gate must say no, or the nudge fires every tool call forever.
    assert feedback.has_pending_work(gadfly_dir) is False


def test_feedback_pass_reconciles_then_extracts(tmp_path):
    gadfly_dir, _ = _diverged(tmp_path)
    (tmp_path / "claude.md").write_text("# CLAUDE.md\n")
    ext = _Extractor([{"type": "project", "text": "prefer X"}])  # project-only → tmp claude.md
    assert feedback_pass(tmp_path, "s", ext) is True
    assert ext.corrections is not None                           # ran on the reconciled diff
    assert corrections.pending(gadfly_dir) == []                 # queue cleared
    assert learned.GADFLY_SECTION in (tmp_path / "claude.md").read_text()


def test_feedback_pass_bails_when_lock_held(tmp_path):
    gadfly_dir, _ = _diverged(tmp_path)
    lock = _lock_path(gadfly_dir, "s", "feedback")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("held")
    ext = _Extractor([{"type": "project", "text": "x"}])
    assert feedback_pass(tmp_path, "s", ext) is False            # another pass owns the lock
    assert ext.corrections is None                               # did nothing


def test_feedback_model_default():
    assert load(None).models.feedback == "claude-sonnet-5"
