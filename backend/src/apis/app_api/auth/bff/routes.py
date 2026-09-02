"""BFF Token Handler auth routes (Phase 3).

`GET  /auth/login`     — initiates the OAuth code flow against the Cognito
                         Hosted UI. Mints the browser-binding cookie, the
                         PKCE verifier, and the OIDC nonce.
`GET  /auth/callback`  — completes the flow: verifies the state is bound to
                         *this* browser, redeems the code with the PKCE
                         verifier, checks the ID-token nonce, persists a
                         session row, writes sealed cookies, redirects to
                         the SPA.
`GET  /auth/session`   — returns the current user + CSRF token. Read by
                         the SPA on bootstrap to confirm "am I logged in?"
`POST /auth/logout`    — drops the session row, clears both cookies, and
                         returns the Cognito Hosted UI logout URL so the
                         SPA can finish the round-trip.

Security invariant for the login round-trip: `state` is a public value —
it rides in a redirect URL and anyone can mint one anonymously — so it is
never sufficient on its own. A callback is only honoured when it also
presents the `__Host-bff_oauth_state` cookie whose digest was recorded with
that state. Without that pairing, an attacker can complete their own
authorization request in a victim's browser and the victim silently
transacts inside the attacker's account (OAuth login CSRF / session
fixation). See `bff_login` and `_binding_matches`.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
import time
import urllib.parse
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from starlette.responses import RedirectResponse

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.auth.state_store import OIDCStateData, create_state_store
from apis.shared.users.repository import UserRepository
from apis.shared.users.sync import UserSyncService
from apis.shared.sessions_bff.cache import get_default_cache
from apis.shared.sessions_bff.config import SESSION_COOKIE_NAME
from apis.shared.sessions_bff.cookie import (
    CookieCodec,
    CookieDecodeError,
    _reset_default_codec_for_tests,
    get_default_codec,
)
from apis.shared.sessions_bff.csrf import CSRFHelper
from apis.shared.sessions_bff.models import CookiePayload, SessionRecord
from apis.shared.sessions_bff.refresh import resolve_bff_client_secret
from apis.shared.sessions_bff.repository import SessionRepository

from .config import BFFAuthConfig
from .cookies import (
    OAUTH_STATE_COOKIE_NAME,
    clear_oauth_state_cookie,
    clear_session_cookies,
    set_oauth_state_cookie,
    set_session_cookies,
)
from .token_exchange import (
    IdTokenClaims,
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

# Entropy budgets for the three per-login secrets. `token_urlsafe(n)` yields
# ceil(n * 4/3) characters, so 64 bytes lands at 86 chars — comfortably inside
# the 43..128 character window RFC 7636 §4.1 mandates for a PKCE verifier.
_CODE_VERIFIER_BYTES = 64
_NONCE_BYTES = 32
_BROWSER_BINDING_BYTES = 32


def _pkce_code_challenge(code_verifier: str) -> str:
    """S256 challenge for `code_verifier` — base64url(SHA-256(v)), unpadded.

    Padding is stripped because RFC 7636 §4.2 specifies base64url *without*
    padding; Cognito rejects the `=`-suffixed form.
    """
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _browser_binding_digest(binding_secret: str) -> str:
    """SHA-256 hex of the state cookie's value.

    Only the digest goes into the state store, so the stored row is not
    itself a usable credential. Plain SHA-256 (no salt/KDF) is right here:
    the input is 32 bytes of CSPRNG output, so there is no guessable
    preimage for a rainbow table to cover.
    """
    return hashlib.sha256(binding_secret.encode("utf-8")).hexdigest()


# ─── Lazy-initialized collaborators ────────────────────────────────────
# Built on first request rather than at import time so the module is
# importable in environments where AWS isn't available (tests, partial
# local dev). All getters return None when the BFF is not configured —
# routes then surface a 503 rather than crashing on a NoneType.

_state_store = None
_repository: Optional[SessionRepository] = None
_user_sync_service: Optional[UserSyncService] = None


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


def _get_user_sync_service() -> Optional[UserSyncService]:
    """Lazy-init the Users-table sync service.

    Returns None if the table isn't configured (the underlying repository
    flips its `enabled` flag off and refuses to write). Failure here is
    non-fatal — the user still gets a valid session, the Users row just
    isn't populated until something else writes it.
    """
    global _user_sync_service
    if _user_sync_service is None:
        try:
            _user_sync_service = UserSyncService(repository=UserRepository())
        except Exception as exc:
            logger.warning("BFF user-sync service init failed: %s", exc)
            return None
    return _user_sync_service


def _get_cookie_codec(config: BFFAuthConfig) -> CookieCodec:
    # Delegates to the process-wide singleton in `sessions_bff.cookie` so the
    # codec used here to seal a fresh cookie is the same instance the
    # SessionRefreshMiddleware uses to unseal it on the next request.
    # `config` is unused but kept in the signature for parity with the other
    # collaborator getters above.
    del config
    return get_default_codec()


def _reset_for_tests() -> None:
    """Drop the lazy singletons — only used by the test suite."""
    global _state_store, _repository, _user_sync_service
    _state_store = None
    _repository = None
    _user_sync_service = None
    _reset_default_codec_for_tests()


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

# Cap on `return_to` length. Most browsers tolerate longer URLs, but a
# session-cookie-bearing redirect target should not be a giant query
# string — anything past this is almost certainly junk.
_RETURN_TO_MAX_LENGTH = 2048


def _sanitized_provider_id(raw: Optional[str]) -> Optional[str]:
    """Return `raw` if it matches the IdP-name allowlist, else None.

    Reject silently rather than 4xx so an old SPA bundle that sends a
    legacy provider ID doesn't break the login flow — Cognito will just
    show its provider chooser instead.
    """
    if raw is None:
        return None
    return raw if _PROVIDER_ID_ALLOWED.match(raw) else None


def _sanitized_return_to(raw: Optional[str]) -> Optional[str]:
    """Return a same-origin `return_to` path or None.

    Hard rules — anything that fails drops the deep link silently and
    falls back to `BFF_POST_LOGIN_REDIRECT_URL`:

      - Must start with `/`. Bare paths only — no scheme, no host.
      - Must NOT start with `//`. Defeats the protocol-relative URL
        trick `//evil.com/abc` which a browser would resolve against
        the BFF's scheme but to a foreign host.
      - Must NOT start with `/\\`. Some browsers normalise `\\` to `/`
        in URL parsing, which would re-enable the protocol-relative
        bypass past the `//` check.
      - Must contain no C0 control bytes (U+0000 .. U+001F). Per the
        WHATWG URL spec, browsers strip TAB/CR/LF from URL inputs
        before parsing, so a value like ``/\t/evil.com`` would parse
        as the protocol-relative ``//evil.com`` and bypass the `//`
        check above when the configured post-login URL is a relative
        path. Rejecting the whole C0 range is cheap defense in depth
        against future browser-quirk discoveries.
      - Length capped at `_RETURN_TO_MAX_LENGTH`.

    The result is used as a Location header value, never passed to a
    template or HTML context, so the SPA's own routing handles it.
    """
    if raw is None:
        return None
    if len(raw) > _RETURN_TO_MAX_LENGTH:
        return None
    if any(ord(ch) < 0x20 for ch in raw):
        return None
    if not raw.startswith("/"):
        return None
    if raw.startswith("//") or raw.startswith("/\\"):
        return None
    return raw


def _absolutize_return_to(*, return_to: str, base_url: str) -> str:
    """Combine a path-only `return_to` with the SPA origin from `base_url`.

    `return_to` has already been allowlist-validated to a same-origin path
    (no scheme, no host). When `base_url` is absolute (e.g. the typical
    `BFF_POST_LOGIN_REDIRECT_URL=http://localhost:4200/`) we splice its
    scheme + netloc onto the path so the 302 lands on the SPA, not on
    whatever host issued the callback. When `base_url` is itself a relative
    path (e.g. the env-var fallback `/`), we hand back the path unchanged
    and let the browser resolve it relative to the issuing host — that's
    what same-origin production deployments want.
    """
    parsed = urllib.parse.urlsplit(base_url)
    if not parsed.scheme or not parsed.netloc:
        return return_to
    return f"{parsed.scheme}://{parsed.netloc}{return_to}"


@router.get("/login", summary="Begin the BFF OAuth code flow")
async def bff_login(
    provider: Optional[str] = None,
    return_to: Optional[str] = None,
) -> RedirectResponse:
    """302 to the Cognito Hosted UI authorize endpoint with a fresh state.

    Three per-login secrets are minted here, and all three are what make the
    round-trip safe:

    1. **Browser binding.** A random secret goes back to the caller in the
       HttpOnly `__Host-bff_oauth_state` cookie while only its SHA-256 digest
       is committed to the state row. `state` on its own is a *public* value —
       it travels in a redirect URL — so without this cookie any party who can
       read a `state` (including one they minted anonymously themselves) can
       have a *different* browser complete the flow, and that browser is
       issued a session for whoever the exchanged code names. That's OAuth
       login CSRF / session fixation; the cookie is the control that stops it.
    2. **PKCE (S256).** `code_verifier` stays server-side in the state row;
       only its challenge goes on the wire. An authorization code lifted from
       the redirect (proxy log, Referer leak, shoulder-surfed URL) is then
       unredeemable without the verifier.
    3. **Nonce.** Echoed by the IdP into the ID token and checked at the
       callback, so an ID token minted for some other authorize request can't
       be substituted into this one.

    Uses the existing `state_store` (in-memory locally, DynamoDB in cloud)
    to bind one-time-use state tokens to a TTL — the callback validates and
    deletes the state in one atomic step.

    When `provider` is supplied (e.g. by the SPA's federated-IdP button),
    it's forwarded to Cognito as `identity_provider` so the user skips
    the Hosted UI chooser and lands on the right IdP directly. Values
    are filtered through `_PROVIDER_ID_ALLOWED` to defeat header/URL
    injection via the query string.

    When `return_to` is supplied, it's stashed in the OIDC state and the
    callback redirects there on success instead of the configured
    post-login URL — so a deep link the user followed before logging in
    survives the round-trip. Values are filtered through
    `_sanitized_return_to` to keep the redirect same-origin only.

    Note on concurrent logins: the binding cookie holds exactly one in-flight
    authorization request, so starting a second login in the same browser
    supersedes the first. Whichever flow the user finishes last is the one
    that completes; the abandoned one fails closed with `state_not_bound` and
    the user simply re-runs login. Tracking a set of live bindings per browser
    would avoid that, at the cost of turning a single cookie compare into
    list-management for a case a user hits by accident, if ever.
    """
    config = BFFAuthConfig.from_env()
    _require_ready(config)

    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(_CODE_VERIFIER_BYTES)
    nonce = secrets.token_urlsafe(_NONCE_BYTES)
    binding_secret = secrets.token_urlsafe(_BROWSER_BINDING_BYTES)

    _get_state_store().store_state(
        state,
        OIDCStateData(
            redirect_uri=config.callback_url,
            provider_id="cognito-bff",
            return_to=_sanitized_return_to(return_to),
            code_verifier=code_verifier,
            nonce=nonce,
            browser_binding_hash=_browser_binding_digest(binding_secret),
        ),
        ttl_seconds=_STATE_TTL_SECONDS,
    )

    authorize_params = {
        "response_type": "code",
        "client_id": config.bff_config.cognito_bff_app_client_id,
        "scope": _AUTHORIZE_SCOPES,
        "redirect_uri": config.callback_url,
        "state": state,
        "code_challenge": _pkce_code_challenge(code_verifier),
        "code_challenge_method": "S256",
        "nonce": nonce,
    }
    sanitized_provider = _sanitized_provider_id(provider)
    if sanitized_provider:
        authorize_params["identity_provider"] = sanitized_provider

    params = urllib.parse.urlencode(authorize_params)
    authorize_url = f"{config.cognito_domain_url}/oauth2/authorize?{params}"
    response = RedirectResponse(
        url=authorize_url, status_code=status.HTTP_302_FOUND
    )
    # Cookie lifetime matches the state TTL: once the state row is gone the
    # binding is useless, so there's no reason for it to outlive the flow.
    set_oauth_state_cookie(
        response,
        binding_secret=binding_secret,
        max_age_seconds=_STATE_TTL_SECONDS,
    )
    return response


# ─── /auth/callback ────────────────────────────────────────────────────


def _binding_matches(
    state_data: Optional[OIDCStateData], binding_secret: str
) -> bool:
    """True iff the callback's cookie is the one minted with this state.

    Fails closed on a state row that carries no binding digest. Such a row can
    only come from a revision of this service that predates browser binding,
    so during a rolling deploy a login started on an old task and finished on
    a new one is rejected and the user re-runs login. That is the correct
    trade: accepting unbound rows would keep the login-CSRF hole open for the
    whole rollout window, which is exactly what an attacker holding a
    pre-minted state would wait for.
    """
    if state_data is None or not state_data.browser_binding_hash:
        return False
    return secrets.compare_digest(
        _browser_binding_digest(binding_secret),
        state_data.browser_binding_hash,
    )


def _nonce_matches(
    state_data: Optional[OIDCStateData], claims: IdTokenClaims
) -> bool:
    """True iff the ID token echoes the nonce issued with this request.

    A state row with no stored nonce passes: it predates this check, and
    `_binding_matches` has already rejected those rows on the binding digest,
    so this is not a bypass — it just keeps the failure attributed to the
    binding.
    """
    if state_data is None or not state_data.nonce:
        return True
    if not claims.nonce:
        return False
    return secrets.compare_digest(claims.nonce, state_data.nonce)



@router.get("/callback", summary="Complete the BFF OAuth code flow")
async def bff_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
) -> Response:
    """Validate state, exchange code, persist session, set cookies, redirect.

    Order of checks matters. The browser-binding cookie is required *before*
    the code is exchanged, so a request that isn't the continuation of a login
    this browser started never reaches Cognito's token endpoint and never
    mints a session row.

    Failure modes all converge on "clear cookies + redirect to a generic
    auth-failed URL". We deliberately don't echo Cognito's error_description
    into the response — that string is attacker-controlled in some flows.

    On why there's no `Origin`/`Sec-Fetch-Site` check here: the legitimate
    arrival at this endpoint *is* a top-level cross-site GET (the IdP redirects
    the browser back), so it carries `Sec-Fetch-Site: cross-site` and no
    `Origin`. Rejecting on those headers would reject every real login while
    an attacker-crafted link looks identical. Browser binding is the control
    that actually distinguishes the two.
    """
    config = BFFAuthConfig.from_env()
    _require_ready(config)

    if error:
        logger.info("Cognito returned OAuth error: %s", _sanitize_for_log(error))
        return _redirect_with_cookies_cleared(config, reason="oauth_error")

    if not code or not state:
        return _redirect_with_cookies_cleared(config, reason="missing_params")

    # Read the binding before touching the state store: no cookie means this
    # browser never started a login, so there's nothing to consume.
    binding_secret = request.cookies.get(OAUTH_STATE_COOKIE_NAME)
    if not binding_secret:
        logger.warning(
            "BFF callback rejected: no %s cookie on the request. Either the "
            "browser dropped it (check that the response to /auth/login is "
            "served over HTTPS so the __Host- prefix is accepted) or this "
            "callback was not initiated by this browser.",
            OAUTH_STATE_COOKIE_NAME,
        )
        return _redirect_with_cookies_cleared(config, reason="missing_state_cookie")

    state_ok, state_data = _get_state_store().get_and_delete_state(state)
    if not state_ok:
        # Either the state was forged, replayed, or expired. All three are
        # terminal — the user re-initiates from /auth/login.
        return _redirect_with_cookies_cleared(config, reason="bad_state")

    if not _binding_matches(state_data, binding_secret):
        # The state is real but was minted for a different browser. This is
        # the login-CSRF / session-fixation attempt: someone is trying to get
        # *this* browser to finish *their* authorization request. Log loudly —
        # unlike the checks above, there is no benign path to here.
        logger.warning(
            "BFF callback rejected: OAuth state is not bound to this browser "
            "(possible login-CSRF attempt)."
        )
        return _redirect_with_cookies_cleared(config, reason="state_not_bound")

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
            code_verifier=state_data.code_verifier if state_data else None,
        )
    except TokenExchangeError:
        return _redirect_with_cookies_cleared(config, reason="exchange_failed")

    if not tokens.id_token:
        return _redirect_with_cookies_cleared(config, reason="no_id_token")

    try:
        claims = decode_id_token_claims(tokens.id_token)
    except TokenExchangeError:
        return _redirect_with_cookies_cleared(config, reason="bad_id_token")

    if not _nonce_matches(state_data, claims):
        logger.warning(
            "BFF callback rejected: ID token nonce does not match the nonce "
            "issued with this authorization request."
        )
        return _redirect_with_cookies_cleared(config, reason="bad_nonce")

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

    # Upsert the Users table from the ID-token claims. The per-request sync
    # in `get_current_user_from_session` runs off the access token, which
    # carries no email/name/picture and only a Cognito-internal group name
    # in `cognito:groups` — so without this seed the Users row would be
    # created with email=None and the wrong roles, breaking sharing,
    # fine-tuning, and RBAC for first-time users. Failure is non-fatal:
    # the user still has a valid session, sync just retries on the next
    # login.
    await _sync_user_from_id_token(claims)

    codec = _get_cookie_codec(config)
    sealed = codec.seal(CookiePayload(session_id=session_id))
    csrf_token = CSRFHelper.derive_token(csrf_secret, session_id)

    # Honour the `return_to` deep link the SPA stashed at /auth/login.
    # The path was already allowlist-validated (same-origin only) before
    # being committed to the OIDC state. We graft it onto the configured
    # post-login origin so the browser lands on the SPA host, not whatever
    # host issued the callback — in dev the BFF (:8000) and SPA (:4200) are
    # cross-origin, and a path-only Location would resolve against the BFF.
    if state_data is not None and state_data.return_to:
        redirect_target = _absolutize_return_to(
            return_to=state_data.return_to,
            base_url=config.post_login_redirect_url,
        )
    else:
        redirect_target = config.post_login_redirect_url

    response = RedirectResponse(
        url=redirect_target,
        status_code=status.HTTP_302_FOUND,
    )
    set_session_cookies(
        response,
        sealed_session_value=sealed,
        csrf_token=csrf_token,
        max_age_seconds=config.bff_config.session_ttl_seconds,
    )
    # The binding cookie has done its job — the state row it guarded is
    # consumed. Drop it so it can't be paired with a later state.
    clear_oauth_state_cookie(response)
    return response


async def _sync_user_from_id_token(claims: IdTokenClaims) -> None:
    """Best-effort upsert of the Users row from fresh ID-token claims.

    Called inline (not fire-and-forget) so the row exists by the time the
    SPA's first authenticated request hits the API. The sync service
    itself is fast (single DDB upsert) and bounded — happy to wait for it
    rather than introduce a "row visible eventually" race for the first
    page load. Any failure (table unconfigured, DDB down, missing email
    claim) is logged and swallowed: a partially-synced user is better
    than a failed login.
    """
    sync_service = _get_user_sync_service()
    if not sync_service or not sync_service.enabled:
        return
    try:
        await sync_service.sync_from_user(
            user_id=claims.sub,
            email=claims.email or "",
            name=claims.name or "",
            roles=claims.roles,
            picture=claims.picture,
        )
    except Exception as exc:
        logger.warning("BFF callback user-sync skipped: %s", exc)


def _redirect_with_cookies_cleared(
    config: BFFAuthConfig, *, reason: str
) -> RedirectResponse:
    """Clear any stale BFF cookies and bounce to the post-login URL with a
    `?auth_error=<reason>` query string the SPA can surface.

    Also drops the OAuth binding cookie: every path through here ends the
    in-flight authorization request, so keeping the binding around would only
    let it be replayed against a subsequent state.
    """
    target = _append_query(config.post_login_redirect_url, {"auth_error": reason})
    response = RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)
    clear_session_cookies(response)
    clear_oauth_state_cookie(response)
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


def _cognito_logout_url(config: BFFAuthConfig) -> Optional[str]:
    """Build the Cognito Hosted UI logout URL, or None if not configured.

    Cognito's `/logout` endpoint clears the Hosted UI session cookie that
    sits independent of our BFF cookies; without this hop the user "logs
    out" of our session but Cognito silently re-issues a code on the next
    /authorize, so they're back in without a credential prompt.

    The `logout_uri` must exactly match a value registered on the BFF app
    client's `logoutUrls` (CDK strips the trailing slash there, so we
    strip it here too).
    """
    if not (
        config.cognito_domain_url
        and config.bff_config.cognito_bff_app_client_id
        and config.post_login_redirect_url
    ):
        return None
    params = urllib.parse.urlencode(
        {
            "client_id": config.bff_config.cognito_bff_app_client_id,
            "logout_uri": config.post_login_redirect_url.rstrip("/"),
        }
    )
    return f"{config.cognito_domain_url}/logout?{params}"


@router.post("/logout", summary="Drop the BFF session and clear cookies")
async def bff_logout(request: Request) -> Response:
    """Best-effort logout.

    Reads the session cookie directly rather than going through the dep so
    we can clear cookies for already-stale sessions too — the user clicked
    "log out", they get logged out, full stop. Returns the Cognito Hosted
    UI logout URL so the SPA can finish the round-trip and clear the
    upstream session cookie too; otherwise Cognito's own session keeps
    silently re-authenticating the user on the next /authorize.
    """
    config = BFFAuthConfig.from_env()
    response = JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"post_logout_url": _cognito_logout_url(config)},
    )
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


