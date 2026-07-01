from gadfly.state.memory import ProjectMemory


def test_reads_exact_name(tmp_path):
    (tmp_path / "spec.md").write_text("the spec")
    assert ProjectMemory(tmp_path).spec == "the spec"


def test_reads_case_insensitively(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("rules")
    assert ProjectMemory(tmp_path).claude == "rules"


def test_exact_case_wins_over_variant(tmp_path):
    (tmp_path / "claude.md").write_text("lower")
    (tmp_path / "CLAUDE.md").write_text("upper")
    assert ProjectMemory(tmp_path).claude == "lower"


def test_missing_reads_empty(tmp_path):
    mem = ProjectMemory(tmp_path)
    assert mem.spec == "" and mem.claude == "" and mem.codemap == ""
