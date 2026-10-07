"""A turn that fails still meters the model calls it completed.

Prod, 2026-10-03: a GPT-6 turn made eight model calls between MCP tool calls.
Seven succeeded; the eighth failed mid-stream with a provider server error and
the turn ended as a force-stop. The provider billed all seven, but the session
row kept ``messageCount`` 0 and ``totalCost`` 0 and the user's cost summary
never moved, because both failure arms of ``StreamCoordinator.stream_response``
(the in-loop ``error`` branch and the outer ``except``) left before the
post-loop block that writes each call's ``C#`` row. Users retry a failed turn in
a new conversation, so every failure left another empty $0 session behind.

These drive ``stream_response`` end to end and stop at the DynamoDB seam: the
real ``store_message_metadata`` runs, and the table, the session-aggregate bump
and the user cost-summary update are fakes that record what reached them.
"""

from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator

SESSION_ID = "sess-fake-failed-turn"
USER_ID = "user-fake-0001"
MODEL_ID = "global.fake.model-v1"

# $1 / $5 per million tokens, so one call of 1,000 in + 100 out is $0.0015.
PRICING = {"inputPricePerMtok": 1.0, "outputPricePerMtok": 5.0, "snapshotAt": "2026-10-01T00:00:00Z"}
CALL_USAGE = {"inputTokens": 1000, "outputTokens": 100, "totalTokens": 1100}
CALL_COST = 0.0015

# Messages already stored before this turn. The turn's calls land at the odd
# positions after it: 5, 7, 9, ...
PRIOR_MESSAGES = 4


class _ProviderServerError(Exception):
    """Stands in for the OpenAI-family mid-stream server fault."""


def _completed_call(text: str) -> List[Dict[str, Any]]:
    return [
        {"event": {"messageStart": {"role": "assistant"}}},
        {"event": {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": text}}}},
        {"event": {"messageStop": {"stopReason": "tool_use"}}},
        {"event": {"metadata": {"usage": dict(CALL_USAGE), "metrics": {"latencyMs": 100}}}},
    ]


def _failing_call_start() -> List[Dict[str, Any]]:
    return [
        {"event": {"messageStart": {"role": "assistant"}}},
        {"event": {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "partial"}}}},
    ]


_RAISE = object()
_FORCE_STOP = {"force_stop": True, "force_stop_reason": "The server had an error while processing your request"}


class _FakeAgent:
    """Yields scripted raw Strands events; ``_RAISE`` raises the provider fault."""

    def __init__(self, events: List[Any]) -> None:
        self.messages = [{"role": "user", "content": [{"text": "hi"}]}]
        self._events = events

    def stream_async(self, prompt: Any) -> AsyncIterator[Dict[str, Any]]:
        events = self._events

        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            for event in events:
                if event is _RAISE:
                    raise _ProviderServerError("The server had an error while processing your request")
                yield event

        return _gen()


class _SessionManager:
    cancelled = False
    message_count = PRIOR_MESSAGES

    async def update_after_turn(self, input_tokens: int, current_messages=None, **_: Any):
        return None


def _wrapper() -> SimpleNamespace:
    return SimpleNamespace(
        model_config=SimpleNamespace(
            model_id=MODEL_ID,
            get_provider=lambda: SimpleNamespace(value="bedrock"),
            long_ttl_static_prefix=lambda: False,
        )
    )


class _Recorded:
    def __init__(self) -> None:
        self.table = MagicMock()
        self.bump_aggregates = AsyncMock()
        self.cost_summary = AsyncMock()
        self.activity = AsyncMock(return_value=True)
        self.synthetic_writes = MagicMock()

    @property
    def cost_rows(self) -> List[Dict[str, Any]]:
        return [
            c.kwargs["Item"]
            for c in self.table.put_item.call_args_list
            if c.kwargs["Item"]["SK"].startswith("C#")
        ]

    def summary_costs(self) -> List[float]:
        return [c.kwargs["message_metadata"].cost["total"] for c in self.cost_summary.await_args_list]

    def session_costs(self) -> List[float]:
        return [c.kwargs["message_metadata"].cost["total"] for c in self.bump_aggregates.await_args_list]


