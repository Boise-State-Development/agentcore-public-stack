"""The integration the spec calls "the one that matters".

`docs/specs/mid-turn-steering.md`, Testing:

    A turn that interrupts on the same tool batch as a steer must leave the
    entry in the inbox. ... a mocked interrupt proves nothing here.

The hazard is silent data loss. `AfterToolsEvent` fires from a ``finally``, so
it fires on the interrupt path too — but there `_stop_for_interrupts` runs and
``agent._append_messages`` is **never reached**, so the message ``SteeringHook``
just mutated is thrown away. A hook that consumed the inbox when it read it
would therefore destroy the user's words every time a steer happened to land on
the same tool batch as an OAuth consent or an approval prompt. Low frequency,
very hard to reproduce, and unrecoverable.

So this drives the **real** Strands event loop: a real ``Agent``, real
``@tool`` functions, a real ``BeforeToolCallEvent`` hook raising a real
interrupt, and the real ``SteeringHook``. Only the model and the DynamoDB inbox
are stood in for. Asserting against a mocked interrupt would prove nothing
about `_stop_for_interrupts`, which is the thing that actually discards the
message.

The batch is deliberately ordered so the ungated tool completes **before** the
gated one pauses: that is the only shape where there is a real injection to
lose. A batch that interrupts before any tool ran carries no tool results, and
the hook declines to inject into it at all (covered here too).
"""

from typing import Any, AsyncIterable
from unittest.mock import AsyncMock, MagicMock

import pytest
from strands import Agent, tool
from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry
from strands.models.model import Model

from agents.main_agent.session.hooks.steering import (
    STEER_OPEN_TAG,
    SteeringHook,
)
from apis.shared.sessions.session_lease import SessionLease


# ---------------------------------------------------------------------------
# Tools: one that completes, one that pauses
# ---------------------------------------------------------------------------

@tool
def quick_lookup(topic: str) -> str:
    """Return a canned fact. Completes normally."""
    return f"fact about {topic}"


@tool
def gated_action(payload: str) -> str:
    """Never actually runs in these tests — the hook below pauses it first."""
    return f"did {payload}"


class _ApprovalHook(HookProvider):
    """Pauses `gated_action` with a real Strands interrupt.

    Same shape as the production `MCPExternalApprovalHook`: a
    `BeforeToolCallEvent` callback calling `event.interrupt(...)`. Using the
    real mechanism is the point — this is what routes the turn through
    `_stop_for_interrupts` and discards the mutated tool-result message.
    """

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._gate)

    def _gate(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use.get("name") != "gated_action":
            return
        event.interrupt(
            name=f"approval:{event.tool_use['toolUseId']}",
            reason={"type": "tool_approval_required"},
        )


# ---------------------------------------------------------------------------
# A model that emits one tool batch, then (if resumed) a final answer
# ---------------------------------------------------------------------------

class _ScriptedModel(Model):
    """Emits a fixed tool-use batch on the first call, text on later calls."""

    def __init__(self, tool_names: list[str]) -> None:
        self._tool_names = tool_names
        self.calls = 0

    def update_config(self, **model_config: Any) -> None:  # pragma: no cover
        pass

    def get_config(self) -> Any:  # pragma: no cover
        return {}

    def structured_output(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise NotImplementedError

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterable[dict]:
        self.calls += 1
        if self.calls == 1:
            yield {"messageStart": {"role": "assistant"}}
            for index, name in enumerate(self._tool_names):
                yield {
                    "contentBlockStart": {
                        "start": {"toolUse": {"name": name, "toolUseId": f"tu-{index}"}},
                        "contentBlockIndex": index,
                    }
                }
                yield {
                    "contentBlockDelta": {
                        "delta": {"toolUse": {"input": '{"topic": "x", "payload": "y"}'}},
                        "contentBlockIndex": index,
                    }
                }
                yield {"contentBlockStop": {"contentBlockIndex": index}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"messageStart": {"role": "assistant"}}
            yield {"contentBlockStart": {"start": {}, "contentBlockIndex": 0}}
            yield {"contentBlockDelta": {"delta": {"text": "done"}, "contentBlockIndex": 0}}
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "end_turn"}}


# ---------------------------------------------------------------------------

