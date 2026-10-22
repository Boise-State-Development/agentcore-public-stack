"""The integration that de-risks the whole feature: a **tool-scoped** interrupt.

Every interrupt this codebase has shipped so far — OAuth consent, per-tool
approval — is raised from a `BeforeToolCallEvent` hook. `ask_user_question`
raises its own from `ToolContext`, and the resume path it depends on
(`PausedTurnSnapshot` → rebuilt agent → restored `_interrupt_state` →
`pending_tool_execution` replayed) was built and proven against the hook
flavor only.

A mocked `interrupt()` proves nothing about that. The question is whether
Strands' event loop treats a tool-raised interrupt identically — whether
`_stop_for_interrupts` captures the same `pending_tool_execution`, and whether
resuming feeds the response back into the *same* tool call rather than
re-invoking it or dropping the batch. So this drives the real loop: a real
`Agent`, the real `@tool`, the real interrupt protocol. Only the model is
scripted.

If this ever fails, the SSE event and the breadcrumb are beside the point — the
turn cannot be resumed at all.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterable

import pytest
from strands import Agent
from strands.models.model import Model

from agents.builtin_tools.ask_user_question import ask_user_question

QUESTIONS = [
    {
        "header": "Scope",
        "question": "How much should this cover?",
        "options": [{"label": "Just the API"}, {"label": "Everything"}],
    }
]


class _ScriptedModel(Model):
    """Calls `ask_user_question` once, then answers in text if resumed."""

    def __init__(self) -> None:
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
            yield {
                "contentBlockStart": {
                    "start": {
                        "toolUse": {"name": "ask_user_question", "toolUseId": "tu-1"}
                    },
                    "contentBlockIndex": 0,
                }
            }
            yield {
                "contentBlockDelta": {
                    "delta": {"toolUse": {"input": json.dumps({"questions": QUESTIONS})}},
                    "contentBlockIndex": 0,
                }
            }
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"messageStart": {"role": "assistant"}}
            yield {"contentBlockStart": {"start": {}, "contentBlockIndex": 0}}
            yield {
                "contentBlockDelta": {
                    "delta": {"text": "Covering everything, then."},
                    "contentBlockIndex": 0,
                }
            }
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "end_turn"}}


def _agent() -> Agent:
    return Agent(
        model=_ScriptedModel(),
        tools=[ask_user_question],
        callback_handler=None,
    )


async def _run(agent: Agent, prompt: Any) -> None:
    async for _ in agent.stream_async(prompt):
        pass


def _tool_results(messages) -> list[dict]:
    return [
        block["toolResult"]
        for message in messages
        for block in (message.get("content") or [])
        if isinstance(block, dict) and "toolResult" in block
    ]


def _all_text(messages) -> str:
    return "\n".join(
        block.get("text", "")
        for message in messages
        for block in (message.get("content") or [])
        if isinstance(block, dict)
    )


def _the_interrupt(agent: Agent):
    return next(iter(agent._interrupt_state.interrupts.values()))


class TestToolScopedInterruptPauses:
    @pytest.mark.asyncio
    async def test_the_turn_really_pauses(self):
        agent = _agent()
        await _run(agent, "write me a summary")

        assert agent._interrupt_state.activated, "expected a paused turn"

    @pytest.mark.asyncio
    async def test_pending_tool_execution_is_captured(self):
        """The state the resume path replays. A hook-raised interrupt gets this
        from `_stop_for_interrupts`; this asserts a tool-raised one does too."""
        agent = _agent()
        await _run(agent, "write me a summary")

        pending = agent._interrupt_state.pending_tool_execution
        assert pending is not None
        assert any(
            block.get("toolUse", {}).get("toolUseId") == "tu-1"
            for block in pending.assistant_message["content"]
        )

    @pytest.mark.asyncio
    async def test_the_interrupt_id_is_tool_scoped(self):
        """`ToolContext._interrupt_id` folds the toolUseId in, which is what
        lets the SPA correlate a prompt with its tool card and what keeps two
        parallel calls distinct."""
        agent = _agent()
        await _run(agent, "write me a summary")

        assert _the_interrupt(agent).id.startswith("v1:tool_call:tu-1:")

    @pytest.mark.asyncio
    async def test_the_reason_carries_the_questions(self):
        agent = _agent()
        await _run(agent, "write me a summary")

        reason = _the_interrupt(agent).reason
        assert reason["type"] == "user_question_required"
        assert reason["questions"][0]["header"] == "Scope"

    @pytest.mark.asyncio
    async def test_no_tool_result_is_recorded_while_paused(self):
        """The call is genuinely suspended, not completed with a placeholder."""
        agent = _agent()
        await _run(agent, "write me a summary")

        assert _tool_results(agent.messages) == []


class TestResume:
    @pytest.mark.asyncio
    async def test_the_answer_becomes_the_tool_result(self):
        """The property that matters: the response feeds back into the *same*
        tool call, so the model reads the user's choice as that call's result."""
        agent = _agent()
        await _run(agent, "write me a summary")

        await _run(
            agent,
            [
                {
                    "interruptResponse": {
                        "interruptId": _the_interrupt(agent).id,
                        "response": {"answers": {"Scope": ["Everything"]}},
                    }
                }
            ],
        )

        [result] = _tool_results(agent.messages)
        assert result["toolUseId"] == "tu-1"
        assert result["status"] == "success"
        assert "- Scope: Everything" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_the_turn_completes_after_resuming(self):
        agent = _agent()
        await _run(agent, "write me a summary")
        await _run(
            agent,
            [
                {
                    "interruptResponse": {
                        "interruptId": _the_interrupt(agent).id,
                        "response": {"answers": {"Scope": ["Everything"]}},
                    }
                }
            ],
        )

        assert not agent._interrupt_state.activated
        assert "Covering everything, then." in _all_text(agent.messages)

    @pytest.mark.asyncio
    async def test_the_tool_is_not_invoked_twice(self):
        """A resume that re-ran the tool would ask the same question again —
        an infinite prompt loop the user could not escape."""
        agent = _agent()
        await _run(agent, "write me a summary")
        await _run(
            agent,
            [
                {
                    "interruptResponse": {
                        "interruptId": _the_interrupt(agent).id,
                        "response": {"answers": {"Scope": ["Everything"]}},
                    }
                }
            ],
        )

        assert len(_tool_results(agent.messages)) == 1

    @pytest.mark.asyncio
    async def test_a_skip_also_resumes(self):
        """The escape hatch. `ToolContext.interrupt` only treats a non-None
        response as an answer, so "Skip" must post a payload rather than null —
        a null would re-raise the interrupt forever."""
        agent = _agent()
        await _run(agent, "write me a summary")

        await _run(
            agent,
            [
                {
                    "interruptResponse": {
                        "interruptId": _the_interrupt(agent).id,
                        "response": {"skipped": True},
                    }
                }
            ],
        )

        assert not agent._interrupt_state.activated
        [result] = _tool_results(agent.messages)
        assert "skipped" in result["content"][0]["text"]
