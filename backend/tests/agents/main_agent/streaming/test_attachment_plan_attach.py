"""The turn's attachment plan reaches its ``C#`` cost row as ``attachmentPlan``.

``docs/specs/unified-attachment-delivery.md`` §8.2: per file, ``mode``, ``class``,
``tokens`` and ``reason`` (plus the upload id), written after the stream with the
row's other fields, on the turn's last row only — the one row per turn a count
reads. Passed per turn like ``turn_agent_id``: the agent is cached, the plan is not.
"""

import inspect
from unittest.mock import AsyncMock, patch

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator
from tests.agents.main_agent.streaming.test_stream_response_signature import (
    _bare_chat_agent,
    _SignatureFaithfulCoordinator,
)

PLAN = {
    "profile": "legacy",
    "files": [{"mode": "native", "class": "pdf", "tokens": 1200, "reason": "inline", "uploadId": "up-1"}],
}


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
async def test_the_plan_is_stored_on_the_row():
    stored = await _store(turn_attachment_plan=PLAN)
    assert stored.model_dump(by_alias=True)["attachmentPlan"] == PLAN


@pytest.mark.asyncio
async def test_no_plan_means_no_field():
    stored = await _store(turn_attachment_plan=None)
    assert "attachmentPlan" not in stored.model_dump(by_alias=True)


@pytest.mark.asyncio
async def test_only_the_last_row_of_the_turn_carries_it():
    coordinator = _coordinator()
    seen = []

    async def record(**kwargs):
        seen.append(kwargs.get("turn_attachment_plan"))

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
            turn_attachment_plan=PLAN,
        )
    assert seen == [None, None, PLAN]


def test_every_turn_row_writer_accepts_it():
    for name in ("stream_response", "_store_turn_call_metadata", "_record_failed_turn_usage", "_persist_interruption", "_store_message_metadata"):
        params = inspect.signature(getattr(StreamCoordinator, name)).parameters
        assert "turn_attachment_plan" in params, name
        assert params["turn_attachment_plan"].default is None, name


@pytest.mark.asyncio
async def test_stream_async_forwards_it_to_the_coordinator():
    coordinator = _SignatureFaithfulCoordinator()
    agent = _bare_chat_agent(coordinator)

    async for _ in agent.stream_async("what does it say?", turn_attachment_plan=PLAN):
        pass

    assert coordinator.captured.get("turn_attachment_plan") == PLAN
