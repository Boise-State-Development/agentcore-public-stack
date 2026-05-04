"""Voice mode — ticket issuance + WebSocket proxy.

The SPA cannot ride the BFF cookie all the way to inference-api: AgentCore
Runtime's WebSocket gate validates only Cognito-issued JWTs, and the BFF
keeps those server-side. So app-api owns both halves of the voice transport:

  1. ``POST /voice/ticket``  — CSRF-protected, issues a single-use ticket
                              bound to ``{user_sub, session_id, jti, exp}``.
  2. ``WebSocket /voice/stream`` — verifies the ticket, opens an upstream
                                  WebSocket to the AgentCore Runtime using
                                  the Cognito access token from the BFF
                                  session, and bidirectionally relays JSON
                                  frames. The browser never sees a Cognito
                                  token.

See issue #211 for the migration context.
"""

from .routes import router

__all__ = ["router"]
