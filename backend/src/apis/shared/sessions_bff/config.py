"""Configuration for the BFF Token Handler.

Reads the seven env vars wired by Phase 1 CDK on the app-api task. The whole
package is gated by `is_enabled()` — when `BFF_SESSIONS_TABLE_NAME` is unset
(local dev, environments before Phase 1 deploys), every BFF code path becomes
a no-op so the existing Bearer flow keeps working.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Cookie name. The `__Host-` prefix forbids `Domain` and requires `Secure` +
# `Path=/`, which gives us same-origin-only delivery for free.
SESSION_COOKIE_NAME = "__Host-bff_session"
CSRF_COOKIE_NAME = "__Host-bff_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"

# Defaults for the optional env vars. Match the Phase 1 CDK defaults.
_DEFAULT_TTL_SECONDS = 28800  # 8 hours
_DEFAULT_REFRESH_LEEWAY_SECONDS = 60
# Hard cap on a single login's lifetime regardless of activity. 30d matches
# the Cognito refresh-token default — past that, the upstream refresh would
# fail anyway, so there's no value in carrying the cookie further.
_DEFAULT_ABSOLUTE_LIFETIME_SECONDS = 30 * 24 * 3600
# Don't write to DDB / re-emit cookies on every request; coalesce to once per
# 5 minutes. Kept a strict multiple of `_DEFAULT_REFRESH_LEEWAY_SECONDS` (60s)
# so cache-expiry (TTL = leeway) and slide-throttle boundaries are never
# aligned — without this, a single request crossing the 60s boundary would
# incur BOTH a `get_item` AND an `update_item` on the critical path. De-
# alignment keeps a cache-miss request from also paying the slide-write cost.
_DEFAULT_SLIDING_RENEWAL_THROTTLE_SECONDS = 60 * 5


@dataclass(frozen=True)
class BFFConfig:
    """Resolved BFF configuration from env vars.

    Construct with `BFFConfig.from_env()`. The dataclass is frozen so callers
    can cache it without worrying about mutation.
    """

    sessions_table_name: Optional[str]
    cookie_signing_key_arn: Optional[str]
    session_ttl_seconds: int
    refresh_leeway_seconds: int
    cognito_bff_app_client_id: Optional[str]
    cognito_bff_app_client_secret_arn: Optional[str]
    inference_api_url: Optional[str]
    # Defaults provided so test fixtures and any direct constructor call sites
    # don't have to learn about these fields. `from_env()` is what production
    # uses and it always supplies values.
    absolute_lifetime_seconds: int = _DEFAULT_ABSOLUTE_LIFETIME_SECONDS
    sliding_renewal_throttle_seconds: int = _DEFAULT_SLIDING_RENEWAL_THROTTLE_SECONDS

    @classmethod
    def from_env(cls) -> "BFFConfig":
        return cls(
            sessions_table_name=os.environ.get("BFF_SESSIONS_TABLE_NAME") or None,
            cookie_signing_key_arn=os.environ.get("BFF_COOKIE_SIGNING_KEY_ARN") or None,
            session_ttl_seconds=int(
                os.environ.get("BFF_SESSION_TTL_SECONDS") or _DEFAULT_TTL_SECONDS
            ),
            refresh_leeway_seconds=int(
                os.environ.get("BFF_SESSION_REFRESH_LEEWAY_SECONDS")
                or _DEFAULT_REFRESH_LEEWAY_SECONDS
            ),
            absolute_lifetime_seconds=int(
                os.environ.get("BFF_SESSION_ABSOLUTE_LIFETIME_SECONDS")
                or _DEFAULT_ABSOLUTE_LIFETIME_SECONDS
            ),
            sliding_renewal_throttle_seconds=int(
                os.environ.get("BFF_SESSION_SLIDING_RENEWAL_THROTTLE_SECONDS")
                or _DEFAULT_SLIDING_RENEWAL_THROTTLE_SECONDS
            ),
            cognito_bff_app_client_id=os.environ.get("COGNITO_BFF_APP_CLIENT_ID") or None,
            cognito_bff_app_client_secret_arn=os.environ.get(
                "COGNITO_BFF_APP_CLIENT_SECRET_ARN"
            )
            or None,
            inference_api_url=os.environ.get("INFERENCE_API_URL") or None,
        )

    def is_enabled(self) -> bool:
        """True when the BFF backplane is provisioned and the package should
        activate. Until Phase 1 CDK is deployed (or in local dev without the
        env vars set), this is False and middleware/dependencies short-circuit
        as pass-throughs."""
        return bool(self.sessions_table_name and self.cookie_signing_key_arn)
