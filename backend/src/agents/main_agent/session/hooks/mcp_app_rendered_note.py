"""Tell the model when a tool's result is also on screen as an MCP App.

An App tool's result reaches two places. The embedded App draws it for the
user from ``structuredContent`` and ``_meta``; the model gets only the text
content. Nothing told the model the user could already see the result, so
after an Atlassian Jira search it re-listed the same issues as a markdown
table under the widget that showed them.

This hook appends one fixed sentence to such a result, after the call and
before the result enters history, so the model answers around the App rather
than repeating it. The model keeps the full data for follow-up questions.

Rules:

- Only while the host can frame an App: the host flag is on and a sandbox
  origin is configured. Otherwise the SPA renders no App and the note would
  be false.
- Only for a tool that declares an App (``_meta.ui``) and only for a
  successful result. An error result is the model's to explain, and an App
  form waiting for the user (``awaiting_user_submission``) already says so in
  its own text.
- The sentence is a constant appended after the existing blocks, so the
  persisted result is byte-stable on restore and the first text block, which
  an App may parse, is untouched.
- Registered after the tool-result offloader (``order=70``) so it is added to
  the bounded result the model actually receives, never offloaded with the
  payload.
"""

from __future__ import annotations

import logging
from typing import Any

from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry

from agents.main_agent.integrations.mcp_apps import (
    is_app_tool,
    is_mcp_apps_host_enabled,
    mcp_apps_sandbox_origin,
)
from apis.shared.mcp_apps.awaiting_submission import has_awaiting_marker

logger = logging.getLogger(__name__)

# After ContextOffloader (70), before the SDK's last slot (100).
RENDERED_NOTE_HOOK_ORDER = 80

RENDERED_APP_NOTE = (
    "[This result is also shown to the user as an interactive view attached to "
    "this tool call. They can already see what it shows, so don't repeat it as a "
    "list or table: answer their question, point out what matters, or add what "
    "the view doesn't show.]"
)


def _app_can_render() -> bool:
    return is_mcp_apps_host_enabled() and bool(mcp_apps_sandbox_origin())


def with_rendered_note(result: Any) -> Any:
    """``result`` with the note appended, or ``result`` itself when it has no
    content list to append to or already carries the note."""
    if not isinstance(result, dict):
        return result
    content = result.get("content")
    if not isinstance(content, list):
        return result
    if any(isinstance(b, dict) and b.get("text") == RENDERED_APP_NOTE for b in content):
        return result
    return {**result, "content": [*content, {"text": RENDERED_APP_NOTE}]}


class McpAppRenderedNoteHook(HookProvider):
    """Append :data:`RENDERED_APP_NOTE` to a successful MCP App tool result."""

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(
            AfterToolCallEvent, self._on_after_tool_call, order=RENDERED_NOTE_HOOK_ORDER
        )

    def _on_after_tool_call(self, event: AfterToolCallEvent) -> None:
        if getattr(event, "exception", None) is not None:
            return
        result = getattr(event, "result", None)
        if not isinstance(result, dict) or result.get("status") != "success":
            return
        if has_awaiting_marker(result):
            return
        if not is_app_tool(getattr(event, "selected_tool", None)):
            return
        if not _app_can_render():
            return
        event.result = with_rendered_note(result)
