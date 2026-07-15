"""Reading the in-flight turn from a CC transcript: batch grouping (by message.id),
complete uncut conversation, and the poll. Fixtures mirror the real format —
each tool_use is its own record; parallel siblings share message.id."""
import json

from gadfly.adapters.claudecode.transcript import (
    batch_of, poll_turn, session_messages, _message_id_of, _read, _turn_tail,
)


def _user(text):
    return {"type": "user", "message": {"content": text}}


def _text(mid, text):
    return {"type": "assistant", "message": {"id": mid, "content": [{"type": "text", "text": text}]}}


def _think(mid, text):
    return {"type": "assistant", "message": {"id": mid, "content": [{"type": "thinking", "thinking": text}]}}


def _tool(mid, tid, name, inp):
    return {"type": "assistant",
            "message": {"id": mid, "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def _tool_result():
    return {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}


def test_batch_grouped_by_message_id():
    recs = [_user("do it"), _text("m1", "plan"),
            _tool("m1", "t1", "Write", {"file_path": "/a"}),
            _tool("m1", "t2", "Write", {"file_path": "/b"}),
            _tool("m2", "t9", "Bash", {"command": "ls"})]  # different message — not in batch
    assert _message_id_of(recs, "t2") == "m1"
    batch = batch_of(recs, "m1")
    assert [c.tool_use_id for c in batch] == ["t1", "t2"]            # order preserved, m2 excluded
    assert batch[0].tool_name == "Write" and batch[1].tool_input == {"file_path": "/b"}


def test_unknown_tool_use_id_has_no_message():
    assert _message_id_of([_tool("m1", "t1", "Write", {})], "nope") is None


def test_session_messages_complete_and_uncut():
    long = "x" * 9000  # would have been truncated by the old char cap
    recs = [_user("prev"), _text("m0", "old"),          # earlier turn — included (whole session)
            _user("build the parser"), _think("m1", long), _text("m1", "I'll write it"),
            _tool("m1", "t1", "Write", {"file_path": "/p"}), _tool_result()]
    msgs = session_messages(recs)
    assert [(m.role, m.kind) for m in msgs] == [
        ("user", "text"), ("assistant", "text"),
        ("user", "text"), ("assistant", "thinking"), ("assistant", "text")]
    assert msgs[0].text == "prev"                        # mid-session install isn't blind
    assert msgs[3].text == long                          # full thinking, nothing cut
    # tool_use and tool_result are not conversation entries


def test_session_messages_skips_harness_injected_user_records():
    recs = [_user("<system-reminder>noise</system-reminder>"), _user("real prompt")]
    msgs = session_messages(recs)
    assert [m.text for m in msgs] == ["real prompt"]


def test_session_messages_captures_mid_turn_user_text():
    # anything the user says MID-TURN (interrupting to redirect the builder) arrives as a
    # text block in list content, not a plain string — the moment their input matters most.
    interruption = {"type": "user", "message": {"content": [
        {"type": "text", "text": "hey don't hardcode that, it gives away the answer"},
        {"type": "tool_result", "tool_use_id": "z9", "content": "file contents"}]}}
    recs = [_user("build it"), _tool("m1", "z9", "Read", {"file_path": "/p"}), interruption]
    texts = [m.text for m in session_messages(recs) if m.role == "user"]
    assert "hey don't hardcode that, it gives away the answer" in texts
    assert "file contents" not in texts  # the sibling tool_result stays out


def test_session_messages_skips_tool_authored_text():
    # A skill load is injected as a user record attributed to the tool call that pulled it
    # in — it isn't the user speaking, and runs to hundreds of KB that would swamp the tail.
    # Keyed on sourceToolUseID, so it holds whatever the body's size or wording; isMeta is
    # NOT usable here (real user messages carry it too).
    skill = {"type": "user", "sourceToolUseID": "toolu_skill", "isMeta": True,
             "message": {"content": [{"type": "text", "text": "Approach this as the design lead " + "x" * 9000}]}}
    real = {"type": "user", "isMeta": True,  # isMeta alone must not silence a real message
            "message": {"content": [{"type": "text", "text": "stop, wrong file"}]}}
    texts = [m.text for m in session_messages([skill, real]) if m.role == "user"]
    assert texts == ["stop, wrong file"]


def test_session_messages_filters_harness_markers_but_keeps_bracketed_user_text():
    # markers are matched by name, not by bracket shape: a user may legitimately write
    # "[important] ..." and must not be silenced by it.
    def _u(t):
        return {"type": "user", "message": {"content": [{"type": "text", "text": t}]}}

    recs = [_u("[Request interrupted by user]"), _u("Continue from where you left off."),
            _u("[Image: source: /tmp/1.png]"), _u("[important] don't hardcode that")]
    texts = [m.text for m in session_messages(recs) if m.role == "user"]
    assert texts == ["[important] don't hardcode that"]


def test_session_messages_captures_askuserquestion_answers():
    # an AskUserQuestion answer is a user record whose content is a tool_result (not a
    # string); its summary is the user's decision and must reach supervisors. An ordinary
    # tool_result (a Read's output) stays excluded — keyed on the question's tool_use_id.
    answer = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "q1",
         "content": 'Your questions have been answered: "Rollover?"="2am"'}]}}
    read_out = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "z9", "content": "file contents"}]}}
    recs = [_user("build it"),
            _tool("m1", "q1", "AskUserQuestion", {"questions": []}), answer,
            _tool("m2", "z9", "Read", {"file_path": "/p"}), read_out]
    texts = [m.text for m in session_messages(recs) if m.role == "user"]
    assert "build it" in texts
    assert any(t.startswith("Your questions have been answered") for t in texts)
    assert "file contents" not in texts


