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

Inert until PR #7 flips `AGENTCORE_MCP_APPS_HOST_ENABLED`: with the host
flag off the inference-api catalog is empty and every call is rejected
there as not app-visible.
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

    # Relay inference-api's status verbatim (403 not-app-visible, 409 no
    # live client, 502 tool failure, 200 success) so the bridge can answer
    # the iframe's JSON-RPC with the right error.
    return JSONResponse(payload, status_code=response.status_code)
