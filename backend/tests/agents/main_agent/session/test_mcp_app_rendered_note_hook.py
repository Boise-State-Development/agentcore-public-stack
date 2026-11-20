"""Tests for the note telling the model an MCP App tool result is on screen.

After an Atlassian Jira search, the user saw Atlassian's widget listing five
issues and then the model's own table of the same five: the model only gets
the result's text and had no way to know the App had drawn it. The hook
appends one constant sentence to a successful App tool result while the host
can frame Apps.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from strands.hooks import AfterToolCallEvent, HookRegistry

from agents.main_agent.config.constants import EnvVars
from agents.main_agent.session.hooks.mcp_app_rendered_note import (
    RENDERED_APP_NOTE,
    RENDERED_NOTE_HOOK_ORDER,
    McpAppRenderedNoteHook,
    with_rendered_note,
)
from apis.shared.mcp_apps.awaiting_submission import AWAITING_USER_SUBMISSION

_RESULT_TEXT = '{"issues": {"nodes": [{"key": "BSUSF-3964"}]}}'


@pytest.fixture
def app_host(monkeypatch):
    monkeypatch.setenv(EnvVars.MCP_APPS_HOST_ENABLED, "true")
    monkeypatch.setenv(EnvVars.MCP_APPS_SANDBOX_ORIGIN, "https://mcp-sandbox.example")


def _app_tool():
    return SimpleNamespace(
        tool_name="searchJiraIssuesUsingJql",
        mcp_tool=SimpleNamespace(
            name="searchJiraIssuesUsingJql",
            meta={"ui": {"resourceUri": "ui://widget/jira-widget.html"}},
        ),
    )


def _plain_tool():
    return SimpleNamespace(
        tool_name="getJiraIssue", mcp_tool=SimpleNamespace(name="getJiraIssue", meta=None)
    )


def _event(selected_tool, status="success", structured=None, exception=None):
    event = MagicMock()
    event.selected_tool = selected_tool
    event.exception = exception
    event.result = {
        "toolUseId": "t1",
        "status": status,
        "content": [{"text": _RESULT_TEXT}],
    }
    if structured is not None:
        event.result["structuredContent"] = structured
    return event


def _run(event):
    McpAppRenderedNoteHook()._on_after_tool_call(event)
    return event.result


def test_a_successful_app_result_gets_the_note_after_its_content(app_host):
    result = _run(_event(_app_tool(), structured={"issues": {"nodes": []}}))

    assert result["content"] == [{"text": _RESULT_TEXT}, {"text": RENDERED_APP_NOTE}]
    # Everything else, including what the App renders from, is untouched.
    assert result["structuredContent"] == {"issues": {"nodes": []}}
    assert result["toolUseId"] == "t1"


def test_a_tool_without_an_app_gets_no_note(app_host):
    result = _run(_event(_plain_tool()))
    assert result["content"] == [{"text": _RESULT_TEXT}]


def test_an_error_result_gets_no_note(app_host):
    """The model has to explain a failure; the App is not a substitute."""
    result = _run(_event(_app_tool(), status="error"))
    assert result["content"] == [{"text": _RESULT_TEXT}]


def test_a_raised_call_gets_no_note(app_host):
    result = _run(_event(_app_tool(), exception=RuntimeError("boom")))
    assert result["content"] == [{"text": _RESULT_TEXT}]


def test_a_form_waiting_for_the_user_gets_no_note(app_host):
    """GitHub's waiting form says so in its own text."""
    result = _run(_event(_app_tool(), structured={"status": AWAITING_USER_SUBMISSION}))
    assert result["content"] == [{"text": _RESULT_TEXT}]


def test_no_note_while_the_host_flag_is_off(monkeypatch):
    monkeypatch.setenv(EnvVars.MCP_APPS_HOST_ENABLED, "false")
    monkeypatch.setenv(EnvVars.MCP_APPS_SANDBOX_ORIGIN, "https://mcp-sandbox.example")
    result = _run(_event(_app_tool()))
    assert result["content"] == [{"text": _RESULT_TEXT}]


def test_no_note_without_a_sandbox_origin(monkeypatch):
    """No origin means the SPA cannot frame the App, so the note would be false."""
    monkeypatch.setenv(EnvVars.MCP_APPS_HOST_ENABLED, "true")
    monkeypatch.setenv(EnvVars.MCP_APPS_SANDBOX_ORIGIN, "")
    result = _run(_event(_app_tool()))
    assert result["content"] == [{"text": _RESULT_TEXT}]


def test_the_note_is_added_once():
    once = with_rendered_note({"status": "success", "content": [{"text": "x"}]})
    assert with_rendered_note(once) == once


def test_the_note_is_a_constant():
    """Prompt-cache contract: the persisted result must be byte-stable, so the
    sentence names no server, tool, time or count."""
    first = with_rendered_note({"status": "success", "content": [{"text": "a"}]})
    second = with_rendered_note({"status": "success", "content": [{"text": "b"}]})
    assert first["content"][-1] == second["content"][-1] == {"text": RENDERED_APP_NOTE}


def test_it_runs_after_the_tool_result_offloader():
    """The offloader (order 70) bounds a large result; the note must be added to
    what the model actually receives, not offloaded with the payload."""
    registry = HookRegistry()
    calls: list[str] = []
    registry.add_callback(AfterToolCallEvent, lambda e: calls.append("offloader"), order=70)
    hook = McpAppRenderedNoteHook()
    hook._on_after_tool_call = lambda e: calls.append("note")  # type: ignore[method-assign]
    hook.register_hooks(registry)

    event = AfterToolCallEvent(
        agent=MagicMock(),
        selected_tool=None,
        tool_use={"toolUseId": "t1", "name": "x", "input": {}},
        invocation_state={},
        result={"toolUseId": "t1", "status": "success", "content": []},
    )
    for callback in registry.get_callbacks_for(event):
        callback(event)

    assert RENDERED_NOTE_HOOK_ORDER > 70
    assert calls == ["offloader", "note"]
