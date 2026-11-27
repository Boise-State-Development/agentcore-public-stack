"""Keep a headless grant's refresh token alive exactly as long as the grant needs it.

A headless grant (``apis.shared.harness.grants``) pins the refresh token of the
BFF session it was enabled from. The BFF app client has no refresh-token
rotation, so the grant and that session hold the **same** token string. Before
this module, signing out of that session revoked the token with Cognito and
silently stopped every scheduled run the user had (docs/specs/proactive-trust.md
§5.2, confirmed on dev 2026-11-27).

The rule this module enforces, in one place:

* **Logout** revokes the session's token with Cognito unless the user's active
  grant holds it (``grant_keeps_token``). The session row is still deleted, so
  the browser is signed out; the token stays alive server-side only, in the
  grant, for scheduled runs.
* **A token the grant lets go of is revoked** once no live session holds it
  (``release_refresh_token``): when the grant is revoked on ``/schedules``, and
  when it is re-pinned to a newer session.
* **A web sign-in re-pins an active grant** to the new session
  (``pin_grant(..., only_if_active=True)``), so the grant tracks the newest
  login. The terminal's device flow and the phone's native exit never call it.

So a token outlives its session only while an active grant holds it, and that
grant is bounded by its own 30-day window and revocable on ``/schedules``.
"""

from __future__ import annotations

import hmac
import logging
from typing import Optional

from apis.shared.harness.grants import HeadlessGrant, HeadlessGrantService
from apis.shared.sessions_bff.models import SessionRecord
from apis.shared.sessions_bff.refresh import CognitoRefreshClient, CognitoRefreshError
from apis.shared.sessions_bff.repository import SessionRepository

logger = logging.getLogger(__name__)


def _same_token(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a, b)


async def grant_keeps_token(record: SessionRecord, grants: HeadlessGrantService) -> bool:
    """Whether the user's active grant holds this session's refresh token.

    One Query on ``HeadlessGrantUserIndex``. Fails toward revoking: if the grant
    can't be read, logout revokes as it always has, since a sign-out that leaves
    a token alive must only ever happen on purpose.
    """
    if not record.cognito_refresh_token or not grants.enabled:
        return False
    try:
        grant = await grants.get_active_grant(record.user_id)
    except Exception:
        logger.warning("Logout could not read the headless grant; revoking the session token", exc_info=True)
        return False
    return grant is not None and _same_token(grant.cognito_refresh_token, record.cognito_refresh_token)


async def release_refresh_token(
    refresh_token: Optional[str],
    pinned_session_id: Optional[str],
    *,
    repository: SessionRepository,
    cognito: CognitoRefreshClient,
) -> bool:
    """Revoke a token a grant no longer holds, unless its session still does. Returns whether it revoked.

    ``pinned_session_id`` is the session the grant took the token from. While
    that session's row exists and holds the token, the browser still needs it,
    and its own logout revokes it later. A grant pinned before the session id
    was recorded has none: its token is left alone, because such a token was
    either already revoked by its session's logout or is still that live
    session's. Any failure leaves the token alone and logs it; the token then
    lapses at the end of its own validity, which is the grant's window too.
    """
    if not refresh_token or not pinned_session_id or not repository.enabled:
        # No session table to ask means no way to know the token is unheld.
        return False
    try:
        record = await repository.get(pinned_session_id)
    except Exception:
        logger.warning("Could not read the pinned session; leaving its token alone", exc_info=True)
        return False
    if record is not None and _same_token(record.cognito_refresh_token, refresh_token):
        return False
    if not cognito.enabled:
        return False
    try:
        await cognito.revoke(refresh_token=refresh_token)
    except CognitoRefreshError as exc:
        logger.warning("Could not revoke a released grant token: %s", exc)
        return False
    logger.info("Revoked a refresh token no grant or session holds any more")
    return True


async def pin_grant(
    record: SessionRecord,
    *,
    grants: HeadlessGrantService,
    repository: SessionRepository,
    cognito: CognitoRefreshClient,
    only_if_active: bool = False,
) -> Optional[HeadlessGrant]:
    """Pin the user's grant to ``record``'s session, and release the token it held before.

    ``only_if_active`` is the sign-in path: it re-pins a grant that is active,
    and never creates one or revives one that is revoked or ``needs_reauth``.
    Enabling scheduled runs from ``/schedules`` (or Run now, or an inbox answer)
    passes False and creates or renews the grant as before.
    """
    previous = await grants.get_active_grant(record.user_id)
    if only_if_active and previous is None:
        return None
    if (
        previous is not None
        and _same_token(previous.cognito_refresh_token, record.cognito_refresh_token)
        and previous.pinned_session_id == record.session_id
    ):
        return previous
    grant = await grants.enable(
        user_id=record.user_id,
        username=record.username,
        refresh_token=record.cognito_refresh_token,
        token_issued_at=record.created_at,
        session_id=record.session_id,
    )
    if previous is not None and not _same_token(previous.cognito_refresh_token, record.cognito_refresh_token):
        await release_refresh_token(
            previous.cognito_refresh_token,
            previous.pinned_session_id,
            repository=repository,
            cognito=cognito,
        )
    return grant
