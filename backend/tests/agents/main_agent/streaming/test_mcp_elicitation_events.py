"""`mcp_elicitation_required` extraction from a paused turn.

Same contract as the other interrupt extractors, plus one rule of its own: a
call that elicits twice pauses twice, and the first round's interrupt is still
in the state, answered. Only an unanswered form is one to show.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator
from apis.shared.mcp_elicitation.forms import build_reason, normalize_form

SCHEMA = {
    "type": "object",
    "properties": {"repository_name": {"type": "string", "title": "Repository name"}},
    "required": ["repository_name"],
}


def _reason(tool_use_id: str = "tu-1") -> dict:
    reason = build_reason(
        normalize_form('Type "acme/scratch" to confirm.', SCHEMA),
        tool_name="github___delete_repository",
        server_name="GitHub",
    )
    reason["toolUseId"] = tool_use_id
    return reason


def _interrupt(reason, interrupt_id="v1:middleware_execute_tool:tu-1:a", response=None):
    return SimpleNamespace(id=interrupt_id, reason=reason, response=response)


def _agent(*interrupts, activated: bool = True):
    return SimpleNamespace(
        _interrupt_state=SimpleNamespace(
            activated=activated, interrupts={i.id: i for i in interrupts}
        )
    )


def _parse(sse: str) -> dict:
    data_line = next(line for line in sse.splitlines() if line.startswith("data: "))
    return json.loads(data_line[len("data: ") :])


@pytest.fixture
def add_pending():
    with patch(
        "apis.shared.sessions.metadata.add_pending_interrupt", new_callable=AsyncMock
    ) as mock:
        yield mock


def _extract(agent, **kwargs):
    coordinator = StreamCoordinator.__new__(StreamCoordinator)
    return coordinator._extract_mcp_elicitation_required_events(agent, **kwargs)


@pytest.mark.asyncio
async def test_emits_one_event_with_the_form_and_who_is_asking(add_pending):
    (sse,) = await _extract(_agent(_interrupt(_reason())), session_id="s", user_id="u")

    assert sse.startswith("event: mcp_elicitation_required\n")
    payload = _parse(sse)
    assert payload["interruptId"] == "v1:middleware_execute_tool:tu-1:a"
    assert payload["toolUseId"] == "tu-1"
    assert payload["serverName"] == "GitHub"
    assert payload["message"] == 'Type "acme/scratch" to confirm.'
    assert [f["name"] for f in payload["fields"]] == ["repository_name"]


@pytest.mark.asyncio
async def test_persists_a_breadcrumb_that_rebuilds_the_same_form(add_pending):
    (sse,) = await _extract(_agent(_interrupt(_reason())), session_id="s", user_id="u")

    crumb = add_pending.await_args.kwargs["interrupt"]
    assert crumb.kind == "mcp_elicitation"
    assert crumb.tool_use_id == "tu-1"
    assert crumb.tool_name == "github___delete_repository"
    assert json.loads(crumb.elicitation) == _parse(sse)


@pytest.mark.asyncio
async def test_an_answered_round_is_not_shown_again(add_pending):
    answered = _interrupt(_reason(), "v1:a", response={"action": "accept", "content": {}})
    waiting = _interrupt(_reason(), "v1:b")

    events = await _extract(_agent(answered, waiting), session_id="s", user_id="u")

    assert [_parse(e)["interruptId"] for e in events] == ["v1:b"]


@pytest.mark.asyncio
async def test_other_interrupt_kinds_are_ignored(add_pending):
    question = _interrupt({"type": "user_question_required", "questions": []}, "v1:q")
    events = await _extract(_agent(question), session_id="s", user_id="u")

    assert events == []
    add_pending.assert_not_awaited()


@pytest.mark.asyncio
async def test_nothing_when_the_turn_is_not_paused(add_pending):
    assert await _extract(_agent(_interrupt(_reason()), activated=False)) == []


@pytest.mark.asyncio
async def test_a_form_with_no_fields_is_dropped(add_pending):
    reason = _reason()
    reason["fields"] = []
    assert await _extract(_agent(_interrupt(reason)), session_id="s", user_id="u") == []


@pytest.mark.asyncio
async def test_a_failed_breadcrumb_write_still_emits_the_event(add_pending):
    add_pending.side_effect = RuntimeError("dynamodb is down")
    events = await _extract(_agent(_interrupt(_reason())), session_id="s", user_id="u")
    assert len(events) == 1
