"""Memory-file compaction — budget check, enforce, proposals, accept/dismiss."""

from pathlib import Path

from gadfly.state import compaction


def test_check_over_budget(tmp_path):
    p = tmp_path / "spec.md"
    p.write_text("x" * 100)
    assert compaction.check(p, 50) is True
    assert compaction.check(p, 200) is False


def test_enforce_budget_noop_when_under():
    assert compaction.enforce_budget("short", 100, lambda c, b: c) == "short"


def test_enforce_budget_truncates_after_failed_condense():
    def bad_condense(content, budget):
        return content

    out = compaction.enforce_budget("a" * 200, 100, bad_condense, retries=0)
    assert len(out) <= 100


def test_enforce_budget_retries_condense():
    calls = []

    def shrink_on_retry(content, budget):
        calls.append(budget)
        if len(calls) == 1:
            return "b" * 150
        return "ok"

    out = compaction.enforce_budget("a" * 200, 100, shrink_on_retry, retries=1)
    assert out == "ok"
    assert len(calls) == 2


def test_enforce_budget_targets_ratio_on_first_attempt():
    targets = []

    def condense(content, budget):
        targets.append(budget)
        return "x" * 200  # always over budget, forces retries and truncate

    compaction.enforce_budget("a" * 200, 100, condense, retries=1)
    assert targets[0] == 75  # int(100 * 0.75)
    assert targets[1] == 90  # max(100 - 500, int(100 * 0.90))
    assert targets[1] == 90


def test_human_owned_writes_proposal(tmp_path):
    p = tmp_path / "spec.md"
    p.write_text("x" * 200)
    gadfly = tmp_path / ".gadfly"

    def condense(content, budget):
        return "y" * 50

    assert compaction.compact(p, 100, condense, gadfly) is False
    assert p.read_text() == "x" * 200  # unchanged
    assert compaction.proposal(gadfly, "spec.md") == "y" * 50
    assert compaction.pending_proposals(gadfly) == ["spec.md"]


def test_ai_owned_applies_in_place(tmp_path):
    p = tmp_path / "memory.md"
    p.write_text("x" * 200)
    gadfly = tmp_path / ".gadfly"

    def condense(content, budget):
        return "compact"

    assert compaction.compact(p, 100, condense, gadfly) is True
    assert p.read_text() == "compact"


def test_accept_and_dismiss_proposal(tmp_path):
    p = tmp_path / "claude.md"
    p.write_text("original")
    gadfly = tmp_path / ".gadfly"

    # write a proposal + pending marker
    compaction._proposal_path(gadfly, "claude.md").parent.mkdir(
        parents=True, exist_ok=True
    )
    compaction._proposal_path(gadfly, "claude.md").write_text("proposed")
    compaction._pending_path(gadfly).write_text("claude.md\n")

    assert compaction.accept(gadfly, "claude.md", p) is True
    assert p.read_text() == "proposed"
    assert compaction.pending_proposals(gadfly) == []

    # dismiss
    compaction._proposal_path(gadfly, "claude.md").write_text("again")
    compaction._pending_path(gadfly).write_text("claude.md\n")
    compaction.dismiss(gadfly, "claude.md")
    assert p.read_text() == "proposed"  # unchanged
    assert compaction.pending_proposals(gadfly) == []


def test_check_all_with_mixed_owned(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    spec = workspace / "spec.md"
    spec.write_text("x" * 200)
    codemap = workspace / "codemap.md"
    codemap.write_text("y" * 50)
    claude = workspace / "claude.md"
    claude.write_text("z" * 200)

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "global")
    monkeypatch.setattr(compaction, "DEFAULT_MEMORY_BUDGET", 100)
    (tmp_path / "global" / ".gadfly").mkdir(parents=True)
    (tmp_path / "global" / ".gadfly" / "memory.md").write_text("m" * 200)

    gadfly = workspace / ".gadfly"

    def condense(content, budget):
        return "compact" * max(1, budget // 8)

    budgets = {"spec": 50, "claude": 50, "codemap": 50, "memory": 50}
    proposed = compaction.check_all(workspace, gadfly, budgets, condense)

    assert set(proposed) == {"spec.md", "claude.md"}
    assert compaction.proposal(gadfly, "spec.md") is not None
    assert compaction.proposal(gadfly, "claude.md") is not None
    assert spec.read_text().startswith("x" * 100)
    assert claude.read_text().startswith("z" * 100)
    global_mem = tmp_path / "global" / ".gadfly" / "memory.md"
    assert "compact" in global_mem.read_text()
    assert codemap.read_text() == "y" * 50  # under budget, untouched


def test_proposal_none_for_missing(tmp_path):
    gadfly = tmp_path / ".gadfly"
    assert compaction.proposal(gadfly, "nonexistent") is None


def test_accept_nonexistent_returns_false(tmp_path):
    p = tmp_path / "spec.md"
    p.write_text("orig")
    gadfly = tmp_path / ".gadfly"
    assert compaction.accept(gadfly, "spec.md", p) is False
    assert p.read_text() == "orig"


def test_pending_proposals_skips_missing_proposal_file(tmp_path):
    gadfly = tmp_path / ".gadfly"
    compaction._pending_path(gadfly).parent.mkdir(parents=True, exist_ok=True)
    compaction._pending_path(gadfly).write_text("spec.md\nclaude.md\n")
    assert compaction.pending_proposals(gadfly) == []


def test_budget_zero_disables_compaction(tmp_path):
    p = tmp_path / "spec.md"
    p.write_text("x" * 200)
    gadfly = tmp_path / ".gadfly"

    def condense(content, budget):
        return "compact"

    assert compaction.compact(p, 0, condense, gadfly) is False
    assert compaction.check(p, 0) is False
