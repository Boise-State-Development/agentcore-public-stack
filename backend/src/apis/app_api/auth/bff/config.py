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
        )

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
