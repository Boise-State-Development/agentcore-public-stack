"""CLI device-authorization routes.

Four endpoints, two audiences:

``POST /auth/cli/authorize``
    The CLI, holding no credential yet, asks for a grant. **Unauthenticated
    by necessity** — this is the entry point of the login flow. Rate-limited
    per client IP so it cannot be used to bulk-mint grants.

``GET /auth/cli/verify``
    The human's browser. Validates the typed or prefilled user code and shows
    it back on a confirmation page with Continue and Cancel. Nothing else
    happens on this request: no OIDC state, no redirect, no approval.

``POST /auth/cli/verify``
    The confirmation page's answer. Continue hands off to the *existing* BFF
    login by stashing the code in the OIDC state, and the device branch in
    ``GET /auth/callback`` finishes the job. Cancel records a denial. The
    page and its answer share one rate limit per client IP, because this is
    the only place a low-entropy user code is submitted and therefore the
    only one that could be guessed at.

``POST /auth/cli/token``
    The CLI polls here. Returns 200 with a sealed session on success, or 400
    with an RFC 8628 error code. Rate-limited per device code — not per IP —
    so one abusive client cannot exhaust the budget for everyone sharing an
    egress address.

Why the verify leg re-runs the full Cognito login rather than reusing a
browser session that may already exist: the CLI gets its **own** session by
design. Two session rows sharing one refresh token would rotate each other
out from under themselves on the next Cognito refresh, which is the hazard
the middleware's per-session lock exists to prevent. If the user already has
a Hosted UI session, Cognito does not re-prompt, so the cost is a redirect
they never see.

This module imports from ``routes`` (state store, OAuth constants) and never
the other way around; the shared HTML lives in ``pages`` so the callback's
device branch can render it without a cycle.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import urllib.parse
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from starlette.responses import RedirectResponse

from apis.shared.auth.device_grants.models import (
    GRANT_TTL_SECONDS,
    USER_CODE_ALPHABET,
    USER_CODE_LENGTH,
    DeviceAuthorizationResponse,
    DeviceTokenRequest,
    DeviceTokenResponse,
    display_user_code,
    hash_device_code,
    normalise_user_code,
)
from apis.shared.auth.device_grants.service import ApprovalOutcome, DeviceGrantService
from apis.shared.auth.state_store import OIDCStateData
from apis.shared.rate_limit import RateLimiter

from .config import BFFAuthConfig
from .cookies import (
    CLI_CONFIRM_COOKIE_NAME,
    clear_cli_confirm_cookie,
    set_cli_confirm_cookie,
    set_oauth_state_cookie,
)
from .pages import device_cancelled_page, device_confirm_page, device_problem_page
from .routes import (
    _AUTHORIZE_SCOPES,
    _BROWSER_BINDING_BYTES,
    _CODE_VERIFIER_BYTES,
    _NONCE_BYTES,
    _STATE_TTL_SECONDS,
    _browser_binding_digest,
    _get_device_grant_service,
    _get_state_store,
    _pkce_code_challenge,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/cli", tags=["auth-cli"])

# ─── Rate limits ───────────────────────────────────────────────────────
# Generous for humans, tight for machines.

#: Grant creation. A person runs `login` a handful of times an hour at worst;
#: beyond this is scripted.
_AUTHORIZE_WINDOW_SECONDS = 300
_AUTHORIZE_MAX_REQUESTS = 10

#: User-code submission — the only guessing surface in the design. The
#: alphabet yields 22**8 (~5.4e10) codes, so a hit is hopeless even
#: unthrottled; a tight window stops an attacker amortising attempts.
_VERIFY_WINDOW_SECONDS = 300
_VERIFY_MAX_REQUESTS = 20

#: Polling. The advertised interval is 5s (~12 requests/min), so 40 leaves
#: room for retries and clock skew while capping a hot loop. The service's
#: `slow_down` already handles the well-behaved-but-eager client; this is the
#: backstop for one that ignores it.
_TOKEN_WINDOW_SECONDS = 60
_TOKEN_MAX_REQUESTS = 40


# ─── Lazy collaborators ────────────────────────────────────────────────

_rate_limiter: Optional[RateLimiter] = None


def _get_service() -> DeviceGrantService:
    """The singleton lives in `routes` so the callback's device branch and
    these routes share one instance and one reset hook."""
    return _get_device_grant_service()


def _get_rate_limiter(config: BFFAuthConfig) -> RateLimiter:
    """Rate-limit counters live alongside the grants, in the sessions table.

    Same reasoning as the grants: no new table, no new IAM grant, and the
    `RATE#`/`WIN#` key shape cannot collide with anything already there.
    """
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(table_name=config.bff_config.sessions_table_name)
    return _rate_limiter


def _reset_for_tests() -> None:
    """Drop the lazy singleton — only used by the test suite.

    The device-grant service singleton is reset by `routes._reset_for_tests`.
    """
    global _rate_limiter
    _rate_limiter = None


def _client_key(request: Request) -> str:
    """Best-effort client identity for the IP-keyed limits.

    Behind the ALB the peer address is the load balancer, so prefer the
    left-most `X-Forwarded-For` hop. That header is client-controlled, so it
    only ever *subdivides* a limit — a forged value buys a fresh bucket, which
    is why the guessing defence rests on the user code's entropy and
    single-use nature rather than on this.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    client = request.client
    return client.host if client else "unknown"


