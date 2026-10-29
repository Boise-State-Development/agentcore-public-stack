"""BFF Token Handler auth routes (Phase 3).

Four endpoints — `/auth/login`, `/auth/callback`, `/auth/session`,
`/auth/logout` — that own the server-side OAuth2 authorization-code flow
against the *confidential* Cognito BFF app client. The browser only ever
sees opaque sealed cookies; Cognito tokens are persisted in DynamoDB by
`apis.shared.sessions_bff.SessionRepository`.

These routes are deliberately separated from the existing `/auth/providers`
endpoint in `apis.app_api.auth.routes` so the BFF flow can be enabled,
versioned, and (eventually) renamed without touching the public provider
listing the SPA already depends on.

`cli_router` adds the device-authorization flow (`/auth/cli/*`) the terminal
client uses to obtain one of these same sessions without a browser redirect
it can receive.
"""

from .cli_routes import router as cli_router
from .routes import router

__all__ = ["cli_router", "router"]
