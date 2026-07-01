"""Translate a neutral Verdict into Claude Code's PreToolUse hook output JSON.

Claude-Code-specific — the permissionDecision shape lives only here. Mapping:
  allow            -> allow (silent)
  allow_with_note  -> allow + additionalContext (the note reaches the builder)
  deny             -> deny + reason (note to the builder) + a user log line
  ask + undiscussed-> deny + reason telling the builder to ask the user, then wait
                      (the relay — a hook can't pose a freeform question itself)
  ask (no question)-> ask (native confirmation for a critical/irreversible action)
  abstain          -> no decision (defer to CC's own permission flow) + a log line
"""
from __future__ import annotations

from ...contracts import Decision, Verdict


def _hook(decision: str = "", *, reason: str = "", additional: str = "", system: str = "") -> dict:
    inner = {"hookEventName": "PreToolUse"}
    if decision:                       # omitted ⇒ no decision ⇒ CC's native flow applies
        inner["permissionDecision"] = decision
    if reason:
        inner["permissionDecisionReason"] = reason
    if additional:
        inner["additionalContext"] = additional
    out = {"hookSpecificOutput": inner}
    if system:
        out["systemMessage"] = system
    return out


def defer(system: str = "") -> dict:
    """Emit NO permissionDecision, so Claude Code's own permission flow applies (in
    default mode the user is asked for anything not pre-allowed). The interactive
    hook schema has no 'defer' value (that's headless-only), so stepping aside means
    staying silent. Used when the action isn't ours to gate (unknown/custom/MCP tool)
    and when review itself errored — we never vouch for an unreviewed action, but we
    never block the build either."""
    return _hook(system=system)


def to_hook_output(verdict: Verdict) -> dict:
    d = verdict.decision
    if d is Decision.ALLOW:
        return _hook("allow")
    if d is Decision.ALLOW_WITH_NOTE:
        note = verdict.note or ""
        return _hook("allow", additional=note)
    if d is Decision.DENY:
        note = verdict.note or "Blocked by Gadfly."
        return _hook("deny", reason=note, system=f"Gadfly blocked: {note}")
    if d is Decision.ASK:
        if verdict.undiscussed:  # a freeform question → relay via the builder, pausing this action
            q = verdict.undiscussed.question
            reason = ("Don't proceed yet — ask the user this and wait for their answer, "
                      f"then continue based on it. Relay the question (and options, if "
                      f"given) verbatim, using your question tool if you have one:"
                      f"\n\nQuestion: {q}")
            if verdict.undiscussed.options:
                reason += "\nOptions:\n" + "\n".join(f"- {o}" for o in verdict.undiscussed.options)
            return _hook("deny", reason=reason, system=f"Gadfly surfaced a decision: {q}")
        # bare ask → native confirmation for a critical/irreversible action
        note = verdict.note or "Gadfly: please confirm this action."
        return _hook("ask", reason=note, system=note)
    if d is Decision.ABSTAIN:  # couldn't get a verdict → step aside, host's flow decides
        return defer("Gadfly: could not produce a verdict — deferred to your normal permission flow.")
    return defer("Gadfly: unrecognised verdict — stepping aside to your normal permission flow.")