@pytest.fixture
def lease():
    return SessionLease(session_id="s1", user_id="u1", owner="owner-1")


@pytest.fixture
def inbox(monkeypatch):
    """Stand-in for the DynamoDB inbox, recording peeks and clears."""
    import apis.shared.sessions.session_lease as mod

    peek = AsyncMock(return_value=[{"id": "e1", "text": "actually use the other file"}])
    clear = AsyncMock(return_value=True)
    monkeypatch.setattr(mod, "peek_steer_queue", peek)
    monkeypatch.setattr(mod, "clear_steer_entry", clear)
    return MagicMock(peek=peek, clear=clear)


def _build_agent(lease, tool_names):
    manager = MagicMock()
    manager.turn_lease = lease
    hook = SteeringHook(manager)
    agent = Agent(
        model=_ScriptedModel(tool_names),
        tools=[quick_lookup, gated_action],
        hooks=[_ApprovalHook(), hook],
        callback_handler=None,
    )
    return agent, hook


async def _run(agent) -> None:
    async for _ in agent.stream_async("do the thing"):
        pass


def _all_text(messages) -> str:
    return "\n".join(
        block.get("text", "")
        for message in messages
        for block in (message.get("content") or [])
        if isinstance(block, dict)
    )


class TestSteerOnAnInterruptedBatch:
    @pytest.mark.asyncio
    async def test_the_entry_survives_a_turn_that_interrupts(self, lease, inbox):
        """The property. Injected, discarded, and NOT consumed."""
        agent, hook = _build_agent(lease, ["quick_lookup", "gated_action"])

        await _run(agent)

        # The hook read the inbox at the tool boundary...
        inbox.peek.assert_awaited()
        # ...and must NOT have consumed it: the message it mutated was thrown
        # away by `_stop_for_interrupts`, so the user's words are still owed.
        inbox.clear.assert_not_awaited()
        assert hook.drain_applied() == []

    @pytest.mark.asyncio
    async def test_the_injection_is_not_in_history(self, lease, inbox):
        """The other half of the same fact, observed from the conversation.

        If this text were in history AND the entry were still queued, the user
        would get it twice. Neither-both-nor-neither is the whole contract.
        """
        agent, _ = _build_agent(lease, ["quick_lookup", "gated_action"])

        await _run(agent)

        assert STEER_OPEN_TAG not in _all_text(agent.messages)

    @pytest.mark.asyncio
    async def test_the_turn_really_did_pause(self, lease, inbox):
        """Guards the test itself.

        If the interrupt stopped firing — an SDK change, a renamed event — every
        assertion above would pass for the wrong reason, because a turn that
        never pauses also never discards anything.
        """
        agent, _ = _build_agent(lease, ["quick_lookup", "gated_action"])

        await _run(agent)

        assert agent._interrupt_state.activated, "expected a paused turn"

    @pytest.mark.asyncio
    async def test_a_batch_that_ran_no_tools_is_not_injected_into(self, lease, inbox):
        """The gated tool alone: nothing completed, so there is nothing to ride.

        The hook declines rather than appending a lone text block to a message
        that carries no tool results.
        """
        agent, hook = _build_agent(lease, ["gated_action"])

        await _run(agent)

        inbox.clear.assert_not_awaited()
        assert STEER_OPEN_TAG not in _all_text(agent.messages)
        assert hook.drain_applied() == []


class TestSteerOnACompletedBatch:
    @pytest.mark.asyncio
    async def test_a_batch_that_completes_consumes_the_entry(self, lease, inbox):
        """The contrast case, so the tests above cannot pass by inertia.

        Same hook, same inbox, no interrupt: here the message IS appended, so
        `MessageAddedEvent` fires and the entry is consumed exactly once.
        """
        manager = MagicMock()
        manager.turn_lease = lease
        hook = SteeringHook(manager)
        agent = Agent(
            model=_ScriptedModel(["quick_lookup"]),
            tools=[quick_lookup, gated_action],
            hooks=[hook],
            callback_handler=None,
        )

        await _run(agent)

        inbox.clear.assert_awaited_once_with(lease, "e1")
        assert [e["id"] for e in hook.drain_applied()] == ["e1"]
        assert STEER_OPEN_TAG in _all_text(agent.messages)
