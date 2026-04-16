"""Hooks for Main Agent"""

from agents.main_agent.session.hooks.stop import StopHook
from agents.main_agent.session.hooks.tool_approval import (
    ToolApprovalHook,
    EmailApprovalHook,
    ExternalWriteApprovalHook,
    DangerousToolApprovalHook,
)

__all__ = [
    "StopHook",
    "ToolApprovalHook",
    "EmailApprovalHook",
    "ExternalWriteApprovalHook",
    "DangerousToolApprovalHook",
]





