"""AgentCore Runtime context middleware.

Bridges AgentCore Runtime request headers into BedrockAgentCoreContext so that
downstream code (e.g. IdentityClient token lookups) can access the per-invocation
workload identity token without threading it through every function call.

AgentCore Runtime injects these headers on every invocation:
    - WorkloadAccessToken: per-user workload identity token, derived from the
      validated inbound JWT by the Runtime's managed JWT authorizer.
    - OAuth2CallbackUrl: OAuth2 callback URL registered on the workload identity.
    - X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: current session ID.
    - X-Amzn-Request-Id: per-request trace ID.

When the inference API is wrapped by BedrockAgentCoreApp, these are populated
automatically. Because this service runs as a plain FastAPI app inside
AgentCore Runtime, we populate the context ourselves.

The middleware is a no-op in local development where these headers are absent,
which keeps tests and `python -m main` runs working without mocks.
"""

import logging
import os
from urllib.parse import urlparse

from bedrock_agentcore.runtime import BedrockAgentCoreContext
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

HEADER_WORKLOAD_ACCESS_TOKEN = "WorkloadAccessToken"
HEADER_OAUTH2_CALLBACK_URL = "OAuth2CallbackUrl"
HEADER_SESSION_ID = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
HEADER_REQUEST_ID = "X-Amzn-Request-Id"

# The frontend always posts to `/oauth-complete` (see
# `frontend/ai.client/src/app/settings/connectors/services/user-connectors.service.ts`).
# Pinning the path closes off path-traversal and arbitrary-endpoint variants of
# the same attack class once the origin is allowlisted.
_ALLOWED_CALLBACK_PATH = "/oauth-complete"


def _allowed_callback_origins() -> frozenset[str]:
    """Origins that are allowed to set `OAuth2CallbackUrl`.

    Reuses `CORS_ORIGINS` (set by CDK on the inference-api task) as the trust
    boundary: the frontend lives at one of those origins, so its callback URL
    must too. Read at request time so tests can monkeypatch the env var.
    """
    raw = os.environ.get("CORS_ORIGINS", "")
    return frozenset(o.strip().rstrip("/") for o in raw.split(",") if o.strip())


def _is_safe_callback_url(url: str) -> bool:
    """Return True iff `url` is an allowlisted `/oauth-complete` URL.

    The header is client-supplied (see `user-connectors.service.ts`), so an
    authenticated user can otherwise pivot the OAuth redirect to an
    attacker-controlled origin and capture the authorization code on consent.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    if parsed.path != _ALLOWED_CALLBACK_PATH:
        return False
    if parsed.query or parsed.fragment:
        return False
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return origin in _allowed_callback_origins()


class AgentCoreContextMiddleware(BaseHTTPMiddleware):
    """Populates BedrockAgentCoreContext from Runtime request headers."""

    async def dispatch(self, request: Request, call_next) -> Response:
        workload_token = request.headers.get(HEADER_WORKLOAD_ACCESS_TOKEN)
        if workload_token:
            BedrockAgentCoreContext.set_workload_access_token(workload_token)

        callback_url = request.headers.get(HEADER_OAUTH2_CALLBACK_URL)
        if callback_url:
            if _is_safe_callback_url(callback_url):
                BedrockAgentCoreContext.set_oauth2_callback_url(callback_url)
            else:
                logger.warning(
                    "Rejected OAuth2CallbackUrl header: not in CORS_ORIGINS "
                    "allowlist or path != %s",
                    _ALLOWED_CALLBACK_PATH,
                )

        session_id = request.headers.get(HEADER_SESSION_ID)
        if session_id:
            BedrockAgentCoreContext.set_request_context(
                request_id=request.headers.get(HEADER_REQUEST_ID, ""),
                session_id=session_id,
            )

        return await call_next(request)
