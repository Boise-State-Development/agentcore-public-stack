"""Tests for the hook that marks an MCP App form left open for the user.

The marker is written at ``AfterToolCallEvent``, before Strands appends the
result to the conversation, so the live ``tool_result`` SSE and the persisted
message carry one shape. It must never change what the model reads, and it
must never fire for a tool without an App.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agents.main_agent.session.hooks.mcp_app_awaiting import (
    McpAppAwaitingSubmissionHook,
    call_awaits_user_submission,
)
from apis.shared.mcp_apps.awaiting_submission import AWAITING_USER_SUBMISSION

_STUB_TEXT = (
    "An interactive form has been shown to the user for creating a new issue in "
    "owner/repo. STOP — do not call any other tools."
)


def _app_tool():
    return SimpleNamespace(
        tool_name="issue_write",
        mcp_tool=SimpleNamespace(
            name="issue_write", meta={"ui": {"resourceUri": "ui://github/issue_write"}}
        ),
    )


def _plain_tool():
    return SimpleNamespace(
        tool_name="search_issues",
        mcp_tool=SimpleNamespace(name="search_issues", meta=None),
    )


def _event(selected_tool, text=_STUB_TEXT, status="error", exception=None):
    event = MagicMock()
    event.tool_use = {"toolUseId": "t1", "name": "issue_write", "input": {}}
    event.selected_tool = selected_tool
    event.exception = exception
    event.result = {
        "status": status,
        "toolUseId": "t1",
        "content": [{"text": text}],
        "isError": status == "error",
    }
    return event


@pytest.fixture
def hook():
    return McpAppAwaitingSubmissionHook()


def test_marks_githubs_stub_from_an_app_tool(hook):
    event = _event(_app_tool())

    hook._on_after_tool_call(event)

    assert event.result["structuredContent"] == {"status": AWAITING_USER_SUBMISSION}
    assert event.result["status"] == "error"


def test_leaves_a_tool_without_an_app_alone(hook):
    event = _event(_plain_tool())

    hook._on_after_tool_call(event)

    assert "structuredContent" not in event.result


def test_leaves_an_invented_tool_alone(hook):
    event = _event(None)

    hook._on_after_tool_call(event)

    assert "structuredContent" not in event.result


def test_leaves_a_real_app_failure_alone(hook):
    event = _event(_app_tool(), text="Connection to the MCP server was closed")

    hook._on_after_tool_call(event)

    assert "structuredContent" not in event.result


def test_a_raised_call_is_never_awaiting(hook):
    event = _event(_app_tool(), exception=RuntimeError("boom"))

    hook._on_after_tool_call(event)

    assert "structuredContent" not in event.result
    assert not call_awaits_user_submission(event)


def test_a_malformed_event_never_raises(hook):
    event = MagicMock()
    event.exception = None
    event.result = None

    hook._on_after_tool_call(event)


def test_the_predicate_does_not_depend_on_the_hook_having_run():
    """AgentStatusHook and ToolCensusHook ask before or after this hook,
    depending on registration order; both must get the same answer."""
    assert call_awaits_user_submission(_event(_app_tool()))
    assert not call_awaits_user_submission(_event(_plain_tool()))


def test_the_marker_is_not_in_what_bedrock_receives():
    """structuredContent must never reach the Converse payload (cache prefix)."""
    from strands.models.bedrock import BedrockModel

    event = _event(_app_tool())
    McpAppAwaitingSubmissionHook()._on_after_tool_call(event)

    model = BedrockModel(model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0")
    formatted = model._format_request_message_content({"toolResult": event.result})

    assert "structuredContent" not in formatted["toolResult"]
    assert AWAITING_USER_SUBMISSION not in str(formatted)
