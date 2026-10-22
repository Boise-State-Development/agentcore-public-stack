"""The coordinator → cost-row seam for the tool census.

`_store_message_metadata` gained a `tool_calls` argument; the tally it
receives must land on the persisted `MessageMetadata` as the `toolCalls`
extra field (the same mechanism `turnAgentId` uses), and must be *absent* —
not an empty dict — when the call requested no tools, so the profile's
coverage flag stays honest.
"""

from unittest.mock import AsyncMock, patch

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator


def _coordinator() -> StreamCoordinator:
    return object.__new__(StreamCoordinator)


def _usage_metadata():
    return {"usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120}}


@pytest.mark.asyncio
async def test_tool_calls_are_attached_to_the_stored_metadata():
    store = AsyncMock()
    with patch("apis.shared.sessions.metadata.store_message_metadata", store):
        await _coordinator()._store_message_metadata(
            session_id="s1",
            user_id="u1",
            message_id=3,
            accumulated_metadata=_usage_metadata(),
            stream_start_time=0.0,
            stream_end_time=1.0,
            first_token_time=0.5,
            agent=None,
            call_index=0,
            tool_calls={"list_courses": {"calls": 2, "errors": 0}},
        )

    store.assert_awaited_once()
    stored = store.await_args.kwargs["message_metadata"]
    assert stored.model_extra["toolCalls"] == {"list_courses": {"calls": 2, "errors": 0}}
    # And it serializes onto the row exactly as the profile reads it.
    assert stored.model_dump(by_alias=True)["toolCalls"]["list_courses"]["calls"] == 2


@pytest.mark.asyncio
async def test_no_tools_means_no_field_at_all():
    store = AsyncMock()
    with patch("apis.shared.sessions.metadata.store_message_metadata", store):
        await _coordinator()._store_message_metadata(
            session_id="s1",
            user_id="u1",
            message_id=3,
            accumulated_metadata=_usage_metadata(),
            stream_start_time=0.0,
            stream_end_time=1.0,
            first_token_time=None,
            agent=None,
            call_index=0,
            tool_calls=None,
        )

    stored = store.await_args.kwargs["message_metadata"]
    assert "toolCalls" not in (stored.model_extra or {})
    assert "toolCalls" not in stored.model_dump(by_alias=True)


@pytest.mark.asyncio
async def test_the_argument_is_optional_for_the_interrupt_path():
    # `_persist_interruption` calls without tool_calls; the default must hold.
    store = AsyncMock()
    with patch("apis.shared.sessions.metadata.store_message_metadata", store):
        await _coordinator()._store_message_metadata(
            session_id="s1",
            user_id="u1",
            message_id=1,
            accumulated_metadata=_usage_metadata(),
            stream_start_time=0.0,
            stream_end_time=1.0,
            first_token_time=None,
        )
    store.assert_awaited_once()
