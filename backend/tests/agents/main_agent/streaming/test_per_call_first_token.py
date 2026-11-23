"""Each ``C#`` row carries its own call's first-byte time, never another call's.

Dev, 2026-11-21/22: pairs of cost rows from one tool turn carried the same
``latency.timeToFirstToken`` (40,170 ms on both), and on the first row it was
larger than that row's own ``endToEndLatency`` (3,891 ms), which no real
measurement can be. The persist loop "enriched" the first call's row with the
turn-level ``accumulated_metadata["metrics"]["timeToFirstByteMs"]`` whenever
the call had none of its own, but that dict is last-write-wins across calls,
so it held the LAST call's figure. A tool-only first call (no text delta, so
nothing measured) took the answering call's long first-byte time. Those rows
feed the release ledger's ``meanFirstTokenMs``.

These drive ``stream_response`` end to end with the harness from
``test_unfinished_turn_metering`` and a clock the scripted agent advances, so
each call's first text delta lands at a known offset from its own start.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List

import pytest

from agents.main_agent.streaming import stream_coordinator, stream_processor
from tests.agents.main_agent.streaming import test_unfinished_turn_metering as harness

CALL_USAGE = harness.CALL_USAGE


class _Tick:
    """Advance the fake clock by ``seconds`` before the next event."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def time(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    # Only the streaming modules' clocks: both read nothing but `time.time()`.
    monkeypatch.setattr(stream_coordinator, "time", SimpleNamespace(time=fake.time))
    monkeypatch.setattr(stream_processor, "time", SimpleNamespace(time=fake.time))

    class _TickingAgent(harness._FakeAgent):
        def stream_async(self, prompt: Any) -> AsyncIterator[Dict[str, Any]]:
            events = self._events

            async def _gen() -> AsyncIterator[Dict[str, Any]]:
                for event in events:
                    if isinstance(event, _Tick):
                        fake.now += event.seconds
                        continue
                    yield event

            return _gen()

    monkeypatch.setattr(harness, "_FakeAgent", _TickingAgent)
    return fake


def _metadata(latency_ms: int, ttfb_ms: int | None = None) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {"latencyMs": latency_ms}
    if ttfb_ms is not None:
        metrics["timeToFirstByteMs"] = ttfb_ms
    return {"event": {"metadata": {"usage": dict(CALL_USAGE), "metrics": metrics}}}


def _tool_only_call(latency_ms: int, ttfb_ms: int | None = None) -> List[Any]:
    """A call that streams only a tool use: no text delta, so no local TTFT."""
    return [
        {"event": {"messageStart": {"role": "assistant"}}},
        _Tick(0.5),
        {
            "event": {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {"toolUse": {"toolUseId": "tool-1", "name": "lookup"}},
                }
            }
        },
        {"event": {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": "{}"}}}}},
        {"event": {"contentBlockStop": {"contentBlockIndex": 0}}},
        {"event": {"messageStop": {"stopReason": "tool_use"}}},
        _metadata(latency_ms, ttfb_ms),
    ]


def _text_call(first_token_after: float, latency_ms: int, stop: str, ttfb_ms: int | None = None) -> List[Any]:
    return [
        {"event": {"messageStart": {"role": "assistant"}}},
        _Tick(first_token_after),
        {"event": {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "words"}}}},
        _Tick(0.25),
        {"event": {"messageStop": {"stopReason": stop}}},
        _metadata(latency_ms, ttfb_ms),
    ]


def _first_token(row: Dict[str, Any]) -> Any:
    return row["latency"].get("timeToFirstToken")


def _assert_never_after_its_own_end(rows: List[Dict[str, Any]]) -> None:
    for row in rows:
        ttft = _first_token(row)
        if ttft is not None:
            assert ttft <= row["latency"]["endToEndLatency"], row["messageId"]


@pytest.mark.asyncio
async def test_each_call_keeps_its_own_measured_first_token(clock, monkeypatch):
    recorded = await harness._run(
        [
            *_text_call(0.25, latency_ms=1_000, stop="tool_use"),
            _Tick(2.0),  # the tool runs
            *_text_call(0.75, latency_ms=2_000, stop="end_turn"),
        ],
        monkeypatch,
    )

    rows = recorded.cost_rows
    assert [row["messageId"] for row in rows] == [5, 7]
    assert [_first_token(row) for row in rows] == [250, 750]
    _assert_never_after_its_own_end(rows)


@pytest.mark.asyncio
async def test_each_call_keeps_the_providers_per_call_figure(clock, monkeypatch):
    recorded = await harness._run(
        [
            *_tool_only_call(latency_ms=900, ttfb_ms=310),
            _Tick(2.0),
            *_text_call(0.75, latency_ms=2_000, stop="end_turn", ttfb_ms=880),
        ],
        monkeypatch,
    )

    assert [_first_token(row) for row in recorded.cost_rows] == [310, 880]
    _assert_never_after_its_own_end(recorded.cost_rows)


@pytest.mark.asyncio
async def test_a_tool_only_first_call_never_takes_the_answering_calls_figure(clock, monkeypatch):
    """The dev shape: a quick tool call, then a long-thinking answer."""
    recorded = await harness._run(
        [
            *_tool_only_call(latency_ms=3_891),
            _Tick(1.0),
            *_text_call(40.0, latency_ms=41_000, stop="end_turn"),
        ],
        monkeypatch,
    )

    rows = recorded.cost_rows
    assert [row["messageId"] for row in rows] == [5, 7]
    # Nothing was measured for the first call, so it says so ...
    assert _first_token(rows[0]) is None
    # ... and the answering call keeps its own.
    assert _first_token(rows[1]) == 40_000
    _assert_never_after_its_own_end(rows)


@pytest.mark.asyncio
async def test_the_live_turn_level_first_token_is_unchanged(clock, monkeypatch):
    """The final SSE `metadata` frame's figure is the turn's, from its start."""
    recorded = await harness._run(
        [
            *_tool_only_call(latency_ms=3_891),
            _Tick(1.0),
            *_text_call(40.0, latency_ms=41_000, stop="end_turn"),
        ],
        monkeypatch,
    )

    frames = [
        json.loads(frame.split("data: ", 1)[1])
        for frame in recorded.sse
        if frame.startswith("event: metadata\n")
    ]
    final = frames[-1]
    # The turn's first delta of any kind is call 0's tool use, 0.5 s in.
    assert final["metrics"]["timeToFirstByteMs"] == 500
