"""Cognito hosted-UI OAuth client for a public (secret-less) CLI.

Endpoints, all under the user pool's hosted-UI domain:

* ``/oauth2/authorize`` — where the browser goes
* ``/oauth2/token``     — code exchange and refresh
* ``/oauth2/revoke``    — invalidates a refresh token server-side
* ``/logout``           — ends the hosted-UI session

No client secret and therefore no HTTP Basic auth on the token call: a public
client authenticates the exchange with the PKCE verifier instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from ..errors import AgentCoreTuiError, ConfigError, ConnectionFailedError
from .pkce import PkceChallenge
from .tokens import TokenSet

logger = logging.getLogger(__name__)

SCOPES = ("openid", "profile", "email")


class AuthorizationError(AgentCoreTuiError):
    """The identity provider refused the authorization or token request."""

    hint = "Sign in again with `agentcore-tui login`. If it persists, the app client may be misconfigured."


class SessionExpiredError(AgentCoreTuiError):
    """The refresh token is no longer usable."""

    hint = "Your session expired or was revoked. Run `agentcore-tui login` to sign in again."


@dataclass(frozen=True, slots=True)
class OidcConfig:
    """Everything needed to talk to the hosted UI."""

    domain_url: str
    client_id: str
    redirect_uri: str

    def __post_init__(self) -> None:
        if not self.domain_url:
            raise ConfigError(
                "No Cognito domain configured",
                hint="Set `cognito_domain_url` in the config file (e.g. https://<prefix>.auth.<region>.amazoncognito.com).",
            )
        if not self.client_id:
            raise ConfigError(
                "No CLI app client id configured",
                hint="Set `cli_client_id` in the config file; find it at SSM /<prefix>/auth/cognito/cli-app-client-id.",
            )

    @property
    def base(self) -> str:
        return self.domain_url.rstrip("/")

    @property
    def authorize_endpoint(self) -> str:
        return f"{self.base}/oauth2/authorize"

    @property
    def token_endpoint(self) -> str:
        return f"{self.base}/oauth2/token"

    @property
    def revoke_endpoint(self) -> str:
        return f"{self.base}/oauth2/revoke"

    @property
    def logout_endpoint(self) -> str:
        return f"{self.base}/logout"


def build_authorize_url(
    config: OidcConfig,
    challenge: PkceChallenge,
    *,
    identity_provider: str | None = None,
) -> str:
    """URL to open in the user's browser.

    ``identity_provider`` jumps straight to a federated IdP instead of showing
    the hosted UI's provider chooser, matching what ``/auth/login?provider=``
    does on the web side.
    """
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "scope": " ".join(SCOPES),
        "state": challenge.state,
        "code_challenge": challenge.challenge,
        "code_challenge_method": challenge.method,
    }
    if identity_provider:
        params["identity_provider"] = identity_provider
    return f"{config.authorize_endpoint}?{urlencode(params)}"


def _error_detail(response: httpx.Response) -> str:
    """Extract an OAuth error without leaking the whole body."""
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        code = payload.get("error")
        description = payload.get("error_description")
        if code and description:
            return f"{code}: {description}"
        if code:
            return str(code)
    return f"HTTP {response.status_code}"


class CognitoOidcClient:
    """Token operations against the hosted UI."""

    def __init__(self, config: OidcConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    async def __aenter__(self) -> CognitoOidcClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _post_form(self, url: str, form: dict[str, str]) -> httpx.Response:
        try:
            return await self._client.post(
                url,
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            raise ConnectionFailedError(self._config.base, str(exc) or type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            raise ConnectionFailedError(self._config.base, str(exc) or type(exc).__name__) from exc

    async def exchange_code(self, code: str, challenge: PkceChallenge) -> TokenSet:
        """Trade an authorization code for tokens."""
        logger.info("exchanging authorization code at %s", self._config.token_endpoint)
        response = await self._post_form(
            self._config.token_endpoint,
            {
                "grant_type": "authorization_code",
                "client_id": self._config.client_id,
                "code": code,
                "redirect_uri": self._config.redirect_uri,
                "code_verifier": challenge.verifier,
            },
        )
        if response.status_code >= 400:
            detail = _error_detail(response)
            logger.error("code exchange failed: %s", detail)
            raise AuthorizationError(f"Could not complete sign-in ({detail})")

        tokens = TokenSet.from_token_response(response.json())
        logger.info(
            "sign-in complete; access token valid for %ds, refresh token %s",
            tokens.seconds_remaining,
            "received" if tokens.refresh_token else "NOT received",
        )
        return tokens

    async def refresh(self, refresh_token: str) -> TokenSet:
        """Mint a new access token from a refresh token."""
        logger.info("refreshing access token")
        response = await self._post_form(
            self._config.token_endpoint,
            {
                "grant_type": "refresh_token",
                "client_id": self._config.client_id,
                "refresh_token": refresh_token,
            },
        )
        if response.status_code >= 400:
            detail = _error_detail(response)
            logger.warning("refresh rejected: %s", detail)
            raise SessionExpiredError(f"Session could not be renewed ({detail})")

        # Cognito omits refresh_token on refresh responses; carry ours forward
        # or the next expiry would force a full re-login.
        return TokenSet.from_token_response(response.json()).with_refresh_token(refresh_token)

    async def revoke(self, refresh_token: str) -> bool:
        """Invalidate a refresh token server-side. False if the attempt failed.

        Best-effort by design: logout must still clear local state even when
        the network call fails, or a user on a plane can never log out.
        """
        try:
            response = await self._post_form(
                self._config.revoke_endpoint,
                {"token": refresh_token, "client_id": self._config.client_id},
            )
        except AgentCoreTuiError:
            logger.warning("revocation request failed; clearing local tokens anyway", exc_info=True)
            return False

        if response.status_code >= 400:
            logger.warning("revocation refused: %s", _error_detail(response))
            return False
        return True
