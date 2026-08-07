"""The login flow, start to finish.

    build authorize URL -> open browser -> receive redirect on loopback
      -> verify state -> exchange code + verifier for tokens
      -> persist refresh token

Kept free of printing and prompting so it can be driven from either the CLI or
the TUI, and tested without a browser.
"""

from __future__ import annotations

import logging
import webbrowser
from dataclasses import dataclass

import httpx

from ..config import DEFAULT_CALLBACK_PORTS as CONFIG_CALLBACK_PORTS
from ..errors import AgentCoreTuiError
from .loopback import LoopbackReceiver
from .oidc import AuthorizationError, CognitoOidcClient, OidcConfig, build_authorize_url
from .pkce import PkceChallenge
from .tokens import TokenSet, load_refresh_token, save_refresh_token

logger = logging.getLogger(__name__)

#: Re-exported from :mod:`agentcore_tui.config`, which is the single definition.
#: These must agree with `cognito.cliClient.callbackPorts` in the CDK config —
#: Cognito matches redirect URIs byte-for-byte and does not honour RFC 8252's
#: variable-port rule, so an unregistered port simply cannot be used.
DEFAULT_CALLBACK_PORTS = CONFIG_CALLBACK_PORTS


class StateMismatchError(AgentCoreTuiError):
    """The redirect's state did not match the one we generated."""

    hint = "This can indicate a forged or stale redirect. Run `agentcore-tui login` again."


@dataclass(frozen=True, slots=True)
class LoginOutcome:
    """Result of a successful login."""

    tokens: TokenSet
    refresh_token_stored: bool
    keyring_error: str | None = None


async def perform_login(
    *,
    base_url: str,
    domain_url: str,
    client_id: str,
    ports: tuple[int, ...] = DEFAULT_CALLBACK_PORTS,
    identity_provider: str | None = None,
    open_browser: bool = True,
    timeout: float = 300.0,
    client: httpx.AsyncClient | None = None,
) -> tuple[LoginOutcome, str]:
    """Run the interactive login. Returns the outcome and the URL that was used.

    The URL is returned so a caller can print it — the browser may fail to
    launch (headless host, WSL, SSH), and pasting it manually still works.
    """
    challenge = PkceChallenge.create()

    with LoopbackReceiver(list(ports)) as receiver:
        config = OidcConfig(
            domain_url=domain_url,
            client_id=client_id,
            redirect_uri=receiver.redirect_uri,
        )
        authorize_url = build_authorize_url(config, challenge, identity_provider=identity_provider)
        logger.info("authorize url built for client %s via %s", client_id, receiver.redirect_uri)

        if open_browser:
            try:
                opened = webbrowser.open(authorize_url)
            except Exception:  # pragma: no cover - platform dependent
                opened = False
            if not opened:
                logger.warning("could not launch a browser; the URL must be opened manually")

        result = receiver.wait(timeout=timeout)

    if result.error:
        detail = result.error_description or result.error
        logger.error("authorization denied: %s", detail)
        raise AuthorizationError(f"Sign-in was refused ({detail})")

    if not result.code:
        raise AuthorizationError("The redirect contained no authorization code")

    # Constant-time comparison is unnecessary here (the state is not a
    # credential), but the check itself is essential: it is what makes a forged
    # redirect to our loopback port unusable.
    if result.state != challenge.state:
        logger.error("state mismatch on redirect")
        raise StateMismatchError("The sign-in response did not match this request")

    async with CognitoOidcClient(config, client=client) as oidc:
        tokens = await oidc.exchange_code(result.code, challenge)

    stored = False
    keyring_error: str | None = None
    if tokens.refresh_token:
        try:
            save_refresh_token(base_url, tokens.refresh_token)
            stored = True
        except AgentCoreTuiError as exc:
            # A session that works until the process exits is still useful.
            keyring_error = exc.message
            logger.warning("refresh token not persisted: %s", exc.message)

    return LoginOutcome(tokens=tokens, refresh_token_stored=stored, keyring_error=keyring_error), authorize_url


async def resume_session(
    *,
    base_url: str,
    domain_url: str,
    client_id: str,
    client: httpx.AsyncClient | None = None,
) -> TokenSet | None:
    """Mint a fresh access token from the stored refresh token.

    Returns None when there is nothing stored — the caller decides whether that
    means "log in" or "fall back to an API key". Propagates
    :class:`SessionExpiredError` when a stored token is rejected, because that
    is actionable and distinct from "never logged in".
    """
    refresh_token, unavailable = load_refresh_token(base_url)
    if unavailable:
        logger.info("keyring unavailable, cannot resume: %s", unavailable)
        return None
    if not refresh_token:
        return None

    config = OidcConfig(domain_url=domain_url, client_id=client_id, redirect_uri="")
    async with CognitoOidcClient(config, client=client) as oidc:
        tokens = await oidc.refresh(refresh_token)

    # Cognito can rotate refresh tokens; persist a new one if we got it.
    if tokens.refresh_token and tokens.refresh_token != refresh_token:
        try:
            save_refresh_token(base_url, tokens.refresh_token)
        except AgentCoreTuiError:
            logger.warning("rotated refresh token could not be persisted", exc_info=True)

    return tokens
