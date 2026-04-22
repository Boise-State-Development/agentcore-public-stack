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

from bedrock_agentcore.runtime import BedrockAgentCoreContext
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

HEADER_WORKLOAD_ACCESS_TOKEN = "WorkloadAccessToken"
HEADER_OAUTH2_CALLBACK_URL = "OAuth2CallbackUrl"
HEADER_SESSION_ID = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
HEADER_REQUEST_ID = "X-Amzn-Request-Id"


class AgentCoreContextMiddleware(BaseHTTPMiddleware):
    """Populates BedrockAgentCoreContext from Runtime request headers."""

    async def dispatch(self, request: Request, call_next) -> Response:
        workload_token = request.headers.get(HEADER_WORKLOAD_ACCESS_TOKEN)
        if workload_token:
            BedrockAgentCoreContext.set_workload_access_token(workload_token)

        callback_url = request.headers.get(HEADER_OAUTH2_CALLBACK_URL)
        if callback_url:
            BedrockAgentCoreContext.set_oauth2_callback_url(callback_url)

        session_id = request.headers.get(HEADER_SESSION_ID)
        if session_id:
            BedrockAgentCoreContext.set_request_context(
                request_id=request.headers.get(HEADER_REQUEST_ID, ""),
                session_id=session_id,
            )

        return await call_next(request)
