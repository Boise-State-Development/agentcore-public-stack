"""SessionRefreshMiddleware — unseals the BFF session cookie, optionally
refreshes the underlying Cognito access token, and stashes the resulting
`SessionRecord` on `request.state.bff_session` for downstream dependencies.

Dormant by default: when the request has no `__Host-bff_session` cookie (the
state during Phases 2–5), this is a fast pass-through. When `BFFConfig` is
not enabled (env vars unset, e.g. local dev or pre-Phase-1 environments),
the middleware short-circuits before doing any work.

Refresh-storm coalescing: the per-session `asyncio.Lock` from
`sessions_bff.lock` ensures multiple concurrent requests for the same
session id only trigger one Cognito refresh exchange — without this, N
parallel tab refreshes can tumble each other's refresh tokens.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from apis.shared.sessions_bff.cache import SessionCache, get_default_cache
from apis.shared.sessions_bff.config import (
    BFFConfig,
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
)
from apis.shared.sessions_bff.cookie import CookieCodec, CookieDecodeError
from apis.shared.sessions_bff.csrf import CSRFHelper
from apis.shared.sessions_bff.lock import get_session_lock
from apis.shared.sessions_bff.models import SessionRecord
from apis.shared.sessions_bff.refresh import (
    CognitoRefreshClient,
    CognitoRefreshError,
)
from apis.shared.sessions_bff.repository import SessionRepository

logger = logging.getLogger(__name__)


class SessionRefreshMiddleware(BaseHTTPMiddleware):
    """Populates `request.state.bff_session` from the session cookie.

    Construct lazily — collaborators (repo, codec, refresh client, cache)
    are created on first request so importing this module has no AWS side
    effects (matters for tests).
    """

    def __init__(
        self,
        app,
        *,
        config: Optional[BFFConfig] = None,
        repository: Optional[SessionRepository] = None,
        cookie_codec: Optional[CookieCodec] = None,
        refresh_client: Optional[CognitoRefreshClient] = None,
        cache: Optional[SessionCache] = None,
    ) -> None:
        super().__init__(app)
        self._config = config
        self._repository = repository
        self._cookie_codec = cookie_codec
        self._refresh_client = refresh_client
        self._cache = cache

    def _ensure_collaborators(self) -> None:
        if self._config is None:
            self._config = BFFConfig.from_env()
        if self._repository is None:
            self._repository = SessionRepository(
                table_name=self._config.sessions_table_name
            )
        if self._cookie_codec is None:
            self._cookie_codec = CookieCodec(
                kms_key_arn=self._config.cookie_signing_key_arn
            )
        if self._refresh_client is None:
            self._refresh_client = CognitoRefreshClient(
                app_client_id=self._config.cognito_bff_app_client_id,
                app_client_secret_arn=self._config.cognito_bff_app_client_secret_arn,
            )
        if self._cache is None:
            # Share a process-wide cache by default so the logout route can
            # invalidate the same in-memory entry the middleware just seeded.
            self._cache = get_default_cache()

    async def dispatch(self, request: Request, call_next) -> Response:
        self._ensure_collaborators()
        assert self._config is not None  # for type-checker; set by ensure

        if not self._config.is_enabled():
            return await call_next(request)

        cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
        if not cookie_value:
            # No BFF cookie — Bearer-token requests, anonymous health checks,
            # static assets. Pass through untouched.
            return await call_next(request)

        record, clear_cookie = await self._resolve_session(cookie_value)
        if record is not None:
            request.state.bff_session = record
            request.state.bff_csrf_token = CSRFHelper.derive_token(
                record.csrf_secret, record.session_id
            )

        response = await call_next(request)

        if clear_cookie:
            self._clear_cookies(response)

        return response

    async def _resolve_session(
        self, cookie_value: str
    ) -> tuple[Optional[SessionRecord], bool]:
        """Return (record, should_clear_cookie).

        `should_clear_cookie` is True when the cookie is present but
        unrecoverable — bad seal, missing row, expired TTL, or refresh failure.
        """
        try:
            payload = self._cookie_codec.unseal(cookie_value)
        except CookieDecodeError:
            logger.info("Discarding unrecoverable BFF cookie (bad seal)")
            return None, True

        session_id = payload.session_id

        cached = self._cache.get(session_id) if self._cache else None
        if cached is not None and not cached.needs_refresh(
            int(time.time()), self._config.refresh_leeway_seconds
        ):
            return cached, False

        record = await self._repository.get(session_id)
        if record is None:
            logger.info("Discarding BFF cookie — no matching session row")
            return None, True

        if not record.needs_refresh(
            int(time.time()), self._config.refresh_leeway_seconds
        ):
            self._cache.set(record)
            return record, False

        # Coalesce concurrent refreshes for the same session id.
        async with get_session_lock(session_id):
            # Re-check after acquiring the lock — another waiter may have
            # already refreshed, in which case we serve the fresh row.
            current = await self._repository.get(session_id)
            if current is None:
                return None, True
            if not current.needs_refresh(
                int(time.time()), self._config.refresh_leeway_seconds
            ):
                self._cache.set(current)
                return current, False

            try:
                refreshed = self._refresh_client.refresh(
                    username=current.username,
                    refresh_token=current.cognito_refresh_token,
                )
            except CognitoRefreshError:
                # Refresh refused — treat as terminal, force re-login.
                self._cache.invalidate(session_id)
                return None, True

            now = int(time.time())
            # TODO(phase-7): if this write fails, Cognito has rotated the
            # refresh token but DDB still holds the old one — the session is
            # silently broken on the next refresh attempt. Add a short retry
            # loop or a conditional update with a version attribute.
            await self._repository.update_tokens(
                session_id=session_id,
                access_token=refreshed.access_token,
                refresh_token=refreshed.refresh_token,
                id_token=refreshed.id_token,
                access_token_exp=refreshed.access_token_exp,
                last_seen_at=now,
            )
            updated = SessionRecord(
                session_id=current.session_id,
                user_id=current.user_id,
                username=current.username,
                cognito_access_token=refreshed.access_token,
                cognito_refresh_token=refreshed.refresh_token,
                id_token=refreshed.id_token,
                access_token_exp=refreshed.access_token_exp,
                csrf_secret=current.csrf_secret,
                created_at=current.created_at,
                last_seen_at=now,
                ttl=current.ttl,
            )
            self._cache.set(updated)
            return updated, False

    @staticmethod
    def _clear_cookies(response: Response) -> None:
        """Clear both BFF cookies on a response. Used after an unrecoverable
        cookie is detected so the browser stops sending it."""
        # `__Host-` prefix requires `Secure`, `Path=/`, and no `Domain`.
        response.delete_cookie(
            SESSION_COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax"
        )
        response.delete_cookie(
            CSRF_COOKIE_NAME, path="/", secure=True, httponly=False, samesite="lax"
        )
