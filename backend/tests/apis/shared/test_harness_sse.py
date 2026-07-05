"""Tests for the harness SSE reader/accumulator (headless run spike).

Event payload shapes below are verbatim captures from a live dev-ai
`/invocations` stream (2026-07-05, spike run `run-8f10d164cff9`), so the
accumulator is pinned to what the runtime actually emits — including the
stream-processor's nested `tool_use` passthrough whose `input` is a
partial JSON *string* re-emitted as the model streams arguments, and the
message-shaped `tool_result`.
"""

import pytest

from apis.shared.harness.sse import InvocationStreamAccumulator, iter_sse_events


async def _aiter(lines):
    for line in lines:
        yield line


@pytest.mark.asyncio
async def test_iter_sse_events_parses_named_and_bare_data_events():
    lines = [
        "event: message_start",
        'data: {"role": "assistant"}',
        "",
        # Bare data event (event_formatter path) — name comes from `type`.
        'data: {"type": "session_title", "title": "T"}',
        "",
        "event: done",
        "data: {}",
        "",
    ]
    events = [pair async for pair in iter_sse_events(_aiter(lines))]
    assert events == [
        ("message_start", {"role": "assistant"}),
        ("session_title", {"type": "session_title", "title": "T"}),
        ("done", {}),
    ]


@pytest.mark.asyncio
async def test_iter_sse_events_survives_unparseable_payload():
    lines = ["event: event", "data: {not json", "", "event: done", "data: {}", ""]
    events = [pair async for pair in iter_sse_events(_aiter(lines))]
    assert events[0][0] == "_unparseable"
    assert events[-1] == ("done", {})


def _drive_turn_events():
    """A tool-use turn in the shapes the runtime actually emits."""
    return [
        ("message_start", {"role": "assistant"}),
        # Streamed partial tool input: same id re-emitted with growing input.
        (
            "tool_use",
            {"tool_use": {"name": "search_classes", "tool_use_id": "t1", "input": ""}},
        ),
        (
            "tool_use",
            {
                "tool_use": {
                    "name": "search_classes",
                    "tool_use_id": "t1",
                    "input": '{"subject": "CO',
                }
            },
        ),
        (
            "tool_use",
            {
                "tool_use": {
                    "name": "search_classes",
                    "tool_use_id": "t1",
                    "input": '{"subject": "COMM", "min_credits": 3}',
                }
            },
        ),
        ("message_stop", {"stopReason": "tool_use"}),
        (
            "tool_result",
            {
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "toolResult": {
                                "status": "success",
                                "toolUseId": "t1",
                                "content": [{"text": '{"total_results": 87}'}],
                            }
                        }
                    ],
                }
            },
        ),
        ("message_start", {"role": "assistant"}),
        ("content_block_delta", {"contentBlockIndex": 0, "type": "text", "text": "Found "}),
        ("content_block_delta", {"contentBlockIndex": 0, "type": "text", "text": "87 classes."}),
        ("message_stop", {"stopReason": "end_turn"}),
        (
            "metadata",
            {"usage": {"inputTokens": 5, "outputTokens": 215, "totalTokens": 10763}},
        ),
        ("session_title", {"type": "session_title", "title": "COMM Search"}),
        ("done", {}),
    ]


def test_accumulator_folds_streamed_tool_use_into_one_entry():
    acc = InvocationStreamAccumulator()
    for name, payload in _drive_turn_events():
        acc.handle(name, payload)

    assert acc.done is True
    assert acc.stop_reason == "end_turn"
    assert acc.final_message == "Found 87 classes."
    assert acc.title == "COMM Search"

    assert len(acc.tool_trace) == 1
    entry = acc.tool_trace[0]
    assert entry.tool_use_id == "t1"
    assert entry.name == "search_classes"
    assert entry.input == {"subject": "COMM", "min_credits": 3}
    assert entry.result_preview == '{"total_results": 87}'
    assert entry.is_error is False

    usage = acc.finalize_usage()
    assert usage["usage"]["totalTokens"] == 10763


def test_accumulator_flat_tool_shapes_and_errors():
    acc = InvocationStreamAccumulator()
    acc.handle("tool_use", {"toolUseId": "t2", "name": "calc", "input": {"x": 1}})
    acc.handle("tool_result", {"toolUseId": "t2", "result": "boom", "status": "error"})
    acc.handle("stream_error", {"message": "model exploded"})

    assert acc.tool_trace[0].input == {"x": 1}
    assert acc.tool_trace[0].is_error is True
    assert acc.tool_trace[0].result_preview == "boom"
    assert acc.error == "model exploded"
    assert acc.done is False


def test_accumulator_oauth_required_and_final_message_fallback():
    acc = InvocationStreamAccumulator()
    acc.handle("message_start", {"role": "assistant"})
    acc.handle(
        "content_block_delta", {"contentBlockIndex": 0, "type": "text", "text": "partial"}
    )
    acc.handle(
        "oauth_required",
        {"providerId": "google-drive", "authorizationUrl": "https://consent"},
    )
    # No message_stop — unterminated buffer must still surface.
    assert acc.final_message == "partial"
    assert acc.oauth_required[0].provider_id == "google-drive"
