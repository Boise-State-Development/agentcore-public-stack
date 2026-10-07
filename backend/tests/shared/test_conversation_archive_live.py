"""Archiving the turn that just finished (the stream coordinator's entry point).

``schedule_turn_archive`` maps the agent's message list onto the session's
global message ids, picks the user's clean words, and queues the write in the
background. These tests pin the index arithmetic, the gates, and that it never
raises into the turn.
"""

from typing import List
from unittest.mock import patch

import pytest

from apis.shared.conversation_archive import ArchivedTurn, drain_pending
from apis.shared.conversation_archive.live import schedule_turn_archive


def _user(text):
    return {"role": "user", "content": [{"text": text}]}


def _assistant(text):
    return {"role": "assistant", "content": [{"text": text}]}


_TOOL_CALL = {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t", "name": "x", "input": {}}}]}
_TOOL_RESULT = {"role": "user", "content": [{"toolResult": {"toolUseId": "t", "content": [{"text": "raw"}]}}]}


@pytest.fixture()
def written(monkeypatch):
    monkeypatch.setenv("CONVERSATION_INDEX_ENABLED", "true")
    captured: List[ArchivedTurn] = []

    async def _fake_write(turns):
        captured.extend(turns)
        return len(turns)

    with patch("apis.shared.conversation_archive.live.write_turns", _fake_write):
        yield captured


def _schedule(messages, **overrides):
    kwargs = dict(
        messages=messages,
        user_id="u1",
        session_id="s1",
        last_message_index=len(messages) - 1,
        turn_first_index=None,
        original_message=None,
    )
    kwargs.update(overrides)
    return schedule_turn_archive(**kwargs)


@pytest.mark.asyncio
async def test_a_tool_using_turn_is_keyed_at_its_user_message(written):
    # History was compacted: the agent holds 5 messages, but the turn's last
    # assistant message is global index 41. The turn began 4 messages earlier.
    messages = [_assistant("older"), _user("find it"), _TOOL_CALL, _TOOL_RESULT, _assistant("found")]
    assert _schedule(messages, last_message_index=41, turn_first_index=38, project_id="p", assistant_id="a")
    await drain_pending()

    assert len(written) == 1
    turn = written[0]
    assert (turn.message_index, turn.user_text, turn.assistant_text) == (38, "find it", "found")
    assert (turn.project_id, turn.assistant_id) == ("p", "a")


@pytest.mark.asyncio
async def test_the_users_original_words_beat_the_augmented_prompt(written):
    messages = [_user("<kb excerpts> ... what is BIO 101?"), _assistant("A course.")]
    _schedule(messages, original_message="what is BIO 101?", turn_first_index=0)
    await drain_pending()
    assert written[0].user_text == "what is BIO 101?"


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
        _schedule(messages, last_message_index=13, turn_first_index=12)
        await drain_pending()

    assert lookups == [("s1", "u1", 10)]
    assert written[0].message_index == 10 and written[0].user_text == "question"


@pytest.mark.asyncio
async def test_a_turn_started_by_this_request_needs_no_lookup(written):
    def _boom(*args):
        raise AssertionError("no lookup expected")

    with patch("apis.shared.sessions.metadata.get_user_display_text", _boom):
        _schedule([_user("q"), _assistant("a")], turn_first_index=0)
        await drain_pending()
    assert written[0].user_text == "q"


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
    "messages,last_index",
    [
        ([], 3),                               # nothing to archive
        ([_user("q"), _assistant("a")], None), # coordinator could not place the turn
        ([_assistant("a")], 5),                # no user turn in view
        ([_user("q"), _assistant("a")], 0),    # arithmetic would go negative
    ],
)
async def test_unplaceable_turns_are_skipped(written, messages, last_index):
    assert not _schedule(messages, last_message_index=last_index)
    await drain_pending()
    assert written == []


@pytest.mark.asyncio
async def test_never_raises_into_the_turn(written):
    assert not _schedule([object()])  # malformed message list
    await drain_pending()
    assert written == []
