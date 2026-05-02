"""BFF Token Handler — server-side session storage backing httpOnly cookies.

This package implements the dormant infrastructure for the Bearer→cookie
migration (Phase 2). Browsers receive an opaque, AES-GCM-sealed session id in
a `__Host-bff_session` cookie; tokens stay in DynamoDB and never reach the
client. The cookie value is unsealed by `CookieCodec`, the row is fetched by
`SessionRepository`, and the resulting `SessionRecord` is attached to
`request.state.bff_session` by `SessionRefreshMiddleware`.

The package is no-op-by-default: `BFFConfig.is_enabled()` returns False unless
the Phase 1 CDK env vars are present, which keeps local dev and the existing
Bearer-token path untouched until Phase 6 cutover.
"""

from .config import BFFConfig
from .cookie import CookieCodec, CookieDecodeError
from .csrf import CSRFHelper
from .lock import get_session_lock
from .models import CookiePayload, SessionRecord
from .repository import SessionRepository

__all__ = [
    "BFFConfig",
    "CookieCodec",
    "CookieDecodeError",
    "CookiePayload",
    "CSRFHelper",
    "SessionRecord",
    "SessionRepository",
    "get_session_lock",
]