async def _enforce(limiter: RateLimiter, *, key: str, window: int, limit: int) -> None:
    """Raise 429 when the caller is over budget.

    `check_rate_limit` is fail-open by design (a DynamoDB blip must not lock
    everyone out of logging in), so this cannot be the only control on any
    path it guards.
    """
    allowed = await limiter.check_rate_limit(key, window_seconds=window, max_requests=limit)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Slow down and try again shortly.",
        )


def _require_service(service: DeviceGrantService) -> None:
    if not service.enabled:
        logger.error("CLI device-auth route hit before configuration is complete")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CLI device authorization is not configured.",
        )


# ─── POST /auth/cli/authorize ──────────────────────────────────────────


@router.post(
    "/authorize",
    response_model=DeviceAuthorizationResponse,
    summary="Begin a CLI device-authorization flow",
)
async def cli_authorize(request: Request) -> DeviceAuthorizationResponse:
    """Mint a pending grant and tell the CLI where to send the human."""
    config = BFFAuthConfig.from_env()
    service = _get_service()
    _require_service(service)

    await _enforce(
        _get_rate_limiter(config),
        key=f"cli-authorize:{_client_key(request)}",
        window=_AUTHORIZE_WINDOW_SECONDS,
        limit=_AUTHORIZE_MAX_REQUESTS,
    )

    return await service.authorize()


# ─── GET / POST /auth/cli/verify ───────────────────────────────────────

#: Bytes of entropy behind the confirmation form's cookie secret.
_CONFIRM_SECRET_BYTES = 32

#: The confirmation page lives no longer than the grant it is answering.
_CONFIRM_TTL_SECONDS = GRANT_TTL_SECONDS

_ACTION_CONTINUE = "continue"
_ACTION_CANCEL = "cancel"


def _valid_user_code_shape(raw: str) -> bool:
    """Cheap shape check before touching the database.

    Rejects wrong-length input and characters outside the alphabet so a
    scanner cannot turn this endpoint into one free DynamoDB read per
    request.
    """
    normalised = normalise_user_code(raw)
    if len(normalised) != USER_CODE_LENGTH:
        return False
    return all(ch in USER_CODE_ALPHABET for ch in normalised)


