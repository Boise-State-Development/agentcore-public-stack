"""Cookie-authenticated MCP App `tools/call` proxy (MCP Apps PR #5).

`docs/kaizen/scoping/mcp-apps-host-renderer.md`, decision #2. The embedded
MCP App iframe issues a JSON-RPC `tools/call` over the postMessage bridge;
the SPA relays it here. The flow mirrors the BFF chat proxy:

  iframe → SPA bridge → app-api `/mcp-apps/proxy-call` → inference-api
  `/invocations` (app_tool_call directive) → MCP server → reverse path

This handler is the **session-cookie boundary**: it authenticates the
caller and forwards the conversation binding (sessionId + originating
toolUseId) so the proxied call inherits provenance. It deliberately does
NOT decide tool visibility — `_meta.ui.visibility` is derived live from the
MCP server and only the inference-api process holds that catalog, so the
authoritative spec-MUST "reject tools whose visibility excludes 'app'"
gate lives in the inference-api dispatch (`dispatch_app_tool_call`). This
boundary's contribution is auth + request validation + the bearer hand-off.

Gated by `AGENTCORE_MCP_APPS_HOST_ENABLED` (default true since PR #7):
with the host flag off the inference-api catalog is empty and every call
is rejected there as not app-visible.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from apis.app_api.chat import proxy_routes
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.mcp_apps.card_store import get_app_card_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp-apps", tags=["mcp-apps"])


class ProxyToolCallRequest(BaseModel):
    """A `tools/call` proxied from an embedded MCP App.

    `enabledTools` / `modelId` carry the conversation's configuration so
    inference-api rebuilds the same agent shape (the MCP client that hosts
    the tool must be loaded). The SPA already has these — it sends them on
    every chat turn.
    """

    session_id: str = Field(..., alias="sessionId")
    tool_use_id: str = Field(..., alias="toolUseId")
    tool_name: str = Field(..., alias="toolName")
    arguments: Dict[str, Any] = Field(default_factory=dict)
    enabled_tools: List[str] = Field(default_factory=list, alias="enabledTools")
    model_id: Optional[str] = Field(default=None, alias="modelId")

    model_config = {"populate_by_name": True}


@router.post("/proxy-call")
async def proxy_call(
    body: ProxyToolCallRequest,
    request: Request,
    current_user: User = Depends(get_current_user_from_session),
) -> JSONResponse:
    """Relay an app-initiated tool call to inference-api and return its result.

    Non-streaming: inference-api runs the single tool (no model turn) and
    returns the `CallToolResult` as JSON; the synthesized tool_use/
    tool_result land in the conversation thread via the per-session broker.
    """
    invocation_body = {
        "session_id": body.session_id,
        "enabled_tools": body.enabled_tools,
        "model_id": body.model_id,
        "app_tool_call": {
            "tool_use_id": body.tool_use_id,
            "tool_name": body.tool_name,
            "arguments": body.arguments,
        },
    }

    target_url = proxy_routes._build_invocations_url(
        proxy_routes._inference_api_url()
    )
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {current_user.raw_token}",
    }

    client = proxy_routes._build_upstream_client()
    try:
        response = await client.post(
            target_url, headers=headers, json=invocation_body
        )
    except httpx.ConnectError:
        logger.error("Cannot reach Inference API at %s", target_url)
        raise HTTPException(status_code=502, detail="Inference API is unreachable")
    except httpx.TimeoutException:
        logger.error("Inference API request timed out: %s", target_url)
        raise HTTPException(status_code=504, detail="Inference API request timed out")
    except Exception as exc:  # noqa: BLE001
        logger.error("MCP Apps proxy-call error: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail="Proxy error")
    finally:
        await client.aclose()

    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - upstream returned non-JSON
        raise HTTPException(status_code=502, detail="Bad upstream response")

    # Option A (PR #6): on success, persist a static provenance card so the
    # call survives a page reload (the broker is in-memory; the live thread
    # event is otherwise lost on refresh). Best-effort + provenance-only —
    # model-visible state flows through ui/update-model-context, never a
    # persisted synthetic tool turn. A failed write must not fail the call.
    if response.status_code == 200 and isinstance(payload, dict):
        result = payload.get("result") or {}
        try:
            get_app_card_store().store(
                user_id=current_user.user_id,
                session_id=body.session_id,
                tool_use_id=body.tool_use_id,
                tool_name=body.tool_name,
                arguments=body.arguments,
                content=result.get("content") or [],
                is_error=bool(result.get("isError")),
            )
        except Exception:  # noqa: BLE001 - provenance is best-effort
            logger.warning(
                "mcp-apps: failed to persist provenance card", exc_info=True
            )

    # Relay inference-api's status verbatim (403 not-app-visible, 409 no
    # live client, 502 tool failure, 200 success) so the bridge can answer
    # the iframe's JSON-RPC with the right error.
    return JSONResponse(payload, status_code=response.status_code)


class ProxyContextUpdateRequest(BaseModel):
    """App-pushed model context proxied from an embedded MCP App (PR #6).

    The iframe's `ui/update-model-context` params are `{content?,
    structuredContent?}`; `resourceUri` is the bound App resource the SPA
    already holds (the `ui_resource` event's `resourceUri`) and is the
    host's per-App dedupe key. `enabledTools` / `modelId` carry the
    conversation config so inference-api rebuilds the same cached agent.
    """

    session_id: str = Field(..., alias="sessionId")
    resource_uri: str = Field(..., alias="resourceUri")
    content: Optional[List[Dict[str, Any]]] = None
    structured_content: Optional[Dict[str, Any]] = Field(
        default=None, alias="structuredContent"
    )
    enabled_tools: List[str] = Field(default_factory=list, alias="enabledTools")
    model_id: Optional[str] = Field(default=None, alias="modelId")

    model_config = {"populate_by_name": True}


@router.post("/update-context")
async def update_context(
    body: ProxyContextUpdateRequest,
    request: Request,
    current_user: User = Depends(get_current_user_from_session),
) -> JSONResponse:
    """Relay an app-pushed model-context update to inference-api.

    Non-streaming, runs no model turn: inference-api stashes the payload on
    the conversation agent's Strands state; the next real user turn merges
    it. Mirrors `/proxy-call`'s auth + bearer hand-off — this boundary's
    only job is the session-cookie → bearer exchange.
    """
    invocation_body = {
        "session_id": body.session_id,
        "enabled_tools": body.enabled_tools,
        "model_id": body.model_id,
        "app_context_update": {
            "resource_uri": body.resource_uri,
            "content": body.content,
            "structured_content": body.structured_content,
        },
    }

    target_url = proxy_routes._build_invocations_url(
        proxy_routes._inference_api_url()
    )
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {current_user.raw_token}",
    }

    client = proxy_routes._build_upstream_client()
    try:
        response = await client.post(
            target_url, headers=headers, json=invocation_body
        )
    except httpx.ConnectError:
        logger.error("Cannot reach Inference API at %s", target_url)
        raise HTTPException(status_code=502, detail="Inference API is unreachable")
    except httpx.TimeoutException:
        logger.error("Inference API request timed out: %s", target_url)
        raise HTTPException(status_code=504, detail="Inference API request timed out")
    except Exception as exc:  # noqa: BLE001
        logger.error("MCP Apps update-context error: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail="Proxy error")
    finally:
        await client.aclose()

    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - upstream returned non-JSON
        raise HTTPException(status_code=502, detail="Bad upstream response")

    return JSONResponse(payload, status_code=response.status_code)


@router.get("/cards")
async def list_cards(
    session_id: str,
    current_user: User = Depends(get_current_user_from_session),
) -> JSONResponse:
    """Return this user's app-initiated tool-call cards for a session.

    Reload hydration for Option A: the SPA replays these as *static
    historical cards* (the App iframe itself is not re-instantiated).
    Ownership is re-checked in the store against a guessed session id.
    """
    cards = get_app_card_store().list_for_session(
        session_id=session_id, user_id=current_user.user_id
    )
    return JSONResponse({"cards": cards})
