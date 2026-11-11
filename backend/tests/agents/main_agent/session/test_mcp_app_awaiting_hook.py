"""Tests for the predicate the outcome hooks use for an MCP App form left open.

``AgentStatusHook`` and ``ToolCensusHook`` ask ``call_awaits_user_submission``
whether a finished call is a form waiting for the user. It reads the server's
own marker; nothing writes one onto a bare stub any more (the #1662 hook is
retired), and the marker must never reach what the model receives.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agents.main_agent.session.hooks.mcp_app_awaiting import call_awaits_user_submission
from apis.shared.mcp_apps.awaiting_submission import AWAITING_USER_SUBMISSION

_STUB_TEXT = (
    "An interactive form has been shown to the user for creating a new issue in "
    "owner/repo. STOP — do not call any other tools."
)
_MARKER = {"status": AWAITING_USER_SUBMISSION}


def _app_tool():
    return SimpleNamespace(
        tool_name="issue_write",
        mcp_tool=SimpleNamespace(
            name="issue_write", meta={"ui": {"resourceUri": "ui://github/issue_write"}}
        ),
    )


def _event(selected_tool=None, text=_STUB_TEXT, structured=None, exception=None):
    event = MagicMock()
    event.tool_use = {"toolUseId": "t1", "name": "issue_write", "input": {}}
    event.selected_tool = selected_tool
    event.exception = exception
    event.result = {
        "status": "error",
        "toolUseId": "t1",
        "content": [{"text": text}],
        "isError": True,
    }
    if structured is not None:
        event.result["structuredContent"] = structured
    return event


def test_a_marked_stub_awaits_the_user():
    assert call_awaits_user_submission(_event(_app_tool(), structured=_MARKER))


def test_the_marker_counts_without_an_app_on_the_tool():
    """The server's own marker is authoritative wherever it appears."""
    assert call_awaits_user_submission(_event(None, structured=_MARKER))


def test_a_bare_stub_is_not_awaiting():
    """Retired #1662 fallback: the stub sentence alone no longer counts."""
    assert not call_awaits_user_submission(_event(_app_tool()))


def test_a_raised_call_is_never_awaiting():
    event = _event(_app_tool(), structured=_MARKER, exception=RuntimeError("boom"))
    assert not call_awaits_user_submission(event)


def test_a_malformed_event_is_not_awaiting():
    event = MagicMock()
    event.exception = None
    event.result = None
    assert not call_awaits_user_submission(event)


def test_the_marker_is_not_in_what_bedrock_receives():
    """structuredContent must never reach the Converse payload (cache prefix)."""
    from strands.models.bedrock import BedrockModel

    event = _event(_app_tool(), structured=_MARKER)

    model = BedrockModel(model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0")
    formatted = model._format_request_message_content({"toolResult": event.result})

    assert "structuredContent" not in formatted["toolResult"]
    assert AWAITING_USER_SUBMISSION not in str(formatted)
