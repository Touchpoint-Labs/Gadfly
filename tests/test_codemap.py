"""Codemap staleness — deterministic, mtime-based nudge counting (no LLM)."""
import json
import os
from datetime import datetime, timedelta, timezone

from gadfly.state import codemap
from gadfly.state.edits import EditLedger


def _entry(file, ts):
    return json.dumps({"ts": ts.isoformat(), "session": "s", "tool": "Edit",
                       "file": file, "hash": "x"})


def _ledger(gadfly_dir, *entries):
    p = gadfly_dir / "edits.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(entries) + "\n")


def _set_mtime(path, when):
    os.utime(path, (path.stat().st_atime, when.timestamp()))


# --- the ledger counter ------------------------------------------------------

def test_edits_since_counts_newer_and_skips_docs(tmp_path):
    g = tmp_path / ".gadfly"
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _ledger(g,
            _entry("/p/a.py", base),                          # older than cutoff
            _entry("/p/b.py", base + timedelta(hours=2)),     # newer
            _entry("/p/c.py", base + timedelta(hours=3)),     # newer
            _entry("/p/README.md", base + timedelta(hours=4)))  # newer but .md → excluded
    cutoff = (base + timedelta(hours=1)).timestamp()
    assert EditLedger(g).edits_since(cutoff) == 2


def test_record_skips_out_of_workspace_files(tmp_path):
    # a /tmp scratch edit isn't project authorship — it must not enter the ledger (else it
    # inflates codemap-staleness and, once cleaned up, reads as a human deletion to feedback)
    import shutil
    import tempfile
    from pathlib import Path
    led = EditLedger(tmp_path / ".gadfly")
    (tmp_path / "real.py").write_text("x")
    led.record("s", "Write", str(tmp_path / "real.py"))          # in-workspace → recorded
    scratch = Path(tempfile.mkdtemp()) / "scratch.py"
    scratch.write_text("y")
    led.record("s", "Write", str(scratch))                       # out-of-workspace → skipped
    assert led.tracked_files() == [str((tmp_path / "real.py").resolve())]
    shutil.rmtree(scratch.parent, ignore_errors=True)


# --- pending() + nudge(), mtime-relative to codemap.md -----------------------

def test_pending_counts_edits_after_codemap(tmp_path):
    g = tmp_path / ".gadfly"
    cm = tmp_path / "codemap.md"
    cm.write_text("map")
    base = datetime.now(timezone.utc)
    _ledger(g, *[_entry(f"/p/f{i}.py", base + timedelta(seconds=i)) for i in range(8)])
    _set_mtime(cm, base - timedelta(minutes=1))               # codemap is older than the edits
    assert codemap.pending(tmp_path) == (8, True)
    msg = codemap.nudge(tmp_path)
    assert msg and "8 code edits" in msg


def test_below_threshold_no_nudge(tmp_path):
    g = tmp_path / ".gadfly"
    cm = tmp_path / "codemap.md"
    cm.write_text("map")
    base = datetime.now(timezone.utc)
    _ledger(g, *[_entry(f"/p/f{i}.py", base + timedelta(seconds=i)) for i in range(7)])
    _set_mtime(cm, base - timedelta(minutes=1))
    assert codemap.pending(tmp_path) == (7, True)
    assert codemap.nudge(tmp_path) is None                    # 7 < THRESHOLD (8)


def test_updating_codemap_resets_the_count(tmp_path):
    g = tmp_path / ".gadfly"
    cm = tmp_path / "codemap.md"
    cm.write_text("map")
    base = datetime.now(timezone.utc)
    _ledger(g, *[_entry(f"/p/f{i}.py", base + timedelta(seconds=i)) for i in range(8)])
    _set_mtime(cm, base + timedelta(minutes=1))               # codemap written AFTER the edits
    assert codemap.pending(tmp_path) == (0, True)             # self-reset via mtime
    assert codemap.nudge(tmp_path) is None


def test_no_codemap_counts_everything(tmp_path):
    g = tmp_path / ".gadfly"
    base = datetime.now(timezone.utc)
    _ledger(g, *[_entry(f"/p/f{i}.py", base + timedelta(seconds=i)) for i in range(8)])
    assert codemap.pending(tmp_path) == (8, False)            # no codemap.md → since=0
    msg = codemap.nudge(tmp_path)
    assert msg and "doesn't exist yet" in msg


def test_missing_codemap_uses_lower_threshold(tmp_path):
    # a project with no codemap at all gets nudged after MISSING_THRESHOLD edits, not 8 —
    # the architect reviews blind until one exists
    g = tmp_path / ".gadfly"
    base = datetime.now(timezone.utc)
    _ledger(g, *[_entry(f"/p/f{i}.py", base + timedelta(seconds=i)) for i in range(3)])
    msg = codemap.nudge(tmp_path)
    assert msg and "doesn't exist yet" in msg


def test_missing_codemap_below_lower_threshold_stays_quiet(tmp_path):
    g = tmp_path / ".gadfly"
    base = datetime.now(timezone.utc)
    _ledger(g, *[_entry(f"/p/f{i}.py", base + timedelta(seconds=i)) for i in range(2)])
    assert codemap.nudge(tmp_path) is None


# --- the gate rides the nudge on an allow, never on a deny -------------------

def test_emit_verdict_rides_nudge_on_allow(monkeypatch, capsys):
    from gadfly.adapters.claudecode.hooks import pretooluse
    from gadfly.contracts import Decision, Verdict
    monkeypatch.setattr(pretooluse.codemap, "nudge", lambda cwd: "REFRESH CODEMAP")
    pretooluse._emit_verdict(Verdict(Decision.ALLOW), "/x")
    out = json.loads(capsys.readouterr().out)
    assert "REFRESH CODEMAP" in out["hookSpecificOutput"]["additionalContext"]


def test_emit_verdict_no_nudge_on_deny(monkeypatch, capsys):
    from gadfly.adapters.claudecode.hooks import pretooluse
    from gadfly.contracts import Decision, Verdict
    monkeypatch.setattr(pretooluse.codemap, "nudge", lambda cwd: "REFRESH CODEMAP")
    pretooluse._emit_verdict(Verdict(Decision.DENY, note="bad"), "/x")
    out = json.loads(capsys.readouterr().out)
    assert "REFRESH CODEMAP" not in out.get("hookSpecificOutput", {}).get("additionalContext", "")
