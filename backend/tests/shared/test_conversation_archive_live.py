"""Archiving the turn that just finished (the stream coordinator's entry point).

``schedule_turn_archive`` maps the agent's message list onto the session's
global message ids, picks the user's clean words, and queues the write in the
background. These tests pin the index arithmetic, the gates, and that it never
raises into the turn.

The placement comes from what the request appended, never from the role of the
list's tail: a resume the model closes without prose ends on a tool result, and
placing that from "the last assistant message" once wrote the turn over the
previous turn's object (1.27.0 smoke pass on dev, 2026-10-09).
"""

from typing import List, Tuple
from unittest.mock import patch

import pytest

from apis.shared.conversation_archive import ArchivedTurn, drain_pending
from apis.shared.conversation_archive.live import appended_since, schedule_turn_archive, tail_anchor


def _user(text):
    return {"role": "user", "content": [{"text": text}]}


def _assistant(text):
    return {"role": "assistant", "content": [{"text": text}]}


def _tool_call(tool_id="t", text=None):
    content = [{"text": text}] if text else []
    return {"role": "assistant", "content": content + [{"toolUse": {"toolUseId": tool_id, "name": "x", "input": {}}}]}


def _tool_result(tool_id="t"):
    return {"role": "user", "content": [{"toolResult": {"toolUseId": tool_id, "content": [{"text": "raw"}]}}]}


_TOOL_CALL = _tool_call()
_TOOL_RESULT = _tool_result()


@pytest.fixture()
def written(monkeypatch):
    """Each queued write as ``(turn, expect_existing)``."""
    monkeypatch.setenv("CONVERSATION_INDEX_ENABLED", "true")
    captured: List[Tuple[ArchivedTurn, bool]] = []

    async def _fake_write(turn, *, expect_existing):
        captured.append((turn, expect_existing))
        return True

    with patch("apis.shared.conversation_archive.live.write_turn_guarded", _fake_write):
        yield captured


def _schedule(messages, **overrides):
    kwargs = dict(
        messages=messages,
        user_id="u1",
        session_id="s1",
        turn_first_index=0,
        appended_count=len(messages),
        original_message=None,
    )
    kwargs.update(overrides)
    return schedule_turn_archive(**kwargs)


@pytest.mark.asyncio
async def test_a_normal_multi_tool_turn_is_keyed_at_its_user_message(written):
    # History was compacted: the agent holds 7 messages, but the stored history
    # had 38 before this turn. The request appended the turn's six messages.
    messages = [
        _assistant("older"),
        _user("find it"),
        _tool_call("a", "Looking."),
        _tool_result("a"),
        _tool_call("b"),
        _tool_result("b"),
        _assistant("found"),
    ]
    assert _schedule(messages, turn_first_index=38, appended_count=6, project_id="p", assistant_id="a")
    await drain_pending()

    assert len(written) == 1
    turn, expect_existing = written[0]
    assert (turn.message_index, turn.user_text, turn.assistant_text) == (38, "find it", "Looking.\n\nfound")
    assert (turn.project_id, turn.assistant_id) == ("p", "a")
    assert expect_existing is False, "a turn this request started has a fresh key: create-only"


@pytest.mark.asyncio
async def test_a_resume_that_ends_without_assistant_text_keeps_the_paused_turns_key(written):
    """The dev repro: messages 0..23, then user 24 → ask_user_question (25) paused
    and archived at 24. The resume appended only the tool result (26); the model
    ended the turn with no prose. Must re-archive at 24, never at 22."""
    messages = [_user("previous"), _assistant("previous answer"), _user("plan my week"), _tool_call("q", "One question first.")]
    resumed = messages + [_tool_result("q")]
    lookups = []

    def _fake_lookup(session_id, user_id, message_id):
        lookups.append(message_id)
        return "plan my week"

    with patch("apis.shared.sessions.metadata.get_user_display_text", _fake_lookup):
        # The pause: stored count 24 before it, the request appended user + toolUse.
        assert _schedule(messages, turn_first_index=24, appended_count=2)
        # The resume: stored count 26 before it, the request appended the tool result.
        assert _schedule(resumed, turn_first_index=26, appended_count=1)
        await drain_pending()

    (paused, paused_existing), (resume, resume_existing) = written
    assert paused.message_index == resume.message_index == 24
    assert (paused_existing, resume_existing) == (False, True)
    assert resume.user_text == "plan my week" and resume.assistant_text == "One question first."
    assert lookups == [24], "the resume reads the clean displayText of the paused turn's user message"


