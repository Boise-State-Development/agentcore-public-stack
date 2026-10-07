"""Cutting turns out of a conversation for the archive (conversation-search.md §4).

Pure functions: the runtime, the fork path and the backfill must all produce
the same object for the same history, so the rules are pinned here once.
"""

import json

import pytest

from apis.shared.conversation_archive import (
    MAX_TURN_TEXT_BYTES,
    MAX_USER_TEXT_BYTES,
    archive_key,
    build_turn,
    is_turn_start,
    last_turn_start,
    message_text,
    session_prefix,
    split_turns,
    with_user_text,
)


def _user(text):
    return {"role": "user", "content": [{"text": text}]}


def _assistant(text):
    return {"role": "assistant", "content": [{"text": text}]}


def _tool_call(name="search"):
    return {
        "role": "assistant",
        "content": [
            {"text": "Let me look."},
            {"toolUse": {"toolUseId": "t1", "name": name, "input": {"q": "secret input"}}},
        ],
    }


def _tool_result(extra_text=None):
    content = [{"toolResult": {"toolUseId": "t1", "content": [{"text": "THIRD PARTY DATA"}]}}]
    if extra_text:
        # Mid-turn steering appends the user's follow-up to the tool-result message.
        content.append({"text": extra_text})
    return {"role": "user", "content": content}


# ── keys ──────────────────────────────────────────────────────────────────


def test_key_is_zero_padded_under_the_session_prefix():
    assert archive_key("u1", "s1", 7) == "conversations/u1/s1/000007.json"
    assert session_prefix("u1", "s1") == "conversations/u1/s1/"


@pytest.mark.parametrize("user_id,session_id", [("", "s"), ("u", ""), ("a/b", "s"), ("u", "../x/y")])
def test_ids_that_could_escape_the_session_prefix_are_rejected(user_id, session_id):
    with pytest.raises(ValueError):
        session_prefix(user_id, session_id)


def test_negative_index_is_rejected():
    with pytest.raises(ValueError):
        archive_key("u", "s", -1)


# ── what counts as a turn ─────────────────────────────────────────────────


def test_tool_result_messages_are_not_turn_starts_even_with_steering_text():
    assert is_turn_start(_user("hello"))
    assert not is_turn_start(_tool_result())
    assert not is_turn_start(_tool_result("actually, use BIO 101"))
    assert not is_turn_start(_assistant("hi"))
    assert not is_turn_start({"role": "user", "content": [{"text": "   "}]})


def test_message_text_keeps_prose_and_drops_tool_and_reasoning_blocks():
    msg = {
        "role": "assistant",
        "content": [
            {"reasoningContent": {"reasoningText": {"text": "private chain"}}},
            {"text": "First."},
            {"toolUse": {"toolUseId": "t", "name": "x", "input": {}}},
            {"text": "Second."},
        ],
    }
    assert message_text(msg) == "First.\n\nSecond."


def test_split_turns_indexes_each_turn_at_its_user_message():
    messages = [
        _user("find the syllabus"),         # 0  turn 1
        _tool_call(),                       # 1
        _tool_result("steer: BIO 101"),     # 2
        _assistant("Found it in BIO 101."), # 3
        _user("thanks"),                    # 4  turn 2
        _assistant("You're welcome."),      # 5
    ]
    turns = split_turns(messages, user_id="u", session_id="s", created_at="t", base_index=10)

    assert [t.message_index for t in turns] == [10, 14]
    first = turns[0]
    assert first.user_text == "find the syllabus"
    assert first.assistant_text == "Let me look.\n\nFound it in BIO 101."
    blob = first.to_json().decode()
    assert "THIRD PARTY DATA" not in blob and "secret input" not in blob


def test_split_turns_skips_a_history_that_opens_mid_turn():
    messages = [_assistant("orphan"), _user("q"), _assistant("a")]
    turns = split_turns(messages, user_id="u", session_id="s", created_at="t")
    assert [t.message_index for t in turns] == [1]


def test_last_turn_start_walks_back_past_tool_rounds():
    messages = [_user("q1"), _assistant("a1"), _user("q2"), _tool_call(), _tool_result(), _assistant("a2")]
    assert last_turn_start(messages) == 2
    assert last_turn_start([_assistant("x")]) is None


# ── object body and caps ──────────────────────────────────────────────────


def test_body_carries_ids_and_optional_attribution():
    turn = build_turn(
        user_id="u",
        session_id="s",
        message_index=4,
        user_text="hi",
        assistant_messages=[_assistant("hello")],
        created_at="2026-10-07T00:00:00+00:00",
        project_id="p1",
        assistant_id="",
    )
    body = json.loads(turn.to_json())
    assert body == {
        "schemaVersion": 1,
        "userId": "u",
        "sessionId": "s",
        "messageIndex": 4,
        "userText": "hi",
        "assistantText": "hello",
        "createdAt": "2026-10-07T00:00:00+00:00",
        "projectId": "p1",
    }


def test_attachments_marker_is_not_indexed():
    turn = build_turn(
        user_id="u", session_id="s", message_index=0,
        user_text="summarize this\n\n[Attached files: report.pdf, data.xlsx]",
        assistant_messages=[], created_at="t",
    )
    assert turn.user_text == "summarize this"


def test_a_turn_with_no_text_is_not_archived():
    assert build_turn(
        user_id="u", session_id="s", message_index=0, user_text="  ",
        assistant_messages=[{"role": "assistant", "content": [{"toolUse": {"toolUseId": "t", "name": "x", "input": {}}}]}],
        created_at="t",
    ) is None


def test_text_is_capped_in_utf8_bytes_with_user_text_at_most_half():
    user = "é" * MAX_TURN_TEXT_BYTES  # 2 bytes each
    answer = "a" * (MAX_TURN_TEXT_BYTES * 2)
    turn = build_turn(
        user_id="u", session_id="s", message_index=0, user_text=user,
        assistant_messages=[_assistant(answer)], created_at="t",
    )
    user_bytes = len(turn.user_text.encode("utf-8"))
    assert user_bytes <= MAX_USER_TEXT_BYTES
    assert user_bytes + len(turn.assistant_text.encode("utf-8")) <= MAX_TURN_TEXT_BYTES
    turn.user_text.encode("utf-8").decode("utf-8")  # no split character


def test_with_user_text_replaces_and_recaps():
    turn = build_turn(
        user_id="u", session_id="s", message_index=2, user_text="RAG CONTEXT ... question",
        assistant_messages=[_assistant("a" * MAX_TURN_TEXT_BYTES)], created_at="t",
    )
    replaced = with_user_text(turn, "question" * 2000)
    assert replaced.message_index == 2
    assert len(replaced.user_text.encode()) <= MAX_USER_TEXT_BYTES
    assert len(replaced.user_text.encode()) + len(replaced.assistant_text.encode()) <= MAX_TURN_TEXT_BYTES
