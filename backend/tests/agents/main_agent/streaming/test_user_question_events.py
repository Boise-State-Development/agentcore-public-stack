"""`user_question_required` extraction from a paused turn.

The extractor reads `agent._interrupt_state` at the `done` event, exactly like
`_extract_tool_approval_required_events`. What makes this flavor worth its own
tests is that the interrupt is raised by the *tool* rather than by a hook — so
these assert the flavor filter both ways, and that a payload the SPA could not
draw is dropped rather than emitted.

Persistence is best-effort by design: the live prompt must survive a DynamoDB
failure. Losing the breadcrumb costs a refresh, losing the event costs the turn.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator


def _parse(sse: str) -> dict:
    data_line = next(line for line in sse.splitlines() if line.startswith("data: "))
    return json.loads(data_line[len("data: ") :])


def _coordinator() -> StreamCoordinator:
    return StreamCoordinator.__new__(StreamCoordinator)


QUESTIONS = [
    {
        "header": "Scope",
        "question": "How much should this cover?",
        "multiSelect": False,
        "options": [{"label": "Just the API", "description": "Routes only"},
                    {"label": "Everything"}],
    }
]


def _agent(*interrupts, activated: bool = True):
    return SimpleNamespace(
        _interrupt_state=SimpleNamespace(
            activated=activated,
            interrupts={f"i-{n}": i for n, i in enumerate(interrupts)},
        )
    )


def _interrupt(reason, interrupt_id: str = "v1:tool_call:tu-1:abc"):
    return SimpleNamespace(id=interrupt_id, reason=reason)


_DEFAULT = object()


def _user_question_reason(questions=_DEFAULT, tool_use_id: str = "tu-1"):
    return {
        "type": "user_question_required",
        "toolUseId": tool_use_id,
        "questions": QUESTIONS if questions is _DEFAULT else questions,
    }


@pytest.fixture
def add_pending():
    """Patch the breadcrumb writer at its definition site."""
    with patch(
        "apis.shared.sessions.metadata.add_pending_interrupt",
        new_callable=AsyncMock,
    ) as mock:
        yield mock


class TestEventEmission:
    @pytest.mark.asyncio
    async def test_emits_one_event_carrying_the_questions(self, add_pending):
        events = await _coordinator()._extract_user_question_required_events(
            _agent(_interrupt(_user_question_reason())),
            session_id="s1",
            user_id="u1",
        )

        assert len(events) == 1
        assert events[0].startswith("event: user_question_required\n")
        payload = _parse(events[0])
        assert payload["interruptId"] == "v1:tool_call:tu-1:abc"
        assert payload["toolUseId"] == "tu-1"
        [question] = payload["questions"]
        assert question["header"] == "Scope"
        assert question["multiSelect"] is False
        assert question["options"][0]["description"] == "Routes only"
        assert "description" not in question["options"][1]

    @pytest.mark.asyncio
    async def test_one_event_per_pending_prompt(self, add_pending):
        """Two parallel calls in one turn get distinct tool-scoped ids, and the
        SPA correlates prompts by them."""
        events = await _coordinator()._extract_user_question_required_events(
            _agent(
                _interrupt(_user_question_reason(tool_use_id="tu-1"), "v1:tool_call:tu-1:a"),
                _interrupt(_user_question_reason(tool_use_id="tu-2"), "v1:tool_call:tu-2:b"),
            ),
            session_id="s1",
            user_id="u1",
        )

        assert {_parse(e)["toolUseId"] for e in events} == {"tu-1", "tu-2"}

    @pytest.mark.asyncio
    async def test_ignores_other_interrupt_flavors(self, add_pending):
        """OAuth and tool-approval interrupts share the same state dict."""
        events = await _coordinator()._extract_user_question_required_events(
            _agent(
                _interrupt({"type": "tool_approval_required", "toolName": "send_email"}),
                _interrupt({"type": "oauth_required", "providerId": "github"}),
            ),
            session_id="s1",
            user_id="u1",
        )

        assert events == []
        add_pending.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_events_when_the_turn_is_not_paused(self, add_pending):
        events = await _coordinator()._extract_user_question_required_events(
            _agent(_interrupt(_user_question_reason()), activated=False),
            session_id="s1",
            user_id="u1",
        )

        assert events == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("questions", [[], None, [{"question": "no options"}], "junk"])
    async def test_unrenderable_questions_are_dropped_not_emitted(
        self, add_pending, questions
    ):
        """An event the SPA cannot draw would leave the turn paused with no
        prompt on screen — worse than no event at all."""
        events = await _coordinator()._extract_user_question_required_events(
            _agent(_interrupt(_user_question_reason(questions=questions))),
            session_id="s1",
            user_id="u1",
        )

        assert events == []


class TestPersistence:
    @pytest.mark.asyncio
    async def test_breadcrumb_is_written_for_reload_recovery(self, add_pending):
        await _coordinator()._extract_user_question_required_events(
            _agent(_interrupt(_user_question_reason())),
            session_id="s1",
            user_id="u1",
        )

        add_pending.assert_awaited_once()
        interrupt = add_pending.await_args.kwargs["interrupt"]
        assert interrupt.kind == "user_question"
        assert interrupt.tool_use_id == "tu-1"
        # JSON-encoded, like `tool_input`: DynamoDB would otherwise coerce
        # numbers in the payload to Decimal on the way back out.
        assert json.loads(interrupt.questions)[0]["header"] == "Scope"

    @pytest.mark.asyncio
    async def test_write_failure_does_not_cost_the_live_prompt(self, add_pending):
        add_pending.side_effect = RuntimeError("dynamo down")

        events = await _coordinator()._extract_user_question_required_events(
            _agent(_interrupt(_user_question_reason())),
            session_id="s1",
            user_id="u1",
        )

        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_anonymous_flows_emit_without_persisting(self, add_pending):
        """Preview / anonymous turns have no metadata row to write to."""
        events = await _coordinator()._extract_user_question_required_events(
            _agent(_interrupt(_user_question_reason())),
            session_id=None,
            user_id=None,
        )

        assert len(events) == 1
        add_pending.assert_not_awaited()
