"""The coordinator's half of the first-token clock (turn-path spec P1b).

`turn_prelude` stops when the agent is ready. Everything after it — the head
of turn, LTM retrieval and the hooks before the model call, the model's own
time to first token — was unmeasured, and a first token that moved because of
any of it was invisible. These pin three things: the coordinator closes those
stages in order on the clock it was handed; the boundary between `pre_model`
and `model` comes from the status hook's stamp; and the `turn_first_token`
line is written only AFTER the first token has reached the consumer, because a
log write in front of the first token is exactly the latency this measures.
"""

from typing import Any, AsyncIterator, Dict, List, Optional

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator


class _Clock:
    """Records each call with how many frames the consumer had received."""

    def __init__(self, received: List[str]) -> None:
        self.calls: List[tuple] = []
        self._received = received

    def mark(self, stage: str) -> None:
        self.calls.append(("mark", stage, len(self._received)))

    def mark_at(self, stage: str, at: float) -> None:
        self.calls.append(("mark_at", stage, at))

    def emit_first_token(self) -> None:
        self.calls.append(("emit_first_token", None, len(self._received)))

    def stages(self) -> List[str]:
        return [c[1] for c in self.calls if c[0] in ("mark", "mark_at")]


class _Hook:
    def __init__(self, stamp: Optional[float]) -> None:
        self.first_model_call_at = stamp

    def drain_statuses(self) -> List[dict]:
        return []

    def drain_batches(self) -> List[dict]:
        return []


class _Wrapper:
    def __init__(self, hook: Optional[_Hook]) -> None:
        if hook is not None:
            self.agent_status_hook = hook


class _SessionManager:
    cancelled = False

    async def update_after_turn(self, input_tokens, current_messages=None):
        return None


class _Agent:
    def __init__(self, events: List[Dict[str, Any]]) -> None:
        self.messages = [{"role": "user", "content": [{"text": "hi"}]}]
        self._events = events

    def stream_async(self, prompt: Any) -> AsyncIterator[Dict[str, Any]]:
        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            for event in self._events:
                yield event

        return _gen()


_TEXT_TURN = [
    {"event": {"messageStart": {"role": "assistant"}}},
    {"event": {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Hello"}}}},
    {"event": {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": " there"}}}},
    {"event": {"contentBlockStop": {"contentBlockIndex": 0}}},
    {"event": {"messageStop": {"stopReason": "end_turn"}}},
]

_HEAD_OF_TURN = [
    "head_of_turn.handoff",
    "head_of_turn.compaction",
    "head_of_turn.history_count",
    "head_of_turn.rest",
]


async def _run(events, wrapper, *, with_clock=True):
    received: List[str] = []
    clock = _Clock(received) if with_clock else None
    async for sse in StreamCoordinator().stream_response(
        agent=_Agent(events),
        prompt="hi",
        session_manager=_SessionManager(),
        session_id="sess-1",
        user_id="user-1",
        main_agent_wrapper=wrapper,
        turn_clock=clock,
    ):
        received.append(sse)
    return clock, received


def _index_of_first_delta(received: List[str], text: str) -> int:
    """The typed frame the SPA renders — not the raw `event: event`
    passthrough the processor also yields ahead of it."""
    return next(
        i
        for i, sse in enumerate(received)
        if sse.startswith("event: content_block_delta") and text in sse
    )


@pytest.mark.asyncio
async def test_stages_close_in_order_and_split_at_the_hook_stamp():
    clock, _ = await _run(_TEXT_TURN, _Wrapper(_Hook(stamp=123.0)))

    assert clock.stages() == [*_HEAD_OF_TURN, "pre_model", "model"]
    assert ("mark_at", "pre_model", 123.0) in clock.calls
    assert [c[0] for c in clock.calls].count("emit_first_token") == 1


@pytest.mark.asyncio
async def test_the_line_is_written_after_the_first_token_reached_the_consumer():
    """The emit must never sit in front of the token it measures."""
    clock, received = await _run(_TEXT_TURN, _Wrapper(_Hook(stamp=1.0)))

    first_token_frame = _index_of_first_delta(received, "Hello")
    model_mark = next(c for c in clock.calls if c[1] == "model")
    emit = next(c for c in clock.calls if c[0] == "emit_first_token")

    # Marked on arrival — before that frame was handed over...
    assert model_mark[2] <= first_token_frame
    # ...and emitted only once the consumer already held it.
    assert emit[2] > first_token_frame


@pytest.mark.asyncio
async def test_without_a_hook_stamp_the_gap_is_not_split():
    clock, _ = await _run(_TEXT_TURN, _Wrapper(_Hook(stamp=None)))

    assert clock.stages() == [*_HEAD_OF_TURN, "pre_model_and_model"]


@pytest.mark.asyncio
async def test_a_tool_first_turn_counts_the_tool_call_as_the_first_token():
    events = [
        {"event": {"messageStart": {"role": "assistant"}}},
        {
            "event": {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {"toolUse": {"toolUseId": "t1", "name": "calculator"}},
                }
            }
        },
        {"event": {"messageStop": {"stopReason": "end_turn"}}},
    ]

    clock, _ = await _run(events, _Wrapper(_Hook(stamp=1.0)))

    assert clock.stages()[-2:] == ["pre_model", "model"]
    assert [c[0] for c in clock.calls].count("emit_first_token") == 1


@pytest.mark.asyncio
async def test_a_turn_with_no_model_output_emits_nothing():
    """No first token, no line — not a line with a made-up number."""
    events = [{"event": {"messageStart": {"role": "assistant"}}}]

    clock, _ = await _run(events, _Wrapper(_Hook(stamp=1.0)))

    assert clock.stages() == _HEAD_OF_TURN
    assert not any(c[0] == "emit_first_token" for c in clock.calls)


@pytest.mark.asyncio
async def test_a_failing_clock_never_breaks_the_turn():
    class _Broken:
        def __getattr__(self, _name):
            raise RuntimeError("clock down")

    received: List[str] = []
    async for sse in StreamCoordinator().stream_response(
        agent=_Agent(_TEXT_TURN),
        prompt="hi",
        session_manager=_SessionManager(),
        session_id="sess-1",
        user_id="user-1",
        main_agent_wrapper=_Wrapper(_Hook(stamp=1.0)),
        turn_clock=_Broken(),
    ):
        received.append(sse)

    assert any("Hello" in sse for sse in received)
