"""Hooks for Main Agent"""

from agents.main_agent.session.hooks.agent_status import AgentStatusHook
from agents.main_agent.session.hooks.context_attribution import ContextAttributionHook
from agents.main_agent.session.hooks.context_ledger import ContextLedgerHook
from agents.main_agent.session.hooks.display_text import DisplayTextHook
from agents.main_agent.session.hooks.mcp_app_awaiting import McpAppAwaitingSubmissionHook
from agents.main_agent.session.hooks.mcp_session_heal import MCPSessionHealHook
from agents.main_agent.session.hooks.oauth_consent import OAuthConsentHook
from agents.main_agent.session.hooks.prefix_fingerprint import PrefixFingerprintHook
from agents.main_agent.session.hooks.steering import SteeringHook
from agents.main_agent.session.hooks.stop import StopHook
from agents.main_agent.session.hooks.tool_approval import MCPExternalApprovalHook
from agents.main_agent.session.hooks.tool_census import ToolCensusHook
from agents.main_agent.session.hooks.web_search_defaults import WebSearchDefaultsHook

__all__ = [
    "AgentStatusHook",
    "ContextAttributionHook",
    "ContextLedgerHook",
    "DisplayTextHook",
    "McpAppAwaitingSubmissionHook",
    "MCPSessionHealHook",
    "OAuthConsentHook",
    "PrefixFingerprintHook",
    "SteeringHook",
    "StopHook",
    "MCPExternalApprovalHook",
    "ToolCensusHook",
    "WebSearchDefaultsHook",
]
