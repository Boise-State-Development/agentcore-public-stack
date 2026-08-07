"""Replay a real `/chat/stream` response through the agent dialect.

Everything else in `test_agent_events.py` is a fixture written from the SPA's
`stream-parser-types.ts` and the SSE table in `CLAUDE.MD`. Those describe the
events the *SPA* handles, which is not the same set as the events the server
*sends* — the SPA silently drops the rest through its `switch` default. That gap
is how `init_event_loop` / `start_event_loop` reached `UnknownEvent` in the first
place.

So this file asserts against bytes captured off the wire: one turn on
claude-haiku-4-5 with the `calculator` tool enabled, taken during the CLI
device-auth end-to-end verification. Its value is precisely that nobody wrote it.

The capture exercises three properties that are each a bug when inverted, and it
is a better witness than a fixture for all three because the shapes are real:

* 17 of its 41 frames are Strands passthrough (`event` x14, `message` x3).
  Consuming them doubles the answer.
* `metadata` arrives 3 times for one turn. Only the last is current context.
* Two assistant messages open and close, so the answer is the LAST non-empty
  one, not the concatenation.

If the server's dialect changes, this test failing is the intended alarm.
Re-capture with `curl -N .../chat/stream` rather than editing the file by hand.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcore_tui.client.agent_events import (
    AgentTurnAccumulator,
    IgnoredEvent,
    UnknownEvent,
    parse_agent_event,
)

FIXTURE = Path(__file__).parent / "fixtures" / "live_agent_stream.sse"


def _frames(raw: str) -> list[tuple[str, str]]:
    """Split an SSE body into (event-name, data) pairs."""
    out: list[tuple[str, str]] = []
    for block in raw.split("\n\n"):
        name = ""
        data: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if name or data:
            out.append((name, "\n".join(data)))
    return out


@pytest.fixture(scope="module")
def replayed() -> tuple[AgentTurnAccumulator, list[str], list[IgnoredEvent]]:
    """Fold the whole capture once; report unknown names and dropped frames."""
    acc = AgentTurnAccumulator()
    unknown: list[str] = []
    dropped: list[IgnoredEvent] = []
    for name, data in _frames(FIXTURE.read_text()):
        event = parse_agent_event(name, data)
        if isinstance(event, UnknownEvent):
            unknown.append(name or "<bare>")
        elif isinstance(event, IgnoredEvent):
            dropped.append(event)
        acc.apply(event)
    return acc, unknown, dropped


class TestLiveCaptureReplay:
    def test_no_frame_is_unknown(self, replayed: tuple[AgentTurnAccumulator, list[str], list[IgnoredEvent]]) -> None:
        """The assertion that found the lifecycle-frame gap.

        An `UnknownEvent` here means the server sends something this client
        cannot name — which is the one failure fixtures structurally cannot
        surface.
        """
        _, unknown, _ = replayed
        assert unknown == []

    def test_passthrough_frames_dominate_and_are_all_dropped(self, replayed: tuple[AgentTurnAccumulator, list[str], list[IgnoredEvent]]) -> None:
        """17 of 41 frames restate content that typed events already carry.

        Real proportions, not a fixture's: nearly half the stream is duplicate,
        so getting this wrong doubles every answer.
        """
        _, _, dropped = replayed
        passthrough = [d for d in dropped if d.reason == "passthrough"]
        assert len(passthrough) == 17
        assert sorted({d.name for d in passthrough}) == ["event", "message"]

    def test_lifecycle_frames_are_dropped_for_a_different_reason(self, replayed: tuple[AgentTurnAccumulator, list[str], list[IgnoredEvent]]) -> None:
        """These three arrived as UnknownEvent before the live replay existed."""
        _, _, dropped = replayed
        lifecycle = [d for d in dropped if d.reason == "lifecycle"]
        assert [d.name for d in lifecycle] == [
            "init_event_loop",
            "start_event_loop",
            "start_event_loop",
        ]

    def test_the_answer_is_the_last_message_not_the_concatenation(self, replayed: tuple[AgentTurnAccumulator, list[str], list[IgnoredEvent]]) -> None:
        """4172 * 39. Two messages opened; only the second holds the answer."""
        acc, _, _ = replayed
        assert acc.text == "162708"

    def test_the_tool_call_folded_completely(self, replayed: tuple[AgentTurnAccumulator, list[str], list[IgnoredEvent]]) -> None:
        """Arguments arrive as `toolUse` input deltas and need reassembling."""
        acc, _, _ = replayed
        assert len(acc.tool_calls) == 1
        call = acc.tool_calls[0]
        assert call.name == "calculator"
        assert call.arguments == {"expression": "4172 * 39"}
        assert call.result is not None and "162708" in call.result
        assert call.finished
        assert not call.is_error

    def test_turn_completed_cleanly(self, replayed: tuple[AgentTurnAccumulator, list[str], list[IgnoredEvent]]) -> None:
        acc, _, _ = replayed
        assert acc.finished
        assert acc.ok
        assert acc.error is None
        assert acc.stop_reason == "end_turn"
        assert not acc.truncated
        assert not acc.interrupted
        assert not acc.blocked

    def test_title_arrives_on_the_stream(self, replayed: tuple[AgentTurnAccumulator, list[str], list[IgnoredEvent]]) -> None:
        """It lands early here, but the dialect must not assume that."""
        acc, _, _ = replayed
        assert acc.title == "Calculator Compute 4172 * 39"

    def test_usage_is_current_context_not_the_turn_total(self, replayed: tuple[AgentTurnAccumulator, list[str], list[IgnoredEvent]]) -> None:
        """The capture carries NO `metadata_summary`, and that is deliberate.

        `stream_coordinator.py` swallows it (`continue`, ~line 378) because
        Strands' `accumulated_usage` sums each call's full context and so
        overstates occupancy. It sends a final per-call `metadata` instead.

        So `usage` here is the last call's context (3360), not the turn's summed
        input (3286 + 3360). That is correct for a context-% readout and WRONG as
        a "tokens billed" figure — whatever renders this must label it as
        context. `_turn_usage` is unreachable on this path.
        """
        acc, _, _ = replayed
        assert acc.usage is not None
        assert acc.usage.input_tokens == 3360
        assert acc.usage.output_tokens == 5
        assert acc.context_window == 200_000

    def test_metadata_fired_once_per_llm_call(self, replayed: tuple[AgentTurnAccumulator, list[str], list[IgnoredEvent]]) -> None:
        """Three for one turn. Treating any one as a turn total is a bug."""
        acc, _, _ = replayed
        assert acc.events_seen.get("Metadata") == 3

    def test_nothing_spurious_was_folded(self, replayed: tuple[AgentTurnAccumulator, list[str], list[IgnoredEvent]]) -> None:
        """A plain tool turn carries no citations, artifacts or notices."""
        acc, _, _ = replayed
        assert acc.citations == []
        assert acc.artifacts == []
        assert acc.quota_notices == []
        assert acc.oauth_required == []
        assert acc.approvals_required == []
        assert acc.compactions == []