@pytest.mark.asyncio
async def test_a_resume_with_text_extends_the_paused_turn(written):
    messages = [_user("plan my week"), _tool_call("q", "One question first."), _tool_result("q"), _assistant("Here is the plan.")]

    with patch("apis.shared.sessions.metadata.get_user_display_text", lambda *a: "plan my week"):
        assert _schedule(messages, turn_first_index=2, appended_count=2)
        await drain_pending()

    turn, expect_existing = written[0]
    assert (turn.message_index, turn.assistant_text) == (0, "One question first.\n\nHere is the plan.")
    assert expect_existing is True


@pytest.mark.asyncio
async def test_the_users_original_words_beat_the_augmented_prompt(written):
    messages = [_user("<kb excerpts> ... what is BIO 101?"), _assistant("A course.")]
    _schedule(messages, original_message="what is BIO 101?")
    await drain_pending()
    assert written[0][0].user_text == "what is BIO 101?"


@pytest.mark.asyncio
async def test_a_resumed_turn_reads_the_stored_display_text(written):
    """After an interrupt the user's message predates this request, and its
    stored text may be the augmented prompt; displayText is the clean copy."""
    messages = [_user("<kb excerpts> ... question"), _TOOL_CALL, _TOOL_RESULT, _assistant("answer")]
    lookups = []

    def _fake_lookup(session_id, user_id, message_id):
        lookups.append((session_id, user_id, message_id))
        return "question"

    with patch("apis.shared.sessions.metadata.get_user_display_text", _fake_lookup):
        _schedule(messages, turn_first_index=12, appended_count=2)
        await drain_pending()

    assert lookups == [("s1", "u1", 10)]
    assert written[0][0].message_index == 10 and written[0][0].user_text == "question"


@pytest.mark.asyncio
async def test_a_turn_started_by_this_request_needs_no_lookup(written):
    def _boom(*args):
        raise AssertionError("no lookup expected")

    with patch("apis.shared.sessions.metadata.get_user_display_text", _boom):
        _schedule([_user("q"), _assistant("a")])
        await drain_pending()
    assert written[0][0].user_text == "q"


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [None, "", "false"])
async def test_flag_off_writes_nothing(written, monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("CONVERSATION_INDEX_ENABLED", raising=False)
    else:
        monkeypatch.setenv("CONVERSATION_INDEX_ENABLED", flag)
    assert not _schedule([_user("q"), _assistant("a")])
    await drain_pending()
    assert written == []


@pytest.mark.asyncio
async def test_preview_sessions_are_never_archived(written):
    assert not _schedule([_user("q"), _assistant("a")], session_id="preview-abc")
    await drain_pending()
    assert written == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "messages,first_index,appended",
    [
        ([], 3, 0),                                 # nothing to archive
        ([_user("q"), _assistant("a")], None, 2),   # coordinator could not count the history
        ([_user("q"), _assistant("a")], 0, None),   # this request's messages could not be counted
        ([_user("q"), _assistant("a")], 0, 3),      # more appended than the list holds
        ([_user("q"), _assistant("a")], 2, 0),      # the request appended nothing: no rewrite
        ([_assistant("a")], 5, 1),                  # no user turn in view
        ([_user("q"), _assistant("a")], 0, 1),      # arithmetic would go negative
    ],
)
async def test_unplaceable_turns_are_skipped(written, messages, first_index, appended):
    assert not _schedule(messages, turn_first_index=first_index, appended_count=appended)
    await drain_pending()
    assert written == []


@pytest.mark.asyncio
async def test_never_raises_into_the_turn(written):
    assert not _schedule([object()])  # malformed message list
    await drain_pending()
    assert written == []


# ── counting what a request appended ─────────────────────────────────────────


def test_appended_since_counts_by_identity_through_a_head_trim():
    messages = [_user("a"), _assistant("b"), _user("c"), _tool_call()]
    anchor = tail_anchor(messages)
    messages.append(_tool_result())
    del messages[:2]  # the sliding window trims the head mid-turn
    assert appended_since(messages, anchor) == 1


def test_appended_since_an_empty_start_counts_everything():
    messages: list = []
    anchor = tail_anchor(messages)
    messages.extend([_user("q"), _assistant("a")])
    assert appended_since(messages, anchor) == 2


def test_appended_since_a_lost_anchor_is_unplaceable():
    anchor = tail_anchor([_user("q")])
    assert appended_since([_user("q"), _assistant("a")], anchor) is None


def test_appended_since_never_raises():
    assert appended_since(object(), _user("q")) is None
