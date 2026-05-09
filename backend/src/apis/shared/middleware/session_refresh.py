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
import secrets
import time
from typing import Optional

from botocore.exceptions import ClientError

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
from apis.shared.sessions_bff.single_flight import resolve_once

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
        refresh_lock_ttl_seconds: int = 30,
    ) -> None:
        super().__init__(app)
        self._config = config
        self._repository = repository
        self._cookie_codec = cookie_codec
        self._refresh_client = refresh_client
        self._cache = cache
        # Cross-task refresh lock TTL. A leader that crashes mid-refresh
        # strands the lock for at most this many seconds, after which any
        # peer can re-acquire and retry. Followers poll for at most this
        # long before falling back to terminal. 30s is a safety cushion
        # over the worst-case (Cognito + DDB + retries) refresh latency.
        self._refresh_lock_ttl_seconds = refresh_lock_ttl_seconds
        # Strong-reference set for fire-and-forget slide-write tasks.
        # Without keeping a reference, `asyncio.create_task(...)` can be
        # garbage-collected mid-execution — Python's docs explicitly warn
        # about this, and on fast CI runners the task dies before the
        # scheduler picks it up. We remove each task via `add_done_callback`
        # so the set doesn't grow unboundedly.
        self._slide_tasks: set[asyncio.Task] = set()

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

        The DDB `touch_last_seen` write is dispatched as a detached
        `asyncio.Task` — the response path must not wait on it. The local
        cache is updated synchronously BEFORE scheduling so subsequent
        same-request reads (and the next cache window) see the slid state
        even if the background write hasn't landed yet. Today's "swallow
        failures" semantics are preserved inside `_slide_write_task`.
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

        # Reflect the slide locally BEFORE dispatching the background write
        # so subsequent same-request reads (and the cache) don't think the
        # row still needs a slide — even if the background task hasn't yet
        # landed the DDB write.
        record.last_seen_at = now
        record.ttl = new_ttl
        if self._cache is not None:
            self._cache.set(record)

        # Fire-and-forget: the response path MUST NOT wait on the DDB write.
        # Failures are swallowed inside `_slide_write_task` (preserving
        # today's "slide failures are non-fatal" semantics — the user still
        # has a valid session for the rest of its current TTL).
        #
        # CRITICAL: keep a strong reference on the middleware instance
        # (`self._slide_tasks`). Without this, Python is free to GC the
        # task before it runs — we observed this on Python 3.12 CI runners
        # where the preservation tests saw 0 update_item calls because the
        # task was collected mid-flight. The done-callback removes the task
        # again so the set doesn't leak.
        task = asyncio.create_task(
            self._slide_write_task(
                session_id=record.session_id,
                last_seen_at=now,
                ttl=new_ttl,
            )
        )
        self._slide_tasks.add(task)
        task.add_done_callback(self._slide_tasks.discard)
        return new_max_age

    async def _slide_write_task(
        self, *, session_id: str, last_seen_at: int, ttl: int
    ) -> None:
        """Background helper for `_maybe_slide`'s fire-and-forget DDB write.

        Swallows exceptions so a DDB blip doesn't surface as an unhandled
        task exception — today's inline slide-write already swallowed
        failures, and we preserve that contract verbatim. The local cache
        was updated synchronously in `_maybe_slide` before this task was
        scheduled, so the user keeps seeing the slid state for the rest of
        their current cache window.
        """
        try:
            await self._repository.touch_last_seen(
                session_id, last_seen_at=last_seen_at, ttl=ttl
            )
        except Exception as exc:
            logger.warning(
                "BFF session slide failed for %s: %s", session_id, exc
            )

    async def _persist_refresh(
        self,
        *,
        session_id: str,
        refreshed,
        last_seen_at: int,
        ttl: int,
        rotated: bool,
        lock_owner: str,
    ) -> bool:
        """Write refreshed tokens to DDB. Retry when rotation makes it critical.

        Returns True on success or on a benign (non-rotation) failure. Returns
        False only when rotation happened *and* every retry failed — caller
        should treat that as session-unrecoverable.

        The write also clears the cross-task refresh lock (atomic with the
        token rotation), conditional on `lock_owner` matching. A
        `ConditionalCheckFailedException` here means a peer task acquired
        the lock after ours expired — we abandon the persist and the caller
        should re-read DDB to adopt the peer's tokens.
        """
        # Three attempts on rotation (≈350ms total worst-case), single shot
        # otherwise. boto3 already retries below us for transient API errors;
        # this layer handles longer blips and validation/throttling errors
        # that boto3 lets through.
        attempts = 3 if rotated else 1
        last_exc: Optional[Exception] = None
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
                    expected_lock_owner=lock_owner,
                )
                return True
            except ClientError as exc:
                # Lock-ownership condition failed — a peer task took over.
                # Don't retry: their refresh is authoritative now. Caller
                # adopts their tokens via the post-failure DDB re-read.
                if (
                    exc.response.get("Error", {}).get("Code")
                    == "ConditionalCheckFailedException"
                ):
                    logger.info(
                        "BFF refresh persist for %s lost lock to a peer task — "
                        "deferring to peer's tokens.",
                        session_id,
                    )
                    return False
                last_exc = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.05 * (2**attempt))  # 50ms, 100ms
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

    async def _wait_for_peer_refresh(
        self,
        *,
        session_id: str,
        previous: SessionRecord,
        max_wait_seconds: float,
    ) -> Optional[SessionRecord]:
        """Poll DDB for a peer task's freshly persisted tokens.

        Called when we lost the cross-task refresh lock to a peer. Polls
        the session row with bounded backoff (50ms → 500ms) until we
        observe tokens that differ from `previous` — at which point we
        adopt them — or `max_wait_seconds` elapses.

        Returns the peer's record on success, or `None` if we timed out
        (peer crashed mid-refresh). The caller treats `None` as terminal
        and clears the cookie; the lock TTL ensures the next request can
        re-acquire and retry without waiting for a stuck row.
        """
        deadline = time.monotonic() + max_wait_seconds
        sleep_for = 0.05
        while time.monotonic() < deadline:
            await asyncio.sleep(sleep_for)
            peer = await self._repository.get(session_id)
            if peer is None:
                # Row gone (delete or TTL eviction) — terminal.
                return None
            # Refresh-token rotation: peer issued a new refresh token, ours
            # is now revoked. Adopt their record.
            if peer.cognito_refresh_token != previous.cognito_refresh_token:
                return peer
            # No rotation but a fresh access token landed: peer refreshed
            # successfully, we can use the new access token.
            if (
                peer.cognito_access_token != previous.cognito_access_token
                and peer.access_token_exp
                > int(time.time()) + self._config.refresh_leeway_seconds
            ):
                return peer
            sleep_for = min(sleep_for * 1.5, 0.5)
        return None

    async def _resolve_session(
        self, cookie_value: str
    ) -> tuple[Optional[SessionRecord], bool]:
        """Return (record, should_clear_cookie).

        `should_clear_cookie` is True when the cookie is present but
        unrecoverable — bad seal, missing row, expired TTL, or refresh failure.

        Cookie unseal happens before the single-flight wrap so a bad seal
        short-circuits without registering a Future (and without keying the
        registry off an untrusted session id). Once we have a validated
        session id, the cache → `repository.get` → `needs_refresh` →
        (maybe refresh) path is coalesced through `resolve_once` so an
        Angular page-load fan-out of N same-session requests issues at most
        one DynamoDB `get_item` per cache window.

        The per-session `get_session_lock(session_id)` around the Cognito
        refresh exchange stays exactly where it is today — the single-flight
        sits upstream of it. In the common case that the single-flight
        already coalesces N callers to one loader invocation, only the
        leader ever reaches the refresh lock; the existing one-`initiate_auth`-
        per-`session_id`-per-leeway-window contract is preserved end-to-end.
        """
        try:
            payload = self._cookie_codec.unseal(cookie_value)
        except CookieDecodeError:
            logger.info("Discarding unrecoverable BFF cookie (bad seal)")
            return None, True

        session_id = payload.session_id

        async def _loader() -> tuple[Optional[SessionRecord], bool]:
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

            # Two-tier coalescing:
            #
            # 1. `get_session_lock` (in-process): collapses N concurrent
            #    same-session callers within ONE task to a single refresh
            #    contender.
            # 2. `try_acquire_refresh_lock` (cross-process, DDB conditional
            #    write): one of those contenders, across all tasks, becomes
            #    the leader and actually calls Cognito. Followers poll DDB
            #    for the leader's persisted tokens.
            #
            # Without the cross-process lock, two tasks under desiredCount: 2
            # would each call `cognito-idp:initiate_auth` with the same refresh
            # token — Cognito rotates on the first; the second fails
            # `NotAuthorizedException` and the loser's middleware clears the
            # user's cookie. The DDB lock turns that race into a leader/
            # follower handoff so exactly one Cognito refresh happens per
            # session per leeway window across the entire fleet.
            async with get_session_lock(session_id):
                current = await self._repository.get(session_id)
                if current is None:
                    return None, True
                if not current.needs_refresh(
                    int(time.time()), self._config.refresh_leeway_seconds
                ):
                    self._cache.set(current)
                    return current, False

                lock_owner = secrets.token_hex(16)
                lock_acquired = await self._repository.try_acquire_refresh_lock(
                    session_id=session_id,
                    owner=lock_owner,
                    lock_ttl_seconds=self._refresh_lock_ttl_seconds,
                )
                if not lock_acquired:
                    # FOLLOWER: a peer task is doing the Cognito refresh.
                    # Wait for their tokens to land on the row, then adopt.
                    peer = await self._wait_for_peer_refresh(
                        session_id=session_id,
                        previous=current,
                        max_wait_seconds=self._refresh_lock_ttl_seconds,
                    )
                    if peer is None:
                        # Peer never wrote — likely crashed or hit a Cognito
                        # error. Lock will TTL out; the user's next request
                        # will get to retry. Fail closed for *this* request.
                        self._cache.invalidate(session_id)
                        return None, True
                    self._cache.set(peer)
                    return peer, False

                # LEADER: do the Cognito refresh and persist.
                try:
                    refreshed = await self._refresh_client.refresh(
                        username=current.username,
                        refresh_token=current.cognito_refresh_token,
                    )
                except CognitoRefreshError:
                    # Refresh refused — release the lock so a peer can retry
                    # the next request without waiting for the full lock TTL,
                    # then treat as terminal for this request.
                    await self._repository.release_refresh_lock(
                        session_id, lock_owner
                    )
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
                    lock_owner=lock_owner,
                )
                if not persist_ok:
                    # Two reasons this lands here:
                    #   (a) Rotation persist exhausted retries — session is
                    #       unrecoverable; clear cookie and force re-auth.
                    #   (b) Lock-owner condition failed (peer took over) —
                    #       re-read DDB and adopt the peer's tokens rather
                    #       than logging the user out.
                    peer = await self._repository.get(session_id)
                    if (
                        peer is not None
                        and not peer.needs_refresh(
                            int(time.time()),
                            self._config.refresh_leeway_seconds,
                        )
                    ):
                        self._cache.set(peer)
                        return peer, False
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

        return await resolve_once(session_id, _loader)

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
