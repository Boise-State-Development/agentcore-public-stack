"""A model call that spent tokens but priced to nothing must be visible.

Such a call is written with no cost, so neither the cost rollups nor the user's
quota ever see it. Prod ran months of $0 Haiku (a hard-coded fallback id with no
catalog row) and Nova Sonic (no catalog row at all) before anyone read the rollup
table. ``UnmeteredModelCall`` is the metric an alarm can watch instead.
"""

from __future__ import annotations

import io
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator


def _capture_emf(fn, *args, **kwargs) -> list[dict]:
    from apis.shared.observability import emf

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    emf._emf_logger.addHandler(handler)
    try:
        fn(*args, **kwargs)
    finally:
        emf._emf_logger.removeHandler(handler)
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_the_record_is_a_countable_emf_metric_with_the_model_as_a_property():
    from apis.shared.observability.emf import emit_unmetered_model_call

    [record] = _capture_emf(
        emit_unmetered_model_call, "us.anthropic.claude-haiku-4-5-20251001-v1:0", "no_pricing", surface="chat",
        session_id="s-1",
    )

    directive = record["_aws"]["CloudWatchMetrics"][0]
    assert directive["Namespace"] == "AgentCoreStack/PromptCache"
    assert directive["Dimensions"] == [[]]
    assert directive["Metrics"] == [{"Name": "UnmeteredModelCall", "Unit": "Count"}]
    assert record["UnmeteredModelCall"] == 1
    assert record["modelId"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert record["unmeteredReason"] == "no_pricing"
    assert record["surface"] == "chat"
    assert record["sessionId"] == "s-1"


PRICING = {"inputPricePerMtok": 1.0, "outputPricePerMtok": 5.0, "snapshotAt": "2026-09-24T00:00:00Z"}


def _agent(model_id: str):
    return SimpleNamespace(
        model_config=SimpleNamespace(
            model_id=model_id,
            get_provider=lambda: SimpleNamespace(value="bedrock"),
            long_ttl_static_prefix=lambda: False,
        )
    )


async def _store(coordinator: StreamCoordinator, agent) -> None:
    await coordinator._store_message_metadata(
        session_id="s-1",
        user_id="u-1",
        message_id=1,
        accumulated_metadata={"usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120}},
        stream_start_time=0.0,
        stream_end_time=1.0,
        first_token_time=0.5,
        agent=agent,
        call_index=0,
    )


@pytest.mark.asyncio
async def test_a_chat_call_with_no_pricing_row_emits_the_metric():
    coordinator = object.__new__(StreamCoordinator)
    coordinator._get_pricing_snapshot = AsyncMock(return_value=None)
    emit = MagicMock()
    with (
        patch("apis.shared.sessions.metadata.store_message_metadata", AsyncMock()),
        patch("apis.shared.costs.pricing_config.get_model_by_model_id", AsyncMock(return_value=None)),
        patch("apis.shared.observability.emf.emit_unmetered_model_call", emit),
    ):
        await _store(coordinator, _agent("us.unpriced"))

    emit.assert_called_once_with("us.unpriced", "no_pricing", surface="chat", session_id="s-1")


@pytest.mark.asyncio
async def test_a_priced_chat_call_emits_nothing():
    coordinator = object.__new__(StreamCoordinator)
    coordinator._get_pricing_snapshot = AsyncMock(return_value=PRICING)
    emit = MagicMock()
    store = AsyncMock()
    with (
        patch("apis.shared.sessions.metadata.store_message_metadata", store),
        patch("apis.shared.costs.pricing_config.get_model_by_model_id", AsyncMock(return_value=None)),
        patch("apis.shared.observability.emf.emit_unmetered_model_call", emit),
    ):
        await _store(coordinator, _agent("global.priced"))

    # The row was written, with a cost — so the silence below is the check passing.
    assert store.await_args.kwargs["message_metadata"].cost["total"] == pytest.approx(0.0002)
    emit.assert_not_called()


@pytest.mark.asyncio
async def test_a_priced_call_the_calculator_fails_on_says_so():
    coordinator = object.__new__(StreamCoordinator)
    coordinator._get_pricing_snapshot = AsyncMock(return_value=PRICING)
    coordinator._calculate_message_cost = MagicMock(return_value=None)
    emit = MagicMock()
    with (
        patch("apis.shared.sessions.metadata.store_message_metadata", AsyncMock()),
        patch("apis.shared.costs.pricing_config.get_model_by_model_id", AsyncMock(return_value=None)),
        patch("apis.shared.observability.emf.emit_unmetered_model_call", emit),
    ):
        await _store(coordinator, _agent("global.priced"))

    emit.assert_called_once_with("global.priced", "calculation_failed", surface="chat", session_id="s-1")


def _voice_agent():
    return SimpleNamespace(
        turn_count=1,
        response_start_count=1,
        accumulated_usage={"inputTokens": 500, "outputTokens": 300},
        per_turn_usage=[],
        voice_model_id="amazon.nova-2-sonic-v1:0",
        session_manager=SimpleNamespace(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("pricing, expect_emit", [(None, True), ({"inputPricePerMtok": 3.0, "outputPricePerMtok": 12.0}, False)])
async def test_a_voice_session_with_no_pricing_row_emits_the_metric(pricing, expect_emit):
    from apis.inference_api.chat import voice_routes

    emit = MagicMock()
    store = AsyncMock()
    with (
        patch.object(voice_routes, "get_session_metadata", AsyncMock(return_value=None)),
        patch("apis.shared.sessions.metadata.store_message_metadata", store),
        patch("apis.shared.costs.pricing_config.get_model_pricing", AsyncMock(return_value=pricing)),
        patch("apis.shared.observability.emf.emit_unmetered_model_call", emit),
    ):
        await voice_routes._finalize_voice_session("s-1", "u-1", _voice_agent())

    store.assert_awaited_once()

    if expect_emit:
        emit.assert_called_once_with("amazon.nova-2-sonic-v1:0", "no_pricing", surface="voice", session_id="s-1")
    else:
        emit.assert_not_called()
