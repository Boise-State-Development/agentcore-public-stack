"""Cognito `/oauth2/token` exchange for the BFF authorization-code flow.

Called from `/auth/callback` with the `code` param Cognito just handed
us. We POST it to the Hosted UI's token endpoint authenticated with HTTP
Basic (`client_id:client_secret` — confidential client, secret never
leaves the server) and parse the resulting access / refresh / id token
trio plus expiry.

The ID token is decoded *without* signature verification: Cognito just
minted it on the same redirect chain, and only its `sub` /
`cognito:username` / `email` / `name` / `picture` claims are read here
to seed the session row. The access token is what drives downstream
auth — its signature is checked by `_get_bff_cognito_validator()` on
every request through the dependency.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

import httpx
import jwt

logger = logging.getLogger(__name__)

# Bound the token-endpoint call so a Cognito hiccup can't pin a callback
# request open. The user-visible flow is "redirect → spinner → app", so a
# 10s ceiling is generous but still safe.
_TOKEN_ENDPOINT_TIMEOUT_SECONDS = 10.0


class TokenExchangeError(Exception):
    """Raised when the Cognito token exchange fails for any reason.

    Caller should clear cookies and surface a generic auth-failed error to
    the browser — distinguishing causes here would mostly help attackers.
    """


@dataclass
class ExchangeResult:
    access_token: str
    refresh_token: str
    id_token: Optional[str]
    access_token_exp: int  # epoch seconds


@dataclass
class IdTokenClaims:
    sub: str
    username: str  # cognito:username — required to compute SECRET_HASH on refresh
    email: Optional[str]
    name: Optional[str]
    picture: Optional[str]
    # IdP-mapped roles parsed from `custom:roles` (preferred) or
    # `cognito:groups` (fallback). The access token also carries these,
    # but the BFF cookie path syncs the Users table off the ID-token
    # decode at /auth/callback — so we extract them here once instead of
    # threading another decode through the call site.
    roles: List[str] = field(default_factory=list)


def _extract_roles_from_id_token(claims: dict) -> List[str]:
    """Mirror of `CognitoJWTValidator._extract_roles` for the ID-token decode.

    Preference order:
      1. ``custom:roles`` — IdP roles mapped via Cognito attribute mapping.
         Value is a string that may be a JSON array (e.g. Entra sends
         ``'["Admin","Staff"]'``) or a comma-separated list.
      2. ``cognito:groups`` — fallback when the IdP didn't map roles.
    """
    custom_roles = claims.get("custom:roles", "")
    if custom_roles:
        try:
            parsed = json.loads(custom_roles)
            if isinstance(parsed, list):
                return [str(r).strip() for r in parsed if str(r).strip()]
        except (json.JSONDecodeError, TypeError):
            logger.debug(
                "custom:roles claim is not a JSON list; falling back to comma-separated parsing"
            )
        return [r.strip() for r in str(custom_roles).split(",") if r.strip()]

    groups = claims.get("cognito:groups")
    if isinstance(groups, list):
        return [str(g) for g in groups]
    return []


async def exchange_code_for_tokens(
    *,
    cognito_domain_url: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    http_client: Optional[httpx.AsyncClient] = None,
) -> ExchangeResult:
    """Exchange an authorization code for Cognito tokens.

    `http_client` is injected so tests can swap in a mock transport without
    needing real network egress.
    """
    token_url = f"{cognito_domain_url.rstrip('/')}/oauth2/token"
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    auth = (client_id, client_secret)

    try:
        if http_client is None:
            async with httpx.AsyncClient(
                timeout=_TOKEN_ENDPOINT_TIMEOUT_SECONDS
            ) as client:
                response = await client.post(token_url, data=data, auth=auth)
        else:
            response = await http_client.post(token_url, data=data, auth=auth)
    except httpx.HTTPError as exc:
        logger.warning("BFF token exchange transport failure: %s", exc)
        raise TokenExchangeError("Token endpoint unreachable") from exc

    if response.status_code != 200:
        logger.warning(
            "BFF token exchange failed: status=%s body=%s",
            response.status_code,
            # Bodies from Cognito's token endpoint are small and don't carry
            # PII — safe to log truncated for diagnosability.
            response.text[:512],
        )
        raise TokenExchangeError(f"Token endpoint returned {response.status_code}")

    payload = response.json()
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    if not access_token or not refresh_token:
        raise TokenExchangeError("Token response missing access or refresh token")

    expires_in = int(payload.get("expires_in", 3600))
    return ExchangeResult(
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=payload.get("id_token"),
        access_token_exp=int(time.time()) + expires_in,
    )


def decode_id_token_claims(id_token: str) -> IdTokenClaims:
    """Pull the identity claims off the ID token without verifying.

    Verification would require fetching JWKS and parsing — pointless work
    for a token Cognito just issued on the same TLS-protected redirect.
    The access token (which IS verified per request) is the security
    boundary; this is just a convenient way to read identity claims.
    """
    try:
        claims = jwt.decode(id_token, options={"verify_signature": False})
    except jwt.DecodeError as exc:
        raise TokenExchangeError("Malformed ID token") from exc

    sub = claims.get("sub")
    username = claims.get("cognito:username") or claims.get("username") or sub
    if not sub or not username:
        raise TokenExchangeError("ID token missing required identity claims")

    return IdTokenClaims(
        sub=str(sub),
        username=str(username),
        email=(claims.get("email") or "").lower() or None,
        name=claims.get("name")
        or (
            f"{claims.get('given_name', '')} {claims.get('family_name', '')}".strip()
            or None
        ),
        picture=claims.get("picture"),
        roles=_extract_roles_from_id_token(claims),
    )
