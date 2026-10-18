"""Mark an MCP App's "form shown, nothing written yet" result at intake.

GitHub's App write tools return ``isError: true`` while their form waits for
the user (see ``apis/shared/mcp_apps/awaiting_submission.py``). On our MCP
protocol version the stub carries no ``structuredContent`` marker, so the SPA
could only render it as a failure. This hook writes the marker onto the
result on ``AfterToolCallEvent``, before Strands appends it to the
conversation, so the live ``tool_result`` SSE and the persisted message carry
the same shape and a reload agrees with what streamed.

The marker sits on ``structuredContent``, which no model formatter sends, so
the Converse payload and the cacheable prefix are unchanged; it is written
once at intake and never mutated later, so restored history stays
byte-stable. Cost: a dict lookup per tool call, after the tool has run.
"""

from __future__ import annotations

import logging
from typing import Any

from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry

from agents.main_agent.integrations.mcp_apps import is_app_tool
from apis.shared.mcp_apps.awaiting_submission import (
    awaiting_user_submission,
    mark_awaiting_user_submission,
)

logger = logging.getLogger(__name__)


def call_awaits_user_submission(event: Any) -> bool:
    """True when a finished tool call is an App form waiting for the user.

    Reads the result directly instead of relying on
    :class:`McpAppAwaitingSubmissionHook` having run first, so a hook that
    asks (``AgentStatusHook``, ``ToolCensusHook``) gets the same answer in
    any registration order.
    """
    if getattr(event, "exception", None) is not None:
        return False
    return awaiting_user_submission(
        getattr(event, "result", None),
        is_app_tool=is_app_tool(getattr(event, "selected_tool", None)),
    )


class McpAppAwaitingSubmissionHook(HookProvider):
    """Writes the ``awaiting_user_submission`` marker onto a recognized stub."""

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self._on_after_tool_call)

    def _on_after_tool_call(self, event: AfterToolCallEvent) -> None:
        try:
            if getattr(event, "exception", None) is not None:
                return
            mark_awaiting_user_submission(
                event.result,
                is_app_tool=is_app_tool(getattr(event, "selected_tool", None)),
            )
        except Exception:  # noqa: BLE001 - a label must never break a turn
            logger.debug("Awaiting-submission marker skipped", exc_info=True)
