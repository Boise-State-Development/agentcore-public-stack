"""Tests for tool approval hooks."""

import pytest
from unittest.mock import MagicMock

from agents.main_agent.session.hooks.tool_approval import (
    ToolApprovalHook,
    EmailApprovalHook,
    ExternalWriteApprovalHook,
    DangerousToolApprovalHook,
)


def _make_event(tool_name: str):
    """Create a mock BeforeToolCallEvent."""
    event = MagicMock()
    event.tool_use = {"name": tool_name}
    return event


class TestEmailApprovalHook:
    """Req AH-1: Email operations require approval."""

    def test_send_email_flagged(self):
        hook = EmailApprovalHook()
        event = _make_event("send_email")
        hook.check_approval(event)
        assert event.tool_use.get("_approval_required") is True

    def test_delete_emails_flagged(self):
        hook = EmailApprovalHook()
        event = _make_event("delete_emails")
        hook.check_approval(event)
        assert event.tool_use.get("_approval_required") is True

    def test_read_email_not_flagged(self):
        hook = EmailApprovalHook()
        event = _make_event("read_email")
        hook.check_approval(event)
        assert "_approval_required" not in event.tool_use

    def test_approval_message_set(self):
        hook = EmailApprovalHook()
        event = _make_event("send_email")
        hook.check_approval(event)
        assert "email operation" in event.tool_use["_approval_message"]


class TestExternalWriteApprovalHook:
    """Req AH-2: External system writes require approval."""

    def test_create_pr_flagged(self):
        hook = ExternalWriteApprovalHook()
        event = _make_event("create_pull_request")
        hook.check_approval(event)
        assert event.tool_use.get("_approval_required") is True

    def test_deploy_flagged(self):
        hook = ExternalWriteApprovalHook()
        event = _make_event("deploy")
        hook.check_approval(event)
        assert event.tool_use.get("_approval_required") is True

    def test_list_repos_not_flagged(self):
        hook = ExternalWriteApprovalHook()
        event = _make_event("list_repositories")
        hook.check_approval(event)
        assert "_approval_required" not in event.tool_use


class TestDangerousToolApprovalHook:
    """Req AH-3: Irreversible operations require approval."""

    def test_delete_file_flagged(self):
        hook = DangerousToolApprovalHook()
        event = _make_event("delete_file")
        hook.check_approval(event)
        assert event.tool_use.get("_approval_required") is True

    def test_execute_sql_flagged(self):
        hook = DangerousToolApprovalHook()
        event = _make_event("execute_sql")
        hook.check_approval(event)
        assert event.tool_use.get("_approval_required") is True

    def test_calculator_not_flagged(self):
        hook = DangerousToolApprovalHook()
        event = _make_event("calculator")
        hook.check_approval(event)
        assert "_approval_required" not in event.tool_use


class TestToolApprovalHookBase:
    """Req AH-4: Base hook class behavior."""

    def test_empty_tool_names_flags_nothing(self):
        hook = ToolApprovalHook()
        event = _make_event("anything")
        hook.check_approval(event)
        assert "_approval_required" not in event.tool_use

    def test_register_hooks_adds_callback(self):
        hook = EmailApprovalHook()
        registry = MagicMock()
        hook.register_hooks(registry)
        registry.add_callback.assert_called_once()
