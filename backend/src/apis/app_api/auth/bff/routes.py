"""BFF Token Handler auth routes (Phase 3).

`GET  /auth/login`     — initiates the OAuth code flow against the Cognito
                         Hosted UI.
`GET  /auth/callback`  — completes the flow, persists a session row,
                         writes sealed cookies, redirects to the SPA.
`GET  /auth/session`   — returns the current user + CSRF token. Read by
                         the SPA on bootstrap to confirm "am I logged in?"
`POST /auth/logout`    — drops the session row, clears both cookies.

These routes are dormant until Phase 5 wires the SPA — a /auth/login hit
today will work end-to-end as long as Phase 1 env vars are set, but no
production code path consumes the cookies yet.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
import urllib.parse
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from starlette.responses import RedirectResponse

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.auth.state_store import OIDCStateData, create_state_store
from apis.shared.sessions_bff.cache import get_default_cache
from apis.shared.sessions_bff.config import SESSION_COOKIE_NAME
from apis.shared.sessions_bff.cookie import CookieCodec, CookieDecodeError
from apis.shared.sessions_bff.csrf import CSRFHelper
from apis.shared.sessions_bff.models import CookiePayload, SessionRecord
from apis.shared.sessions_bff.refresh import resolve_bff_client_secret
from apis.shared.sessions_bff.repository import SessionRepository

from .config import BFFAuthConfig
from .cookies import clear_session_cookies, set_session_cookies
from .token_exchange import (
    TokenExchangeError,
    decode_id_token_claims,
    exchange_code_for_tokens,
)

logger = logging.getLogger(__name__)


def _sanitize_for_log(value: Optional[str]) -> str:
    """Normalize untrusted input before logging to prevent log forging."""
    if value is None:
        return ""
    return value.replace("\r", "").replace("\n", "")

router = APIRouter(prefix="/auth", tags=["auth-bff"])

# State token TTL. The user has to bounce off Cognito and back, so 10 minutes
# matches the OIDC state-store default and gives slow IdPs ample headroom.
_STATE_TTL_SECONDS = 600
_AUTHORIZE_SCOPES = "openid email profile"


# ─── Lazy-initialized collaborators ────────────────────────────────────
# Built on first request rather than at import time so the module is
# importable in environments where AWS isn't available (tests, partial
# local dev). All getters return None when the BFF is not configured —
# routes then surface a 503 rather than crashing on a NoneType.

_state_store = None
_repository: Optional[SessionRepository] = None
_cookie_codec: Optional[CookieCodec] = None


def _get_state_store():
    global _state_store
    if _state_store is None:
        _state_store = create_state_store()
    return _state_store


def _get_repository(config: BFFAuthConfig) -> SessionRepository:
    global _repository
    if _repository is None:
        _repository = SessionRepository(
            table_name=config.bff_config.sessions_table_name
        )
    return _repository


def _get_cookie_codec(config: BFFAuthConfig) -> CookieCodec:
    global _cookie_codec
    if _cookie_codec is None:
        _cookie_codec = CookieCodec(
            kms_key_arn=config.bff_config.cookie_signing_key_arn
        )
    return _cookie_codec


def _reset_for_tests() -> None:
    """Drop the lazy singletons — only used by the test suite."""
    global _state_store, _repository, _cookie_codec
    _state_store = None
    _repository = None
    _cookie_codec = None


def _require_ready(config: BFFAuthConfig) -> None:
    if not config.is_ready():
        # 503: the route is registered but the environment hasn't been
        # provisioned (Phase 1 CDK not deployed in this environment, env
        # vars missing). Surfacing this is friendlier than a 500.
        logger.error("BFF auth routes hit before configuration is complete")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="BFF auth flow is not configured.",
        )


# ─── /auth/login ───────────────────────────────────────────────────────


# Cognito's `identity_provider` query parameter accepts the IdP name and
# tells Cognito to skip the Hosted UI provider chooser and forward the
# user straight to that IdP. The SPA's federated-login buttons rely on
# this for one-click SSO; we forward an allowlisted set of characters
# only so a malicious referrer can't smuggle CRLF or other authorize-URL
# tampering payloads through the BFF.
_PROVIDER_ID_ALLOWED = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _sanitized_provider_id(raw: Optional[str]) -> Optional[str]:
    """Return `raw` if it matches the IdP-name allowlist, else None.

    Reject silently rather than 4xx so an old SPA bundle that sends a
    legacy provider ID doesn't break the login flow — Cognito will just
    show its provider chooser instead.
    """
    if raw is None:
        return None
    return raw if _PROVIDER_ID_ALLOWED.match(raw) else None


@router.get("/login", summary="Begin the BFF OAuth code flow")
async def bff_login(provider: Optional[str] = None) -> RedirectResponse:
    """302 to the Cognito Hosted UI authorize endpoint with a fresh state.

    Uses the existing `state_store` (in-memory locally, DynamoDB in cloud)
    to bind one-time-use state tokens to a TTL — the callback validates and
    deletes the state in one atomic step.

    When `provider` is supplied (e.g. by the SPA's federated-IdP button),
    it's forwarded to Cognito as `identity_provider` so the user skips
    the Hosted UI chooser and lands on the right IdP directly. Values
    are filtered through `_PROVIDER_ID_ALLOWED` to defeat header/URL
    injection via the query string.
    """
    config = BFFAuthConfig.from_env()
    _require_ready(config)

    state = secrets.token_urlsafe(32)
    _get_state_store().store_state(
        state,
        OIDCStateData(
            redirect_uri=config.callback_url,
            provider_id="cognito-bff",
        ),
        ttl_seconds=_STATE_TTL_SECONDS,
    )

    authorize_params = {
        "response_type": "code",
        "client_id": config.bff_config.cognito_bff_app_client_id,
        "scope": _AUTHORIZE_SCOPES,
        "redirect_uri": config.callback_url,
        "state": state,
    }
    sanitized_provider = _sanitized_provider_id(provider)
    if sanitized_provider:
        authorize_params["identity_provider"] = sanitized_provider

    params = urllib.parse.urlencode(authorize_params)
    authorize_url = f"{config.cognito_domain_url}/oauth2/authorize?{params}"
    return RedirectResponse(url=authorize_url, status_code=status.HTTP_302_FOUND)


# ─── /auth/callback ────────────────────────────────────────────────────


@router.get("/callback", summary="Complete the BFF OAuth code flow")
async def bff_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
) -> Response:
    """Validate state, exchange code, persist session, set cookies, redirect.

    Failure modes all converge on "clear cookies + redirect to a generic
    auth-failed URL". We deliberately don't echo Cognito's error_description
    into the response — that string is attacker-controlled in some flows.
    """
    config = BFFAuthConfig.from_env()
    _require_ready(config)

    if error:
        logger.info("Cognito returned OAuth error: %s", _sanitize_for_log(error))
        return _redirect_with_cookies_cleared(config, reason="oauth_error")

    if not code or not state:
        return _redirect_with_cookies_cleared(config, reason="missing_params")

    state_ok, _state_data = _get_state_store().get_and_delete_state(state)
    if not state_ok:
        # Either the state was forged, replayed, or expired. All three are
        # terminal — the user re-initiates from /auth/login.
        return _redirect_with_cookies_cleared(config, reason="bad_state")

    try:
        client_secret = resolve_bff_client_secret(
            secret_arn=config.bff_config.cognito_bff_app_client_secret_arn,
        )
    except Exception as exc:
        logger.error("BFF client secret resolution failed: %s", exc)
        return _redirect_with_cookies_cleared(config, reason="server_misconfig")

    try:
        tokens = await exchange_code_for_tokens(
            cognito_domain_url=config.cognito_domain_url,
            client_id=config.bff_config.cognito_bff_app_client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=config.callback_url,
        )
    except TokenExchangeError:
        return _redirect_with_cookies_cleared(config, reason="exchange_failed")

    if not tokens.id_token:
        return _redirect_with_cookies_cleared(config, reason="no_id_token")

    try:
        claims = decode_id_token_claims(tokens.id_token)
    except TokenExchangeError:
        return _redirect_with_cookies_cleared(config, reason="bad_id_token")

    now = int(time.time())
    session_id = secrets.token_urlsafe(24)
    csrf_secret = CSRFHelper.generate_secret()
    record = SessionRecord(
        session_id=session_id,
        user_id=claims.sub,
        username=claims.username,
        cognito_access_token=tokens.access_token,
        cognito_refresh_token=tokens.refresh_token,
        id_token=tokens.id_token,
        access_token_exp=tokens.access_token_exp,
        csrf_secret=csrf_secret,
        created_at=now,
        last_seen_at=now,
        ttl=now + config.bff_config.session_ttl_seconds,
    )

    repository = _get_repository(config)
    try:
        await repository.put(record)
    except Exception as exc:
        logger.error("Failed to persist BFF session row: %s", exc)
        return _redirect_with_cookies_cleared(config, reason="persist_failed")

    codec = _get_cookie_codec(config)
    sealed = codec.seal(CookiePayload(session_id=session_id))
    csrf_token = CSRFHelper.derive_token(csrf_secret, session_id)

    response = RedirectResponse(
        url=config.post_login_redirect_url,
        status_code=status.HTTP_302_FOUND,
    )
    set_session_cookies(
        response,
        sealed_session_value=sealed,
        csrf_token=csrf_token,
        max_age_seconds=config.bff_config.session_ttl_seconds,
    )
    return response


def _redirect_with_cookies_cleared(
    config: BFFAuthConfig, *, reason: str
) -> RedirectResponse:
    """Clear any stale BFF cookies and bounce to the post-login URL with a
    `?auth_error=<reason>` query string the SPA can surface."""
    target = _append_query(config.post_login_redirect_url, {"auth_error": reason})
    response = RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)
    clear_session_cookies(response)
    return response


def _append_query(url: str, extra: dict[str, str]) -> str:
    parsed = urllib.parse.urlparse(url)
    existing = dict(urllib.parse.parse_qsl(parsed.query))
    existing.update(extra)
    return urllib.parse.urlunparse(
        parsed._replace(query=urllib.parse.urlencode(existing))
    )


# ─── /auth/session ─────────────────────────────────────────────────────


@router.get("/session", summary="Return the current BFF-session user")
async def bff_session(
    request: Request,
    user: User = Depends(get_current_user_from_session),
) -> dict:
    """Minimal user payload + CSRF token for the SPA to mirror.

    The dependency raises 401 when the session cookie is missing, the
    DDB row is gone, or the access token can't be revalidated. We pull
    the CSRF token off `request.state` (set by `SessionRefreshMiddleware`)
    rather than re-deriving so we can't drift from what the middleware
    handed downstream code.
    """
    csrf_token = getattr(request.state, "bff_csrf_token", None)
    if csrf_token is None:
        # Defense in depth: re-derive if for any reason the middleware
        # did not stash a token (shouldn't happen — the dep depends on
        # request.state.bff_session being populated, which the middleware
        # only does alongside csrf_token).
        record = getattr(request.state, "bff_session", None)
        if record is not None:
            csrf_token = CSRFHelper.derive_token(
                record.csrf_secret, record.session_id
            )

    return {
        "user_id": user.user_id,
        "email": user.email,
        "name": user.name,
        "roles": list(user.roles or []),
        "picture": user.picture,
        "csrf_token": csrf_token,
    }


# ─── /auth/logout ──────────────────────────────────────────────────────


@router.post("/logout", summary="Drop the BFF session and clear cookies")
async def bff_logout(request: Request) -> Response:
    """Best-effort logout.

    Reads the session cookie directly rather than going through the dep so
    we can clear cookies for already-stale sessions too — the user clicked
    "log out", they get logged out, full stop. Returns 204 plus cleared
    cookies; the SPA owns the post-logout UX.
    """
    config = BFFAuthConfig.from_env()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookies(response)

    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie_value:
        return response

    if not config.bff_config.is_enabled():
        # No backplane — nothing to delete from. Still clear the cookies on
        # the way out so a stale browser doesn't keep echoing them.
        return response

    codec = _get_cookie_codec(config)
    try:
        payload = codec.unseal(cookie_value)
    except CookieDecodeError:
        # Cookie is unrecoverable; the session row (if any) is unreachable
        # via this cookie, so drop the cookies and call it done.
        return response

    session_id = payload.session_id
    repository = _get_repository(config)
    try:
        await repository.delete(session_id)
    except Exception as exc:
        # Logout is idempotent — log the failure but still tell the browser
        # the cookies are gone.
        logger.warning(
            "Failed to delete BFF session row %s on logout: %s", session_id, exc
        )

    # Local-process cache invalidation. Other tasks lag by ≤ refresh leeway
    # seconds (documented in apis/shared/sessions_bff/cache.py).
    try:
        get_default_cache().invalidate(session_id)
    except Exception:
        # Cache miss-or-error must never block logout success.
        logger.debug("Local cache invalidation skipped for %s", session_id)

    return response


