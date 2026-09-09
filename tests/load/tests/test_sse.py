"""Tests for the SSE reader.

The sequences here are copied from the literal ``yield`` statements in
``agents/main_agent/streaming/stream_coordinator.py`` so a protocol change on
the server shows up as a failure here rather than as mystery timeouts in a run.
"""

from __future__ import annotations

from agentcore_load.sse import iter_sse_events


def _events(raw: str):
    return list(iter_sse_events(iter(raw.split("\n"))))


def test_parses_a_complete_turn() -> None:
    raw = (
        'event: message_start\ndata: {"role": "assistant"}\n\n'
        'event: content_block_start\ndata: {"contentBlockIndex": 0, "type": "text"}\n\n'
        'event: content_block_delta\ndata: {"contentBlockIndex": 0, "type": "text", '
        '"text": "Hello"}\n\n'
        'event: content_block_stop\ndata: {"contentBlockIndex": 0}\n\n'
        'event: message_stop\ndata: {"stopReason": "end_turn"}\n\n'
        "event: done\ndata: {}\n\n"
    )
    events = _events(raw)

    assert [event.name for event in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_stop",
        "done",
    ]
    assert events[-2].data["stopReason"] == "end_turn"


def test_first_token_is_the_first_content_delta() -> None:
    raw = (
        'event: message_start\ndata: {"role": "assistant"}\n\n'
        'event: content_block_start\ndata: {"contentBlockIndex": 0, "type": "text"}\n\n'
        'event: content_block_delta\ndata: {"text": "Hi"}\n\n'
    )
    first = next(e for e in _events(raw) if e.is_first_token_candidate)
    assert first.name == "content_block_delta"
    assert first.text == "Hi"


def test_text_accumulates_only_from_string_payloads() -> None:
    # `content_block_delta` also carries tool-input deltas, where `text` is
    # absent. Those must not be counted as response characters.
    raw = (
        'event: content_block_delta\ndata: {"text": "abc"}\n\n'
        'event: content_block_delta\ndata: {"input": "{\\"q\\":1}"}\n\n'
    )
    assert sum(len(event.text) for event in _events(raw)) == 3


def test_keepalive_comments_are_ignored() -> None:
    # app-api emits SSE comments so intermediaries do not cut an idle stream.
    raw = ": keepalive\n\nevent: done\ndata: {}\n\n"
    assert [event.name for event in _events(raw)] == ["done"]


def test_side_channel_after_message_stop_is_still_parsed() -> None:
    # oauth_required and friends legitimately arrive after message_stop, which
    # is why `done` is the terminator rather than message_stop.
    raw = (
        'event: message_stop\ndata: {"stopReason": "end_turn"}\n\n'
        'event: oauth_required\ndata: {"provider": "google"}\n\n'
        "event: done\ndata: {}\n\n"
    )
    assert [event.name for event in _events(raw)] == [
        "message_stop",
        "oauth_required",
        "done",
    ]


def test_malformed_data_yields_empty_dict_instead_of_raising() -> None:
    raw = "event: metadata\ndata: {not json\n\n"
    events = _events(raw)
    assert events[0].name == "metadata"
    assert events[0].data == {}


def test_unterminated_final_event_is_still_emitted() -> None:
    # A stream cut without its trailing blank line must not lose the last
    # event, or a completed turn would be reported as never finishing.
    raw = "event: done\ndata: {}"
    assert [event.name for event in _events(raw)] == ["done"]


def test_error_turn_exposes_stop_reason() -> None:
    raw = 'event: message_stop\ndata: {"stopReason": "error"}\n\nevent: done\ndata: {}\n\n'
    events = _events(raw)
    assert events[0].data["stopReason"] == "error"


def test_multiline_data_is_joined() -> None:
    raw = 'event: metadata\ndata: {"a":\ndata:  1}\n\n'
    assert _events(raw)[0].data == {"a": 1}
