"""Verdict -> Claude Code permissionDecision mapping (pure)."""
from gadfly.adapters.claudecode.verdict import defer, to_hook_output
from gadfly.contracts import Decision, UndiscussedDecision, Verdict


def _pd(out):
    return out["hookSpecificOutput"]["permissionDecision"]


def test_allow_is_silent():
    out = to_hook_output(Verdict(Decision.ALLOW))
    assert _pd(out) == "allow"
    assert "additionalContext" not in out["hookSpecificOutput"]
    assert "systemMessage" not in out


def test_allow_with_note_passes_note_to_builder():
    out = to_hook_output(Verdict(Decision.ALLOW_WITH_NOTE, note="second token path next to sessions.py"))
    assert _pd(out) == "allow"
    assert "second token path" in out["hookSpecificOutput"]["additionalContext"]


def test_deny_feeds_reason_back_and_logs():
    out = to_hook_output(Verdict(Decision.DENY, note="off-by-one in loop bound"))
    assert _pd(out) == "deny"
    assert "off-by-one" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "off-by-one" in out["systemMessage"]


def test_deny_with_undiscussed_surfaces_the_question_too():
    # a code DENY co-occurring with an architect ASK+undiscussed (merge → DENY) must not
    # drop the surfaced question: it rides along in the reason (verbatim + options) and in
    # the user's log line, mirroring the ASK path
    out = to_hook_output(Verdict(Decision.DENY, note="off-by-one in loop bound",
        undiscussed=UndiscussedDecision(question="JWT or sessions?", options=["JWT", "sessions"])))
    assert _pd(out) == "deny"
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "off-by-one" in reason                        # the block is still relayed
    assert "JWT or sessions?" in reason                  # the surfaced question rides along
    assert "- JWT" in reason and "- sessions" in reason  # options verbatim, like the ASK path
    assert "JWT or sessions?" in out["systemMessage"]    # user sees it even if the builder fumbles


def test_ask_with_undiscussed_relays_question_as_deny():
    out = to_hook_output(Verdict(Decision.ASK, undiscussed=UndiscussedDecision(
        question="JWT or sessions?", options=["JWT", "sessions"])))
    assert _pd(out) == "deny"  # relay: block + tell the builder to ask the user
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "JWT or sessions?" in reason and "ask the user" in reason.lower()
    assert "- JWT" in reason and "- sessions" in reason   # options relayed verbatim


def test_bare_ask_is_native_confirm():
    out = to_hook_output(Verdict(Decision.ASK, note="force-push to main"))
    assert _pd(out) == "ask"


def test_defer_emits_no_decision():
    # Stepping aside = OMITTING permissionDecision (no interactive "defer" value
    # exists) — CC's own permission flow then applies. D8: never vouch, never block.
    out = defer("Gadfly: review errored")
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert out["systemMessage"] == "Gadfly: review errored"


def test_abstain_steps_aside():
    out = to_hook_output(Verdict(Decision.ABSTAIN))
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert "systemMessage" in out   # nothing unlogged