def _confirm_token(confirm_secret: str, user_code: str) -> str:
    """The form's proof that it came from the page this browser was shown.

    Keyed on the cookie's secret and bound to the normalised user code, so a
    token minted for one code cannot answer another, and a page that cannot
    read the cookie cannot mint one at all.
    """
    return hmac.new(
        confirm_secret.encode("ascii"),
        f"cli-verify:{user_code}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _is_same_origin_post(request: Request) -> bool:
    """Refuse a form post a browser reports as coming from another site.

    `Sec-Fetch-Site` is set by the browser and cannot be forged by page
    script. A browser too old to send it is still held by the cookie, which
    is `SameSite=strict`, and by the token it keys; this is the first check,
    not the only one.
    """
    site = request.headers.get("sec-fetch-site")
    return site is None or site == "same-origin"


def _expired_code_page() -> Response:
    # Unknown, expired, and already-answered are reported identically: this
    # caller has not authenticated, so distinguishing them would hand out a
    # free existence oracle for user codes.
    return device_problem_page(
        "That code has expired",
        "Device codes are valid for a few minutes. Start a new sign-in from your terminal and try again.",
    )


def _stale_form_page() -> Response:
    return device_problem_page(
        "This page has expired",
        "Open the link from your terminal again, check the code, and choose Continue.",
    )


async def _guard_verify(request: Request) -> tuple[BFFAuthConfig, DeviceGrantService]:
    """What both halves of verify check before reading anything.

    One rate-limit bucket for the page and its answer: a sign-in costs two
    requests from it, and the code-guessing budget is no larger than before.
    """
    config = BFFAuthConfig.from_env()
    if not config.is_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CLI device authorization is not configured.",
        )
    service = _get_service()
    _require_service(service)

    await _enforce(
        _get_rate_limiter(config),
        key=f"cli-verify:{_client_key(request)}",
        window=_VERIFY_WINDOW_SECONDS,
        limit=_VERIFY_MAX_REQUESTS,
    )
    return config, service


@router.get("/verify", summary="Show a CLI sign-in for the person to confirm")
async def cli_verify(request: Request, user_code: Optional[str] = None) -> Response:
    """Check the code, then ask the person to confirm it.

    Opening this URL starts nothing. ``verification_uri_complete`` prefills
    the code, so whoever opens the link may not be whoever started the
    sign-in; RFC 8628 §3.3.1 and §5.4 ask for the code to be shown and
    confirmed before the browser authenticates. The identity provider is
    contacted only from the POST below, after an explicit Continue.
    """
    _config, service = await _guard_verify(request)

    if not user_code or not _valid_user_code_shape(user_code):
        return device_problem_page(
            "That code doesn't look right",
            "Check the code shown in your terminal and open the link again.",
        )

    normalised = normalise_user_code(user_code)
    grant = await service.lookup_pending(user_code=normalised)
    if grant is None:
        return _expired_code_page()

    confirm_secret = secrets.token_urlsafe(_CONFIRM_SECRET_BYTES)
    response = device_confirm_page(
        display_code=display_user_code(grant.user_code),
        user_code=normalised,
        confirm_token=_confirm_token(confirm_secret, normalised),
    )
    set_cli_confirm_cookie(
        response,
        confirm_secret=confirm_secret,
        max_age_seconds=_CONFIRM_TTL_SECONDS,
    )
    return response


