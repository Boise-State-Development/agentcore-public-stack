"""The "form shown, nothing written yet" result of an MCP App write tool.

GitHub's write tools (``issue_write``, the pull-request writes) answer the
model's own call with a stub when they hand the operation to their App's
form: ``isError: true`` on purpose, so an agent that bails on error does not
chain dependent calls or claim the write happened, plus
``structuredContent: {"status": "awaiting_user_submission", ...}`` so a host
can tell it apart from a real failure (``NewToolResultAwaitingFormSubmission``
in github/github-mcp-server ``pkg/utils/result.go``). The App submits the
write itself when the user clicks Submit.

GitHub only attaches that ``structuredContent`` when the session negotiated
MCP ``2026-07-28`` (``TestTypedIssueWriteAwaitingForm``). The ``mcp`` 1.x SDK
we run negotiates ``2025-11-25``, so on our sessions the stub arrives as a
bare error whose text opens with :data:`_GITHUB_FORM_STUB_PREFIX`.
:func:`awaiting_user_submission` recognizes both shapes, and
:func:`mark_awaiting_user_submission` writes the marker onto the second so
everything downstream (the ``tool_result`` SSE, the persisted message, the
SPA) reads one shape. The text match retires once an ``mcp`` 2.x upgrade
negotiates ``2026-07-28`` and GitHub sends the marker itself.

The marker lives on the tool result's ``structuredContent``, which no model
formatter sends to a provider (Bedrock and OpenAI-compatible formatters keep
only ``content`` / ``toolUseId`` / ``status``), so it never reaches the
Converse payload or the cacheable prefix. ``status`` and ``isError`` are left
alone: the model must still read the call as "not done".
"""

from __future__ import annotations

from typing import Any, Mapping

AWAITING_USER_SUBMISSION = "awaiting_user_submission"

# GitHub's create and update stubs both open with this sentence
# (``issueWriteAwaitingFormResult`` in pkg/github/issues.go and its
# pull-request counterpart).
_GITHUB_FORM_STUB_PREFIX = "An interactive form has been shown to the user"


def has_awaiting_marker(result: Any) -> bool:
    """True when the result's ``structuredContent`` carries the marker."""
    if not isinstance(result, Mapping):
        return False
    structured = result.get("structuredContent")
    return (
        isinstance(structured, Mapping)
        and structured.get("status") == AWAITING_USER_SUBMISSION
    )


def _is_form_stub_text(result: Mapping[str, Any]) -> bool:
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return False
    first = content[0]
    text = first.get("text") if isinstance(first, Mapping) else None
    return isinstance(text, str) and text.startswith(_GITHUB_FORM_STUB_PREFIX)


def awaiting_user_submission(result: Any, *, is_app_tool: bool) -> bool:
    """True when ``result`` means "the App's form is waiting for the user".

    The marker counts wherever it appears. The text fallback counts only for
    an error result from a tool that has an MCP App (``_meta.ui``), so a
    model-only tool that happens to say the same sentence is never relabeled.
    """
    if has_awaiting_marker(result):
        return True
    if not is_app_tool or not isinstance(result, Mapping):
        return False
    if result.get("status") != "error" and result.get("isError") is not True:
        return False
    return _is_form_stub_text(result)


def mark_awaiting_user_submission(result: Any, *, is_app_tool: bool) -> bool:
    """Write the marker onto a recognized stub that lacks it, in place.

    Returns True when the result is an awaiting stub (marked now or already).
    A result that already has a ``structuredContent`` of its own is never
    overwritten.
    """
    if not awaiting_user_submission(result, is_app_tool=is_app_tool):
        return False
    if result.get("structuredContent") is None:
        result["structuredContent"] = {"status": AWAITING_USER_SUBMISSION}
    return True
