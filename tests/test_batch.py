"""Filesystem batch coordination: atomic leadership, publish/read round-trip, and
the degrade contract — a visible file is the COMPLETE set, so a follower whose id
is missing (or unreadable) degrades immediately; it waits only while no file exists."""
import json
import time

from gadfly.adapters.claudecode import batch
from gadfly.contracts import Decision, DecisionOp, ScopeRef, UndiscussedDecision, Verdict


def test_first_claim_leads_rest_follow(tmp_path):
    assert batch.claim_leader(tmp_path, "msg_1") is True
    assert batch.claim_leader(tmp_path, "msg_1") is False   # sibling hook: follower
    assert batch.claim_leader(tmp_path, "msg_2") is True    # new batch, new election


def test_roundtrip_preserves_full_verdict(tmp_path):
    v = Verdict(decision=Decision.DENY, note="no",
                undiscussed=UndiscussedDecision(question="q?", options=["a", "b"]),
                ops=[DecisionOp(op="add", what="X", why="y",
                                scope=[ScopeRef(file="a.py", symbol="f")],
                                supersedes=[3], human_accepted=True)])
    batch.write_verdicts(tmp_path, "m", {"t1": v, "t2": Verdict(decision=Decision.ALLOW)})
    assert batch.read_verdict(tmp_path, "m", "t1", timeout=1) == v
    assert batch.read_verdict(tmp_path, "m", "t2", timeout=1) == Verdict(decision=Decision.ALLOW)


def test_missing_id_degrades_immediately_not_after_timeout(tmp_path):
    batch.write_verdicts(tmp_path, "m", {"t1": Verdict(decision=Decision.ALLOW)})
    t0 = time.monotonic()
    assert batch.read_verdict(tmp_path, "m", "t2", timeout=30) is None
    assert time.monotonic() - t0 < 2   # file visible ⇒ complete: no waiting on a missing id


def test_empty_map_degrades_everyone(tmp_path):
    batch.write_verdicts(tmp_path, "m", {})   # the leader-crashed signal
    t0 = time.monotonic()
    assert batch.read_verdict(tmp_path, "m", "t1", timeout=30) is None
    assert time.monotonic() - t0 < 2


def test_no_file_times_out_to_degrade(tmp_path):
    assert batch.read_verdict(tmp_path, "m", "t1", timeout=0.2) is None


def test_malformed_entry_degrades_instead_of_crashing(tmp_path):
    d = tmp_path / "batch"
    d.mkdir()
    (d / "m.verdicts.json").write_text(json.dumps(
        {"t1": {"decision": "bogus"}, "t2": {"decision": "allow", "ops": [{"nope": 1}]}}))
    assert batch.read_verdict(tmp_path, "m", "t1", timeout=1) is None
    assert batch.read_verdict(tmp_path, "m", "t2", timeout=1) is None
