"""A turn that fails or is interrupted still meters the model calls it made.

Prod, 2026-10-03: a GPT-6 turn made eight model calls between MCP tool calls.
Seven succeeded; the eighth failed mid-stream with a provider server error and
the turn ended as a force-stop. The provider billed all seven, but the session
row kept ``messageCount`` 0 and ``totalCost`` 0 and the user's cost summary
never moved, because both failure arms of ``StreamCoordinator.stream_response``
(the in-loop ``error`` branch and the outer ``except``) left before the
post-loop block that writes each call's ``C#`` row. Users retry a failed turn in
a new conversation, so every failure left another empty $0 session behind.

A Stop or a disconnect had the same gap: ``_persist_interruption`` wrote at most
one row, from the turn-level usage, which holds only the most recent call's, so
every call completed earlier in the turn went unmetered.

These drive ``stream_response`` end to end and stop at the DynamoDB seam: the
real ``store_message_metadata`` runs, and the table, the session-aggregate bump
and the user cost-summary update are fakes that record what reached them.
"""

import asyncio
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
_STOP = object()  # the user's Stop, observed through the session lease
_DISCONNECT = object()  # the client's socket going away mid-stream
_FORCE_STOP = {"force_stop": True, "force_stop_reason": "The server had an error while processing your request"}


class _SessionManager:
    message_count = PRIOR_MESSAGES

    def __init__(self) -> None:
        self.cancelled = False

    async def update_after_turn(self, input_tokens: int, current_messages=None, **_: Any):
        return None


class _FakeAgent:
    """Yields scripted raw Strands events.

    ``_RAISE`` raises the provider fault, ``_STOP`` arms the lease's cancel (the
    coordinator sees it on the next event), ``_DISCONNECT`` raises the
    ``CancelledError`` a dropped socket delivers. ``tail_role`` is the role of
    the last committed message: ``user`` while a model call is in flight (the
    turn's prompt or its tool results), ``assistant`` while tools run.
    """

    def __init__(self, events: List[Any], session_manager: _SessionManager, tail_role: str) -> None:
        self.messages = [{"role": tail_role, "content": [{"text": "hi"}]}]
        self._events = events
        self._session_manager = session_manager

    def stream_async(self, prompt: Any) -> AsyncIterator[Dict[str, Any]]:
        events = self._events
        session_manager = self._session_manager

        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            for event in events:
                if event is _RAISE:
                    raise _ProviderServerError("The server had an error while processing your request")
                if event is _DISCONNECT:
                    raise asyncio.CancelledError()
                if event is _STOP:
                    session_manager.cancelled = True
                    # The cancel is observed on the next event the agent yields.
                    yield {"event": {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "…"}}}}
                    continue
                yield event

        return _gen()


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
    tail_role: str = "user",
    projected_input_tokens: Optional[int] = None,
    expect: Optional[type] = None,
    recorded: Optional[_Recorded] = None,
) -> _Recorded:
    monkeypatch.setenv("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", "fake-sessions-metadata")
    recorded = recorded or _Recorded()
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
        patch("apis.shared.sessions.metadata.set_interrupted_turn", AsyncMock()),
        patch(
            "agents.main_agent.session.hooks.context_attribution.get_projected_input_tokens",
            return_value=projected_input_tokens,
        ),
    ):
        session_manager = _SessionManager()

        async def _drive() -> None:
            async for _sse in coordinator.stream_response(
                agent=_FakeAgent(events, session_manager, tail_role),
                prompt="look this up",
                session_manager=session_manager,
                session_id=SESSION_ID,
                user_id=USER_ID,
                main_agent_wrapper=_wrapper(),
            ):
                pass

        if expect is not None:
            with pytest.raises(expect):
                await _drive()
        else:
            await _drive()

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


# ---------------------------------------------------------------------------
# Interrupted turns: a user Stop (cooperative, via the session lease) or a
# client disconnect (CancelledError). Both go through `_persist_interruption`.
# ---------------------------------------------------------------------------