def test_read_tolerates_torn_line(tmp_path):
    # the transcript is written concurrently; a still-flushing final line must lose one
    # record, not crash the whole-file read (which would defer the action unreviewed)
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps(_user("hi")) + "\n" + '{"type": "assistant", "message": {"conte')
    recs = _read(str(p))  # must not raise
    assert len(recs) == 1 and recs[0].get("type") == "user"


def test_poll_no_transcript_path_degrades():
    assert poll_turn(None, "t1").found is False


def test_poll_finds_turn_in_file(tmp_path):
    p = tmp_path / "t.jsonl"
    recs = [_user("go"), _text("m1", "ok"), _tool("m1", "t1", "Edit", {"file_path": "/a"})]
    p.write_text("\n".join(json.dumps(r) for r in recs))
    view = poll_turn(str(p), "t1", timeout=1.0)
    assert view.found and view.batch_id == "m1"
    assert [c.tool_use_id for c in view.batch] == ["t1"]
    assert view.messages[0].text == "go"


def test_turn_tail_expands_to_capture_whole_batch(tmp_path):
    # A batch bigger than the initial tail window must still be captured whole — the window
    # widens within the current message until its start boundary is in view (no sibling lost).
    p = tmp_path / "t.jsonl"
    big = "x" * 2000
    recs = [_user("go " + big), _text("m1", "plan " + big),
            _tool("m1", "t1", "Write", {"content": big}),
            _tool("m1", "t2", "Write", {"content": big}),
            _tool("m1", "t3", "Write", {"content": big})]
    p.write_text("\n".join(json.dumps(r) for r in recs))
    tail = _turn_tail(str(p), min_bytes=64)                          # tiny window forces expansion
    assert [c.tool_use_id for c in batch_of(tail, "m1")] == ["t1", "t2", "t3"]


def test_turn_tail_reads_only_current_turn(tmp_path):
    # With old history far exceeding the window, the tail returns just the current turn
    # (found whole) — not the whole file. Bounds batch detection to O(turn), not O(file).
    p = tmp_path / "t.jsonl"
    pad = "z" * 1000
    recs = []
    for i in range(8):
        recs += [_user("q " + pad), _text(f"m{i}", "reply " + pad)]  # ~16KB of history
    recs += [_tool("mX", "t1", "Edit", {"file_path": "/a"})]
    p.write_text("\n".join(json.dumps(r) for r in recs))
    tail = _turn_tail(str(p), min_bytes=512)                         # window << history
    assert [c.tool_use_id for c in batch_of(tail, "mX")] == ["t1"]   # current call found whole
    assert len(tail) < len(recs)                                     # did not read the whole file


def test_poll_timeout_still_captures_conversation(tmp_path):
    # The triggering call never appears (poll times out), but the transcript still holds
    # the conversation — capture it so the store never freezes on a slow/large transcript.
    p = tmp_path / "t.jsonl"
    recs = [_user("go"), _text("m1", "working"), _tool("m1", "t1", "Edit", {"file_path": "/a"})]
    p.write_text("\n".join(json.dumps(r) for r in recs))
    view = poll_turn(str(p), "absent-id", timeout=0.1)
    assert view.found is False                                    # triggering call never seen
    assert [m.text for m in view.messages] == ["go", "working"]   # convo captured anyway
