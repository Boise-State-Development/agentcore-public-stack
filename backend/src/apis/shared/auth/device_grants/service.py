"""Device-grant orchestration: authorize, approve, poll.

Three callers, three entry points:

``authorize``
    The CLI asks for a grant. Returns the pair of codes plus the URL to open.

``approve`` / ``deny``
    A browser that has *already* authenticated (its own BFF session was just
    minted by the callback) tells us which pending grant it is answering for.

``poll``
    The CLI presents its device code and gets back either a sealed session
    value or an RFC 8628 error code telling it what to do next.

The security-relevant ordering lives in ``poll``, and it is worth stating
explicitly because it is easy to "simplify" into a bug:

1. **Throttle before anything else.** ``slow_down`` is answered from the
   *previous* poll's timestamp, so the stamp is written after the decision.
   A client that ignores ``interval`` therefore never advances past
   ``slow_down`` — which is the point, since a tight poll loop against a
   short user code is the only guessing amplifier in this design.
2. **Read the session and seal before claiming.** Claiming is destructive
   (single-use), so every failure that can be detected without it — the
   session row having vanished, the codec being unable to reach Secrets
   Manager — is detected first. Otherwise a transient infrastructure blip
   burns the user's grant and makes them start over.
3. **Claim last, and trust only its return value.** Two concurrent polls
   both reach step 3; the repository's conditional update picks one winner
   and the loser is told ``invalid_grant``. The loser has, by then, sealed a
   perfectly valid session value — it MUST discard it. That is the one place
   in this file where returning the obvious local variable would be a
   session-sharing vulnerability.
"""

from __future__ import annotations

import logging
import os
import time
import urllib.parse
from enum import StrEnum
from typing import Optional, Union

from botocore.exceptions import ClientError

from ...sessions_bff.cookie import CookieCodec, get_default_codec
from ...sessions_bff.models import CookiePayload
from ...sessions_bff.repository import SessionRepository
from .models import (
    GRANT_TTL_SECONDS,
    POLL_INTERVAL_SECONDS,
    DeviceAuthorizationResponse,
    DeviceGrant,
    DevicePendingResponse,
    DeviceTokenResponse,
    GrantStatus,
    generate_device_code,
    generate_user_code,
    hash_device_code,
)
from .repository import DeviceGrantRepository, get_device_grant_repository

logger = logging.getLogger(__name__)

#: Attempts to find a free user code before giving up. The alphabet gives
#: 22**8 combinations against at most a handful of live grants, so a second
#: attempt is already improbable; this exists so a collision is a retry
#: rather than a user-visible failure.
_MAX_CODE_ATTEMPTS = 5


class ApprovalOutcome(StrEnum):
    """Why a browser-side approval or denial did or did not take effect.

    Distinguished so the verify page can say something true. "That code has
    already been used" and "that code has expired" send the user to different
    next actions, and both differ from a typo.
    """

    APPROVED = "approved"
    DENIED = "denied"
    #: No grant for that user code — a typo, or a code from a dead flow.
    NOT_FOUND = "not_found"
    #: The grant existed but its window closed.
    EXPIRED = "expired"
    #: Already approved, claimed, or denied. Terminal, and not re-answerable.
    ALREADY_RESOLVED = "already_resolved"


# RFC 8628 §3.5 error codes, plus `invalid_grant` for an unknown device code.
_ERR_PENDING = "authorization_pending"
_ERR_SLOW_DOWN = "slow_down"
_ERR_EXPIRED = "expired_token"
_ERR_DENIED = "access_denied"
_ERR_INVALID = "invalid_grant"

_PENDING_DESCRIPTIONS = {
    _ERR_PENDING: "Waiting for the sign-in to complete in your browser.",
    _ERR_SLOW_DOWN: "Polling too quickly — wait for the advertised interval.",
    _ERR_EXPIRED: "This sign-in request expired. Start a new one.",
    _ERR_DENIED: "The sign-in request was declined.",
    _ERR_INVALID: "This sign-in request is no longer valid. Start a new one.",
}


def _pending(error: str) -> DevicePendingResponse:
    return DevicePendingResponse(
        error=error,
        error_description=_PENDING_DESCRIPTIONS[error],
    )


def derive_verification_uri(callback_url: Optional[str]) -> Optional[str]:
    """Where to send the human, derived from the registered callback URL.

    ``BFF_AUTH_CALLBACK_URL`` is the one absolute app-api URL guaranteed to be
    configured (``BFFAuthConfig.is_ready`` requires it, because Cognito
    matches it byte-for-byte). The verify page is its sibling, so deriving
    beats introducing a second env var that can drift out of agreement with
    the first.

    ``https://host/api/auth/callback`` -> ``https://host/api/auth/cli/verify``

    ``BFF_CLI_VERIFICATION_URL`` overrides this outright for deployments whose
    routing makes the sibling assumption wrong.
    """
    override = os.environ.get("BFF_CLI_VERIFICATION_URL")
    if override:
        return override.rstrip("/")
    if not callback_url:
        return None
    parts = urllib.parse.urlsplit(callback_url)
    parent = parts.path.rstrip("/").rsplit("/", 1)[0]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, f"{parent}/cli/verify", "", ""))


