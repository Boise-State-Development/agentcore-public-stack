"""Hooks for Main Agent"""

from agents.main_agent.session.hooks.oauth_consent import OAuthConsentHook
from agents.main_agent.session.hooks.stop import StopHook
from agents.main_agent.session.hooks.tool_approval import MCPExternalApprovalHook

__all__ = [
    "OAuthConsentHook",
    "StopHook",
    "MCPExternalApprovalHook",
]