async def _run(
    events: List[Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    coordinator: Optional[StreamCoordinator] = None,
) -> _Recorded:
    monkeypatch.setenv("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", "fake-sessions-metadata")
    recorded = _Recorded()
    coordinator = coordinator or StreamCoordinator()
    coordinator._get_pricing_snapshot = AsyncMock(return_value=PRICING)

    with (
        patch("apis.shared.sessions.metadata.get_dynamodb_table", return_value=recorded.table),
        patch("apis.shared.sessions.metadata._derive_cache_observability", return_value={}),
        patch("apis.shared.sessions.metadata._emit_cache_metrics"),
        patch("apis.shared.sessions.metadata._bump_session_aggregates", recorded.bump_aggregates),
        patch("apis.shared.sessions.metadata._update_cost_summary_async", recorded.cost_summary),
        patch("apis.shared.sessions.metadata._update_project_rollup_async", AsyncMock()),
        patch("apis.shared.sessions.metadata.update_session_activity", recorded.activity),
        patch("apis.shared.costs.pricing_config.get_model_by_model_id", AsyncMock(return_value=None)),
        patch(
            "agents.main_agent.session.session_factory.SessionFactory.create_session_manager",
            return_value=SimpleNamespace(create_message=recorded.synthetic_writes),
        ),
    ):
        async for _sse in coordinator.stream_response(
            agent=_FakeAgent(events),
            prompt="look this up",
            session_manager=_SessionManager(),
            session_id=SESSION_ID,
            user_id=USER_ID,
            main_agent_wrapper=_wrapper(),
        ):
            pass

    return recorded


def _three_calls_then(failure: List[Any]) -> List[Any]:
    return [
        *_completed_call("one"),
        *_completed_call("two"),
        *_completed_call("three"),
        *_failing_call_start(),
        *failure,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        pytest.param([_RAISE], id="exception-out-of-agent-stream"),
        pytest.param([_FORCE_STOP], id="strands-force-stop"),
    ],
)
async def test_calls_completed_before_a_failure_are_metered(failure, monkeypatch):
    recorded = await _run(_three_calls_then(failure), monkeypatch)

    rows = recorded.cost_rows
    assert [row["messageId"] for row in rows] == [5, 7, 9]
    assert all(row["sessionId"] == SESSION_ID for row in rows)
    assert [float(row["cost"]["total"]) for row in rows] == pytest.approx([CALL_COST] * 3)

    # The session's totalCost and the user's quota summary each see all three.
    assert sum(recorded.session_costs()) == pytest.approx(3 * CALL_COST)
    assert sum(recorded.summary_costs()) == pytest.approx(3 * CALL_COST)

    # And the session's activity advanced (messageCount, lastMessageAt).
    recorded.activity.assert_awaited_once()
    # The error reply itself was still persisted, exactly once.
    recorded.synthetic_writes.assert_called_once()


@pytest.mark.asyncio
async def test_the_failed_call_is_metered_when_the_provider_reported_its_usage(monkeypatch):
    failed_usage = {"inputTokens": 1000, "outputTokens": 0, "totalTokens": 1000}
    recorded = await _run(
        _three_calls_then(
            [{"event": {"metadata": {"usage": failed_usage, "metrics": {"latencyMs": 50}}}}, _RAISE]
        ),
        monkeypatch,
    )

    rows = recorded.cost_rows
    # The failed call's row sits where its error reply landed.
    assert [row["messageId"] for row in rows] == [5, 7, 9, 11]
    assert sum(recorded.summary_costs()) == pytest.approx(3 * CALL_COST + 0.001)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        pytest.param([*_failing_call_start(), _RAISE], id="mid-stream"),
        pytest.param([_RAISE], id="before-any-output"),
        pytest.param([_FORCE_STOP], id="force-stop"),
    ],
)
async def test_a_turn_that_fails_on_its_first_call_records_no_cost(failure, monkeypatch):
    recorded = await _run(failure, monkeypatch)

    assert recorded.cost_rows == []
    recorded.bump_aggregates.assert_not_awaited()
    recorded.cost_summary.assert_not_awaited()
    # The turn still happened: the user's message and the error reply are in
    # the conversation, so the session's activity advances once.
    recorded.activity.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_retried_attempt_that_reported_nothing_is_not_metered(monkeypatch):
    """A retry restarts a call that failed before output; its abandoned
    attempt opened a message slot but never reported usage, so it writes no
    row, and the successful retry is metered once — not twice."""
    recorded = await _run(
        [
            *_completed_call("one"),
            {"event": {"messageStart": {"role": "assistant"}}},  # abandoned attempt
            *_completed_call("two, on retry"),
            *_failing_call_start(),
            _RAISE,
        ],
        monkeypatch,
    )

    assert len(recorded.cost_rows) == 2
    assert sum(recorded.summary_costs()) == pytest.approx(2 * CALL_COST)


@pytest.mark.asyncio
async def test_a_coordinator_exception_meters_the_calls_once(monkeypatch):
    """The outer ``except`` arm: a failure in the coordinator's own loop body,
    not in the agent stream."""

    class _Exploding(StreamCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.stops_seen = 0

        def _drain_steering_events(self, main_agent_wrapper: Any, session_id: str):
            # Blow up on the event after the second completed call.
            if self.stops_seen >= 2:
                raise RuntimeError("coordinator bug")
            return []

        def _format_sse_event(self, event: Dict[str, Any]) -> str:
            if event.get("type") == "metadata":
                self.stops_seen += 1
            return super()._format_sse_event(event)

    recorded = await _run(
        [*_completed_call("one"), *_completed_call("two"), *_completed_call("three")],
        monkeypatch,
        coordinator=_Exploding(),
    )

    assert [row["messageId"] for row in recorded.cost_rows] == [5, 7]
    assert sum(recorded.summary_costs()) == pytest.approx(2 * CALL_COST)
    recorded.activity.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_successful_turn_is_metered_exactly_once(monkeypatch):
    """The success path still owns its turn: same rows, no second pass."""
    recorded = await _run(
        [*_completed_call("one"), *_completed_call("two")],
        monkeypatch,
    )

    assert [row["messageId"] for row in recorded.cost_rows] == [5, 7]
    assert sum(recorded.summary_costs()) == pytest.approx(2 * CALL_COST)
    recorded.activity.assert_awaited_once()