# 2,000 projected input tokens at $1/M: the in-flight call's input-side cost.
PROJECTED_TOKENS = 2000
PROJECTED_COST = 0.002


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interruption, expect",
    [
        pytest.param(_STOP, None, id="user-stop"),
        pytest.param(_DISCONNECT, asyncio.CancelledError, id="disconnect"),
    ],
)
async def test_an_interrupted_turn_meters_every_completed_call_and_the_one_in_flight(
    interruption, expect, monkeypatch
):
    recorded = await _run(
        _three_calls_then([interruption]),
        monkeypatch,
        projected_input_tokens=PROJECTED_TOKENS,
        expect=expect,
    )

    rows = recorded.cost_rows
    # Calls 0-2 at their own ids; the cut call at the id its partial reply
    # was persisted at, priced from the projected input.
    assert [row["messageId"] for row in rows] == [5, 7, 9, 11]
    assert sum(recorded.session_costs()) == pytest.approx(3 * CALL_COST + PROJECTED_COST)
    assert sum(recorded.summary_costs()) == pytest.approx(3 * CALL_COST + PROJECTED_COST)
    recorded.activity.assert_awaited_once()
    recorded.synthetic_writes.assert_called_once()


@pytest.mark.asyncio
async def test_a_stop_during_tool_execution_meters_the_completed_calls_only(monkeypatch):
    """The tail is the assistant's tool request, so no model call is in
    flight: the projection would describe the last completed call, which is
    already metered, so it must not be used."""
    recorded = await _run(
        [*_completed_call("one"), *_completed_call("two"), _STOP],
        monkeypatch,
        tail_role="assistant",
        projected_input_tokens=PROJECTED_TOKENS,
    )

    assert [row["messageId"] for row in recorded.cost_rows] == [5, 7]
    assert sum(recorded.summary_costs()) == pytest.approx(2 * CALL_COST)
    # Nothing synthetic is persisted after an assistant tail.
    recorded.synthetic_writes.assert_not_called()


@pytest.mark.asyncio
async def test_a_stop_before_the_first_token_meters_the_projected_input(monkeypatch):
    """The pre-existing case: the first call was cut before it streamed or
    reported anything, so its input side comes from the projection."""
    recorded = await _run(
        [_STOP],
        monkeypatch,
        projected_input_tokens=PROJECTED_TOKENS,
    )

    assert [row["messageId"] for row in recorded.cost_rows] == [5]
    assert sum(recorded.summary_costs()) == pytest.approx(PROJECTED_COST)


@pytest.mark.asyncio
async def test_a_stop_with_no_usage_and_no_projection_records_no_cost(monkeypatch):
    recorded = await _run([*_failing_call_start(), _STOP], monkeypatch)

    assert recorded.cost_rows == []
    recorded.cost_summary.assert_not_awaited()
    recorded.activity.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_disconnect_during_the_success_writes_does_not_meter_again(monkeypatch):
    """The client goes away while the success block is writing its rows. The
    disconnect arm now meters completed calls whatever the history tail, so
    without `turn_usage_recorded` it would write this turn's rows again."""
    recorded = _Recorded()
    # The second row's aggregate bump is where the cancellation lands.
    recorded.bump_aggregates.side_effect = [None, asyncio.CancelledError()]

    result = await _run(
        [*_completed_call("one"), *_completed_call("two")],
        monkeypatch,
        tail_role="assistant",  # the turn finished answering
        projected_input_tokens=PROJECTED_TOKENS,
        expect=asyncio.CancelledError,
        recorded=recorded,
    )

    # The success block wrote both rows before the second aggregate bump was
    # cancelled; the disconnect arm wrote none of its own.
    assert [row["messageId"] for row in result.cost_rows] == [5, 7]
    assert result.cost_summary.await_count == 1
    result.activity.assert_awaited_once()
