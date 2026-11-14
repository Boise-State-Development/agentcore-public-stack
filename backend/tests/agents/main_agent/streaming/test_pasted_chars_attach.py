"""The composer's paste count reaches the turn's ``C#`` cost row as ``pastedChars``.

A number only (docs/one-pagers/idle-return-context.md): it tells a large paste
apart from context the server injected. Written after the stream on the turn's
last row, beside ``attachmentPlan``, and passed per turn because the agent is
cached and the count is not.
"""

import inspect
from unittest.mock import AsyncMock, patch

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator
from apis.inference_api.chat.models import InvocationRequest
from apis.shared.observability.content_policy import CALL_ROW_PROJECTION
from tests.agents.main_agent.streaming.test_stream_response_signature import (
    _bare_chat_agent,
    _SignatureFaithfulCoordinator,
)


def _coordinator() -> StreamCoordinator:
    return object.__new__(StreamCoordinator)


def _usage():
    return {"usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120}}


async def _store(**kw):
    store = AsyncMock()
    with patch("apis.shared.sessions.metadata.store_message_metadata", store):
        await _coordinator()._store_message_metadata(
            session_id="s1",
            user_id="u1",
            message_id=3,
            accumulated_metadata=_usage(),
            stream_start_time=0.0,
            stream_end_time=1.0,
            first_token_time=0.5,
            **kw,
        )
    return store.await_args.kwargs["message_metadata"]


@pytest.mark.asyncio
async def test_the_count_is_stored_on_the_row():
    stored = await _store(turn_pasted_chars=41_250)
    assert stored.model_dump(by_alias=True)["pastedChars"] == 41_250


@pytest.mark.asyncio
async def test_no_count_means_no_field():
    stored = await _store(turn_pasted_chars=None)
    assert "pastedChars" not in stored.model_dump(by_alias=True)


@pytest.mark.asyncio
async def test_only_the_last_row_of_the_turn_carries_it():
    coordinator = _coordinator()
    seen = []

    async def record(**kwargs):
        seen.append(kwargs.get("turn_pasted_chars"))

    with patch.object(coordinator, "_store_message_metadata", side_effect=record, create=True):
        await coordinator._store_turn_call_metadata(
            session_id="s1",
            user_id="u1",
            main_agent_wrapper=None,
            calls=[(0, 10), (1, 11), (2, 12)],
            per_message_metadata=[_usage(), _usage(), _usage()],
            accumulated_metadata=_usage(),
            stream_start_time=0.0,
            stream_end_time=1.0,
            first_token_time=0.5,
            turn_started_at=0.0,
            citations=None,
            turn_agent_id=None,
            turn_project_id=None,
            turn_pasted_chars=900,
        )
    assert seen == [None, None, 900]


def test_every_turn_row_writer_accepts_it():
    for name in ("stream_response", "_store_turn_call_metadata", "_record_failed_turn_usage", "_persist_interruption", "_store_message_metadata"):
        params = inspect.signature(getattr(StreamCoordinator, name)).parameters
        assert "turn_pasted_chars" in params, name
        assert params["turn_pasted_chars"].default is None, name


@pytest.mark.asyncio
async def test_stream_async_forwards_it_to_the_coordinator():
    coordinator = _SignatureFaithfulCoordinator()
    agent = _bare_chat_agent(coordinator)

    async for _ in agent.stream_async("what does it say?", turn_pasted_chars=12):
        pass

    assert coordinator.captured.get("turn_pasted_chars") == 12


@pytest.mark.parametrize("raw, expected", [(None, None), (0, None), (-5, None), (250, 250)])
def test_the_request_field_drops_a_count_that_is_not_positive(raw, expected):
    request = InvocationRequest(session_id="s1", message="x" * 300, pasted_chars=raw)
    assert request.pasted_chars == expected


def test_the_request_field_never_exceeds_the_message():
    assert InvocationRequest(session_id="s1", message="hello", pasted_chars=9_000).pasted_chars == 5
    assert InvocationRequest(session_id="s1", message="", pasted_chars=9_000).pasted_chars is None


def test_a_request_without_it_is_unchanged():
    assert InvocationRequest(session_id="s1", message="hello").pasted_chars is None


def test_the_cost_row_projection_reads_it():
    assert "pastedChars" in CALL_ROW_PROJECTION
