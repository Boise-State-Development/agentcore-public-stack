"""Marking a first-party (local) tool as backed by a user OAuth connector.

External MCP tools are OAuth-gated through their ``MCPClient`` (the
``OAuthConsentHook``'s provider lookup maps client → provider). A first-party
tool that calls a vendor REST API on the user's behalf — Zoom, Google
Calendar, GitHub — has no client to key on, so it carries the provider id
itself: the factory that builds it calls :func:`bind_oauth_provider`, and
``BaseAgent._build_oauth_consent_hook`` reads it back with
:func:`oauth_provider_for`.

That is the whole contract. With the id on the tool object, the consent hook
treats the tool exactly like an OAuth-gated MCP tool: it warms the token
cache before the call (or pauses the turn on an ``oauth_required``
interrupt), and retries once on a 401-shaped result. The tool body then reads
its bearer from ``oauth_token_cache`` keyed on ``(user_id, provider_id)`` —
it never calls AgentCore Identity itself, because a tool body has no way to
raise the consent interrupt.

The provider id is **deployment data**, not code: it is whatever the admin
set as "Required OAuth provider" on the tool's catalog row
(``ToolDefinition.requires_oauth_provider``). Resolving it at build time and
closing over it is cache-safe because the agent cache key carries the enabled
tools' ``updated_at`` freshness hash — editing the row evicts the agent.
"""

from __future__ import annotations

from typing import Any, Optional

_ATTR = "_platform_oauth_provider_id"


def bind_oauth_provider(tool_obj: Any, provider_id: Optional[str]) -> Any:
    """Stamp ``provider_id`` on a Strands tool object and return it.

    ``None`` (the admin has not picked a connector yet) is stored as-is so the
    tool can explain the misconfiguration conversationally; the consent hook
    sees no provider and stays out of the way.
    """
    setattr(tool_obj, _ATTR, provider_id or None)
    return tool_obj


def oauth_provider_for(tool_obj: Any) -> Optional[str]:
    """The connector id a local tool was bound to, or None for any other tool."""
    return getattr(tool_obj, _ATTR, None)
