"""Recognize an MCP App's "form shown, nothing written yet" result.

GitHub's App write tools return ``isError: true`` while their form waits for
the user, and mark it with ``structuredContent.status ==
"awaiting_user_submission"`` (see ``apis/shared/mcp_apps/awaiting_submission.py``).
The hooks that judge a call's outcome (``AgentStatusHook``,
``ToolCensusHook``) ask :func:`call_awaits_user_submission` so such a call
reads as "waiting for you", not as a failure.
"""

from __future__ import annotations

from typing import Any

from apis.shared.mcp_apps.awaiting_submission import has_awaiting_marker


def call_awaits_user_submission(event: Any) -> bool:
    """True when a finished tool call is an App form waiting for the user."""
    if getattr(event, "exception", None) is not None:
        return False
    return has_awaiting_marker(getattr(event, "result", None))