class DeviceGrantService:
    """Create / approve / claim device grants.

    Every collaborator is injectable because the interesting tests here are
    about ordering and race outcomes, not about AWS.
    """

    def __init__(
        self,
        *,
        repository: Optional[DeviceGrantRepository] = None,
        session_repository: Optional[SessionRepository] = None,
        codec: Optional[CookieCodec] = None,
        verification_uri: Optional[str] = None,
        grant_ttl_seconds: int = GRANT_TTL_SECONDS,
        poll_interval_seconds: int = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._repository = repository or get_device_grant_repository()
        self._session_repository = session_repository or SessionRepository()
        self._codec = codec or get_default_codec()
        # Derived from the env var rather than from `app_api`'s BFFAuthConfig:
        # `apis.shared` must not import from `app_api` (enforced by
        # tests/architecture/test_import_boundaries.py).
        self._verification_uri = verification_uri or derive_verification_uri(os.environ.get("BFF_AUTH_CALLBACK_URL"))
        self._grant_ttl_seconds = grant_ttl_seconds
        self._poll_interval_seconds = poll_interval_seconds

    @property
    def enabled(self) -> bool:
        """True when every backing dependency is configured.

        The routes surface this as a 503 rather than failing at import, so a
        deployment missing the BFF backplane reports a clear cause on the
        request instead of refusing to start.
        """
        return bool(self._repository.enabled and self._session_repository.enabled and self._verification_uri)

    # ------------------------------------------------------------------
    # CLI: start
    # ------------------------------------------------------------------

    async def authorize(self, *, now: Optional[int] = None) -> DeviceAuthorizationResponse:
        """Mint a pending grant and describe how to complete it.

        Retries on a user-code collision. The device code is regenerated
        alongside it: the pair is written in one transaction, so on a
        collision we do not know which key was taken, and reusing a device
        code whose hash might already exist would fail identically forever.
        """
        stamp = now if now is not None else int(time.time())
        expires_at = stamp + self._grant_ttl_seconds

        last_error: Optional[ClientError] = None
        for attempt in range(_MAX_CODE_ATTEMPTS):
            device_code = generate_device_code()
            user_code = generate_user_code()
            grant = DeviceGrant(
                device_code_hash=hash_device_code(device_code),
                user_code=user_code,
                status=GrantStatus.PENDING,
                created_at=stamp,
                expires_at=expires_at,
            )
            try:
                await self._repository.create(grant)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code != "TransactionCanceledException":
                    raise
                last_error = exc
                logger.warning(
                    "Device grant code collision (attempt %d/%d); regenerating",
                    attempt + 1,
                    _MAX_CODE_ATTEMPTS,
                )
                continue

            return DeviceAuthorizationResponse(
                device_code=device_code,
                user_code=user_code,
                verification_uri=self._verification_uri or "",
                verification_uri_complete=self._complete_uri(user_code),
                expires_in=self._grant_ttl_seconds,
                interval=self._poll_interval_seconds,
            )

        raise RuntimeError(f"Could not allocate a free device-grant code after " f"{_MAX_CODE_ATTEMPTS} attempts") from last_error

    def _complete_uri(self, user_code: str) -> str:
        base = self._verification_uri or ""
        query = urllib.parse.urlencode({"user_code": user_code})
        return f"{base}?{query}"

    # ------------------------------------------------------------------
    # Browser: approve / deny
    # ------------------------------------------------------------------

    async def approve(
        self,
        *,
        user_code: str,
        session_id: str,
        user_id: str,
        now: Optional[int] = None,
    ) -> ApprovalOutcome:
        """Attach an authenticated browser's freshly minted session to a grant.

        The caller must have authenticated the browser independently — the
        user code proves nothing on its own. It only says *which* pending
        grant this already-authenticated human is answering for.
        """
        stamp = now if now is not None else int(time.time())

        grant = await self._repository.get_by_user_code(user_code)
        outcome = self._classify_for_answer(grant, stamp)
        if outcome is not None:
            return outcome
        assert grant is not None  # narrowed by _classify_for_answer

        approved = await self._repository.approve(
            grant.device_code_hash,
            session_id=session_id,
            user_id=user_id,
            now=stamp,
        )
        if not approved:
            # Lost a race with another approval or a denial between our read
            # and our write.
            return ApprovalOutcome.ALREADY_RESOLVED

        logger.info(
            "Device grant approved for user %s (grant %s...)",
            user_id,
            grant.device_code_hash[:8],
        )
        return ApprovalOutcome.APPROVED

    async def deny(self, *, user_code: str, now: Optional[int] = None) -> ApprovalOutcome:
        """Record an explicit refusal so the CLI can say "you declined"."""
        stamp = now if now is not None else int(time.time())

        grant = await self._repository.get_by_user_code(user_code)
        outcome = self._classify_for_answer(grant, stamp)
        if outcome is not None:
            return outcome
        assert grant is not None

        if not await self._repository.deny(grant.device_code_hash, now=stamp):
            return ApprovalOutcome.ALREADY_RESOLVED
        return ApprovalOutcome.DENIED

    @staticmethod
    def _classify_for_answer(grant: Optional[DeviceGrant], now: int) -> Optional[ApprovalOutcome]:
        """Shared precondition check for approve/deny.

        Returns the outcome to report, or None when the grant is answerable.
        """
        if grant is None:
            return ApprovalOutcome.NOT_FOUND
        if grant.is_expired(now):
            return ApprovalOutcome.EXPIRED
        if grant.status is not GrantStatus.PENDING:
            return ApprovalOutcome.ALREADY_RESOLVED
        return None

    async def lookup_pending(self, *, user_code: str, now: Optional[int] = None) -> Optional[DeviceGrant]:
        """Fetch a grant that is still answerable, for the confirmation page.

        Lets the verify route render "a terminal is requesting access" before
        the user commits, without exposing anything: the grant carries no
        credential, and the caller is already authenticated.
        """
        stamp = now if now is not None else int(time.time())
        grant = await self._repository.get_by_user_code(user_code)
        if grant is None or not grant.is_approvable(stamp):
            return None
        return grant

    # ------------------------------------------------------------------
    # CLI: poll
    # ------------------------------------------------------------------

    async def poll(self, *, device_code: str, now: Optional[int] = None) -> Union[DeviceTokenResponse, DevicePendingResponse]:
        """Answer one poll. See the module docstring for why the order matters.

        Raises whatever the codec raises when Secrets Manager is unreachable
        (``CookieDataKeyUnavailable``) so the route can return 5xx. That path
        deliberately happens *before* the claim, so the grant survives for the
        client's next poll.
        """
        stamp = now if now is not None else int(time.time())
        device_code_hash = hash_device_code(device_code)

        grant = await self._repository.get_by_device_code_hash(device_code_hash)
        if grant is None:
            return _pending(_ERR_INVALID)

        # (1) Throttle on the *previous* poll's stamp, then record this one.
        # Recording even on the slow_down path is what makes ignoring
        # `interval` unprofitable rather than merely rude.
        too_fast = grant.should_slow_down(stamp)
        await self._repository.record_poll(device_code_hash, now=stamp)
        if too_fast:
            return _pending(_ERR_SLOW_DOWN)

        if grant.is_expired(stamp):
            return _pending(_ERR_EXPIRED)
        if grant.status is GrantStatus.DENIED:
            return _pending(_ERR_DENIED)
        if grant.status is GrantStatus.CLAIMED:
            # Terminal: the value was already handed over exactly once.
            return _pending(_ERR_INVALID)
        if grant.status is GrantStatus.PENDING:
            return _pending(_ERR_PENDING)

        # APPROVED.
        if grant.session_id is None:
            # Not reachable through `approve`, which writes both together.
            logger.error(
                "Device grant %s... is approved with no session id",
                device_code_hash[:8],
            )
            return _pending(_ERR_INVALID)

        # (2) Everything that can fail non-destructively, before the claim.
        record = await self._session_repository.get(grant.session_id)
        if record is None:
            # The session was revoked or aged out between approval and poll.
            return _pending(_ERR_EXPIRED)
        sealed = self._codec.seal(CookiePayload(session_id=grant.session_id))

        # (3) The single-use gate. `sealed` above is valid but must not be
        # returned unless *we* are the caller that won the claim.
        claimed_session_id = await self._repository.claim(device_code_hash, now=stamp)
        if claimed_session_id is None:
            return _pending(_ERR_INVALID)

        logger.info(
            "Device grant claimed by user %s (grant %s...)",
            record.user_id,
            device_code_hash[:8],
        )
        return DeviceTokenResponse(
            session=sealed,
            expires_in=max(0, record.ttl - stamp),
            user_id=record.user_id,
            username=record.username,
        )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_service: Optional[DeviceGrantService] = None


def get_device_grant_service() -> DeviceGrantService:
    global _service
    if _service is None:
        _service = DeviceGrantService()
    return _service


def _reset_service_for_tests() -> None:
    global _service
    _service = None
