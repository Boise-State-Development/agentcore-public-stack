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

import asyncio
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
from apis.shared.sessions_bff.cookie import (
    CookieCodec,
    CookieDecodeError,
    get_default_codec,
)
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
            # Process-wide singleton — must be the same instance the auth
            # callback used to seal the cookie, otherwise the AES key
            # diverges and every freshly-minted cookie unseals as "bad seal".
            self._cookie_codec = get_default_codec()
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
        renewal_max_age: Optional[int] = None
        if record is not None:
            request.state.bff_session = record
            request.state.bff_csrf_token = CSRFHelper.derive_token(
                record.csrf_secret, record.session_id
            )
            # Decide whether this request should slide the session forward.
            # `_maybe_slide` writes DDB if past the throttle window and returns
            # the cookie Max-Age to re-emit (or None to leave the existing
            # cookie alone).
            renewal_max_age = await self._maybe_slide(record)

        response = await call_next(request)

        if clear_cookie:
            self._clear_cookies(response)
        elif renewal_max_age is not None and record is not None:
            self._reemit_cookies(
                response,
                sealed_session_value=cookie_value,
                csrf_token=request.state.bff_csrf_token,
                max_age_seconds=renewal_max_age,
            )

        return response

    async def _maybe_slide(self, record: SessionRecord) -> Optional[int]:
        """Slide the session's DDB TTL + return a fresh cookie Max-Age.

        Returns None when no slide is warranted (within throttle window, or
        past the absolute lifetime cap). Otherwise returns the Max-Age the
        caller should write into the response's Set-Cookie headers.

        The absolute cap is enforced as `created_at + absolute_lifetime`. Once
        past it, the cookie is allowed to expire on its own original Max-Age
        — we don't extend, but we also don't proactively clear (the user
        might still complete the in-flight request).
        """
        assert self._config is not None
        now = int(time.time())
        absolute_cap = record.created_at + self._config.absolute_lifetime_seconds
        remaining_to_cap = absolute_cap - now
        if remaining_to_cap <= 0:
            return None

        new_max_age = min(self._config.session_ttl_seconds, remaining_to_cap)

        # Throttle: only write to DDB if it's been long enough since the last
        # touch. The cookie re-emit is coupled to the write so the browser
        # and DDB stay in sync about when the session should expire.
        if (
            now - record.last_seen_at
            < self._config.sliding_renewal_throttle_seconds
        ):
            return None

        new_ttl = now + new_max_age
        try:
            await self._repository.touch_last_seen(
                record.session_id, last_seen_at=now, ttl=new_ttl
            )
        except Exception as exc:
            # Don't fail the request if the slide-write fails — the user
            # still has a valid session for the rest of its current TTL.
            logger.warning(
                "BFF session slide failed for %s: %s", record.session_id, exc
            )
            return None

        # Reflect the slide locally so subsequent same-request reads (and the
        # cache) don't think the row still needs a slide.
        record.last_seen_at = now
        record.ttl = new_ttl
        if self._cache is not None:
            self._cache.set(record)
        return new_max_age

    async def _persist_refresh(
        self,
        *,
        session_id: str,
        refreshed,
        last_seen_at: int,
        ttl: int,
        rotated: bool,
    ) -> bool:
        """Write refreshed tokens to DDB. Retry when rotation makes it critical.

        Returns True on success or on a benign (non-rotation) failure. Returns
        False only when rotation happened *and* every retry failed — caller
        should treat that as session-unrecoverable.
        """
        # Three attempts on rotation (≈350ms total worst-case), single shot
        # otherwise. boto3 already retries below us for transient API errors;
        # this layer handles longer blips and validation/throttling errors
        # that boto3 lets through.
        attempts = 3 if rotated else 1
        for attempt in range(attempts):
            try:
                await self._repository.update_tokens(
                    session_id=session_id,
                    access_token=refreshed.access_token,
                    refresh_token=refreshed.refresh_token,
                    id_token=refreshed.id_token,
                    access_token_exp=refreshed.access_token_exp,
                    last_seen_at=last_seen_at,
                    ttl=ttl,
                )
                return True
            except Exception as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.05 * (2**attempt))  # 50ms, 100ms

        if rotated:
            logger.error(
                "BFF refresh-token rotation persist failed for %s after %d attempts: %s — "
                "invalidating session to force re-auth.",
                session_id,
                attempts,
                last_exc,
            )
            return False

        # No rotation — old refresh token still works, next request will retry.
        logger.warning(
            "BFF token persist failed for %s (no rotation, session still serviceable): %s",
            session_id,
            last_exc,
        )
        return True

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
            # Slide the row's DDB TTL alongside the token rotation: the user
            # is provably active. Capped at `created_at + absolute_lifetime`
            # so a long-lived browser tab can't roll the session forever.
            absolute_cap = (
                current.created_at + self._config.absolute_lifetime_seconds
            )
            new_ttl = min(
                now + self._config.session_ttl_seconds,
                absolute_cap,
            )
            # Detect refresh-token rotation. When Cognito rotates, the OLD
            # refresh token is dead the moment the new one is issued — so a
            # DDB write failure here means the session is unrecoverable on
            # the *next* request even though *this* one succeeded. Retry
            # aggressively, then fail-closed (clear cookie now) so the user
            # re-auths immediately rather than getting silently 401'd later.
            # Without rotation, the previous refresh token is still valid,
            # so a DDB write failure is benign: the next request will just
            # re-trigger refresh with the same (still good) refresh token.
            rotated = refreshed.refresh_token != current.cognito_refresh_token
            persist_ok = await self._persist_refresh(
                session_id=session_id,
                refreshed=refreshed,
                last_seen_at=now,
                ttl=new_ttl,
                rotated=rotated,
            )
            if not persist_ok:
                self._cache.invalidate(session_id)
                return None, True
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
                ttl=new_ttl,
            )
            self._cache.set(updated)
            return updated, False

    @staticmethod
    def _reemit_cookies(
        response: Response,
        *,
        sealed_session_value: str,
        csrf_token: str,
        max_age_seconds: int,
    ) -> None:
        """Re-emit both BFF cookies with a fresh `Max-Age`.

        The sealed value is the *same* one the browser already holds (cookie
        seals are stable for a given session_id) — only the Max-Age slides
        forward. Attribute set must mirror `auth/bff/cookies.py:set_session_cookies`
        exactly so the browser updates the existing cookie rather than
        creating a phantom twin.
        """
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=sealed_session_value,
            max_age=max_age_seconds,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        response.set_cookie(
            key=CSRF_COOKIE_NAME,
            value=csrf_token,
            max_age=max_age_seconds,
            path="/",
            secure=True,
            httponly=False,
            samesite="lax",
        )

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
