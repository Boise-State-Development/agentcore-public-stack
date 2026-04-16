"""
Tool approval hooks for gating dangerous operations.

These hooks intercept tool calls before execution and can request
user confirmation for operations that modify external systems.

Based on the approval hook pattern from:
https://github.com/aws-samples/sample-strands-agent-with-agentcore
"""

import logging
from typing import Any, Set

from strands.hooks import HookProvider, HookRegistry, BeforeToolCallEvent

logger = logging.getLogger(__name__)


class ToolApprovalHook(HookProvider):
    """
    Base approval hook that gates specified tool names.

    Subclasses define which tool names require approval and what
    message to show the user. The hook sets the approval_required
    flag on the event, which the streaming layer can surface to
    the client for user confirmation.
    """

    # Subclasses override these
    tool_names: Set[str] = set()
    approval_message: str = "This operation requires approval."

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.check_approval)

    def check_approval(self, event: BeforeToolCallEvent) -> None:
        """Check if the tool call requires user approval."""
        tool_name = event.tool_use.get("name", "")
        if tool_name in self.tool_names:
            logger.info(f"Tool approval required: {tool_name}")
            # The approval_required attribute signals the streaming layer
            # to elicit user confirmation before proceeding
            event.tool_use["_approval_required"] = True
            event.tool_use["_approval_message"] = self.approval_message


class EmailApprovalHook(ToolApprovalHook):
    """Gate bulk email operations (send, delete, forward)."""

    tool_names = {
        "send_email",
        "send_bulk_email",
        "delete_emails",
        "forward_email",
    }
    approval_message = (
        "This tool will perform an email operation. "
        "Please confirm you want to proceed."
    )


class ExternalWriteApprovalHook(ToolApprovalHook):
    """Gate operations that write to external systems (GitHub, APIs, etc.)."""

    tool_names = {
        "create_pull_request",
        "merge_pull_request",
        "create_issue",
        "push_code",
        "deploy",
        "delete_repository",
    }
    approval_message = (
        "This tool will modify an external system. "
        "Please confirm you want to proceed."
    )


class DangerousToolApprovalHook(ToolApprovalHook):
    """Gate tools with irreversible side effects."""

    tool_names = {
        "delete_file",
        "drop_table",
        "execute_sql",
    }
    approval_message = (
        "This tool performs an irreversible operation. "
        "Please confirm you want to proceed."
    )
