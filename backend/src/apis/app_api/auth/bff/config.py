"""Resolved configuration for the BFF auth routes.

The `sessions_bff.BFFConfig` dataclass owns the env vars shared with the
middleware. This module adds the three values that only the auth routes
care about — Cognito Hosted UI base URL (already on the task as
`COGNITO_DOMAIN_URL`), the BFF callback URL we registered on the Cognito
client in CDK, and the post-login redirect we hand the browser after
writing the cookies.

Resolved lazily so importing the routes module does no env-var work.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from apis.shared.sessions_bff.config import BFFConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BFFAuthConfig:
    """Env-resolved configuration for the BFF auth routes."""

    bff_config: BFFConfig
    cognito_domain_url: Optional[str]
    callback_url: Optional[str]
    post_login_redirect_url: str
    # Redirect URIs a native app may ask the login to end on
    # (`NATIVE_AUTH_REDIRECT_URIS`, comma-separated, empty by default). The
    # list is the native sign-in's only switch: while it is empty the native
    # login exit and `POST /auth/native/exchange` answer 404
    # (docs/specs/native-app-auth.md D7). Compared as exact strings.
    native_redirect_uris: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "BFFAuthConfig":
        cognito_domain = os.environ.get("COGNITO_DOMAIN_URL") or None
        callback = os.environ.get("BFF_AUTH_CALLBACK_URL") or None
        post_login = os.environ.get("BFF_POST_LOGIN_REDIRECT_URL") or "/"
        return cls(
            bff_config=BFFConfig.from_env(),
            cognito_domain_url=cognito_domain.rstrip("/") if cognito_domain else None,
            callback_url=callback,
            post_login_redirect_url=post_login,
            native_redirect_uris=parse_native_redirect_uris(
                os.environ.get("NATIVE_AUTH_REDIRECT_URIS")
            ),
        )

    @property
    def native_auth_enabled(self) -> bool:
        return bool(self.native_redirect_uris)

    def allows_native_redirect(self, redirect_uri: Optional[str]) -> bool:
        """Exact-string allowlist check. No normalisation, no patterns."""
        return bool(redirect_uri) and redirect_uri in self.native_redirect_uris

    def is_ready(self) -> bool:
        """True iff every value the auth routes need at runtime is resolved.

        The routes themselves still register on import so a misconfigured
        environment surfaces a 503-style failure on the request, not a
        startup crash that hides the real cause from operators.
        """
        return bool(
            self.bff_config.is_enabled()
            and self.bff_config.cognito_bff_app_client_id
            and self.bff_config.cognito_bff_app_client_secret_arn
            and self.cognito_domain_url
            and self.callback_url
        )


def parse_native_redirect_uris(raw: Optional[str]) -> tuple[str, ...]:
    """Split `NATIVE_AUTH_REDIRECT_URIS` into its entries.

    An unset GitHub variable arrives as "", which is the empty list. Entries
    are trimmed and blanks dropped; nothing else is normalised, because the
    check is an exact match against what the app sends.
    """
    if not raw:
        return ()
    return tuple(entry.strip() for entry in raw.split(",") if entry.strip())
