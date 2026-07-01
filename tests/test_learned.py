"""learned.py: the global cross-project memory store (memory.md, architect-read) and
project rules auto-written into the supervised project's claude.md — both deduped."""
from gadfly.state import learned


def test_cross_project_record_dedups_and_reads(tmp_path):
    mem = tmp_path / "memory.md"
    learned.record_cross_project(mem, "prefer logging over print")
    learned.record_cross_project(mem, "prefer  logging over print")   # same after normalize
    learned.record_cross_project(mem, "use type hints")
    text = learned.read_cross_project(mem)
    assert text.count("prefer logging over print") == 1 and "use type hints" in text


def test_read_cross_project_missing_is_empty(tmp_path):
    assert learned.read_cross_project(tmp_path / "nope.md") == ""


def test_project_rule_auto_written_under_section(tmp_path):
    claude = tmp_path / "claude.md"
    claude.write_text("# CLAUDE.md\n\nHuman rule.\n")
    learned.record_project_rule(claude, "use Decimal for money")
    learned.record_project_rule(claude, "use  Decimal for money")   # dup after normalize
    text = claude.read_text()
    assert "Human rule." in text                       # human content preserved
    assert learned.GADFLY_SECTION in text              # demarcated from human rules
    assert text.count("use Decimal for money") == 1    # deduped


def test_project_rule_creates_file_when_absent(tmp_path):
    claude = tmp_path / "claude.md"
    learned.record_project_rule(claude, "use Decimal for money")
    text = claude.read_text()
    assert learned.GADFLY_SECTION in text and "use Decimal for money" in text
