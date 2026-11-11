"""The "form shown, nothing written yet" result of an MCP App write tool.

GitHub's write tools (``issue_write``, the pull-request writes) answer the
model's own call with a stub when they hand the operation to their App's
form: ``isError: true`` on purpose, so an agent that bails on error does not
chain dependent calls or claim the write happened, plus
``structuredContent: {"status": "awaiting_user_submission", ...}`` so a host
can tell it apart from a real failure (``NewToolResultAwaitingFormSubmission``
in github/github-mcp-server ``pkg/utils/result.go``). The App submits the
write itself when the user clicks Submit.

GitHub attaches that ``structuredContent`` only when the session negotiated
MCP ``2026-07-28`` (``TestTypedIssueWriteAwaitingForm``), which every session
does since the ``mcp`` 2.x upgrade, so the marker is the only signal read
here. A server on an older protocol sends a bare error, which reads as a
failure. The text match on GitHub's stub sentence that covered that case
under ``mcp`` 1.x (PR #1662) is retired
(docs/kaizen/scoping/2026-10-17-mcp-sdk-v2-upgrade.md).

The marker lives on the tool result's ``structuredContent``, which no model
formatter sends to a provider (Bedrock and OpenAI-compatible formatters keep
only ``content`` / ``toolUseId`` / ``status``), so it never reaches the
Converse payload or the cacheable prefix.
"""

from __future__ import annotations

from typing import Any, Mapping

AWAITING_USER_SUBMISSION = "awaiting_user_submission"


def has_awaiting_marker(result: Any) -> bool:
    """True when the result's ``structuredContent`` carries the marker."""
    if not isinstance(result, Mapping):
        return False
    structured = result.get("structuredContent")
    return (
        isinstance(structured, Mapping)
        and structured.get("status") == AWAITING_USER_SUBMISSION
    )