@router.post("/verify", summary="Continue or cancel a CLI sign-in")
async def cli_verify_answer(request: Request) -> Response:
    """Act on the confirmation page's answer.

    Continue starts the normal BFF login; the grant is approved only in the
    callback, after the identity provider has vouched for the person. The
    user code still authorizes nothing: it selects which pending grant that
    separately authenticated session will answer. Cancel records a refusal,
    which the CLI's next poll reports as ``access_denied``.

    The answer counts only when it carries the token the confirmation page
    was rendered with, keyed on this browser's ``SameSite=strict`` cookie, so
    a form on another site cannot submit it. Every check here is local and
    runs before the one database read.
    """
    config, service = await _guard_verify(request)

    if not _is_same_origin_post(request):
        logger.info("CLI verify answer refused: cross-site post")
        return _stale_form_page()

    form = await request.form()
    user_code = form.get("user_code")
    confirm_token = form.get("confirm_token")
    action = form.get("action")
    confirm_secret = request.cookies.get(CLI_CONFIRM_COOKIE_NAME)

    if not isinstance(user_code, str) or not _valid_user_code_shape(user_code):
        return _stale_form_page()
    normalised = normalise_user_code(user_code)
    if not confirm_secret or not isinstance(confirm_token, str) or not hmac.compare_digest(confirm_token, _confirm_token(confirm_secret, normalised)):
        logger.info("CLI verify answer refused: missing or mismatched confirmation token")
        return _stale_form_page()
    if action not in (_ACTION_CONTINUE, _ACTION_CANCEL):
        return _stale_form_page()

    grant = await service.lookup_pending(user_code=normalised)
    if grant is None:
        response = _expired_code_page()
    elif action == _ACTION_CANCEL:
        outcome = await service.deny(user_code=normalised)
        response = device_cancelled_page() if outcome is ApprovalOutcome.DENIED else _expired_code_page()
    else:
        response = _begin_device_login(config, normalised)

    # One answer per page: a second submit has to come from a fresh page.
    clear_cli_confirm_cookie(response)
    return response


def _begin_device_login(config: BFFAuthConfig, user_code: str) -> Response:
    """Hand off to the identity provider with the user code in the OIDC state.

    Same shape of authorize request as `/auth/login` — PKCE, nonce and the
    browser-binding cookie — because the callback is the web app's and it
    rejects any state that lacks them. The CLI adapts to that contract; it
    must not loosen it. The binding ties the authorize URL to this browser,
    so that URL is useless relayed to anyone else.

    303, not 302: the answer to a POST is fetched with a GET.
    """
    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(_CODE_VERIFIER_BYTES)
    nonce = secrets.token_urlsafe(_NONCE_BYTES)
    binding_secret = secrets.token_urlsafe(_BROWSER_BINDING_BYTES)

    _get_state_store().store_state(
        state,
        OIDCStateData(
            redirect_uri=config.callback_url,
            provider_id="cognito-bff",
            code_verifier=code_verifier,
            nonce=nonce,
            browser_binding_hash=_browser_binding_digest(binding_secret),
            device_user_code=user_code,
        ),
        ttl_seconds=_STATE_TTL_SECONDS,
    )

    params = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": config.bff_config.cognito_bff_app_client_id,
            "scope": _AUTHORIZE_SCOPES,
            "redirect_uri": config.callback_url,
            "state": state,
            "code_challenge": _pkce_code_challenge(code_verifier),
            "code_challenge_method": "S256",
            "nonce": nonce,
        }
    )
    response = RedirectResponse(
        url=f"{config.cognito_domain_url}/oauth2/authorize?{params}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    set_oauth_state_cookie(
        response,
        binding_secret=binding_secret,
        max_age_seconds=_STATE_TTL_SECONDS,
    )
    return response


# ─── POST /auth/cli/token ──────────────────────────────────────────────


@router.post("/token", summary="Poll for the result of a CLI sign-in")
async def cli_token(request: Request, body: DeviceTokenRequest) -> JSONResponse:
    """Exchange a device code for a sealed session, or report why not.

    Follows RFC 8628's response shape: a pending or failed poll is an HTTP 400
    carrying an `error` code rather than a 200 with a status field, so a
    stock OAuth device-flow client can drive this loop unmodified.
    """
    config = BFFAuthConfig.from_env()
    service = _get_service()
    _require_service(service)

    # Keyed on the device code's hash, not the caller's IP: polling is
    # per-flow, and IP-keying would let one aggressive client throttle every
    # other user behind the same egress address.
    await _enforce(
        _get_rate_limiter(config),
        key=f"cli-token:{hash_device_code(body.device_code)}",
        window=_TOKEN_WINDOW_SECONDS,
        limit=_TOKEN_MAX_REQUESTS,
    )

    result = await service.poll(device_code=body.device_code)

    if isinstance(result, DeviceTokenResponse):
        return JSONResponse(status_code=status.HTTP_200_OK, content=result.model_dump())

    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content=result.model_dump())
