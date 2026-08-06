"""OIDC authentication for the terminal client.

Authorization-code + PKCE against a public Cognito app client, with a loopback
redirect (RFC 8252). No client secret is involved, and the only persisted
credential is the refresh token, which lives in the OS keyring.
"""

from __future__ import annotations

from .flow import (
    DEFAULT_CALLBACK_PORTS,
    LoginOutcome,
    StateMismatchError,
    perform_login,
    resume_session,
)
from .loopback import CallbackResult, LoopbackError, LoopbackReceiver, find_free_port
from .oidc import (
    AuthorizationError,
    CognitoOidcClient,
    OidcConfig,
    SessionExpiredError,
    build_authorize_url,
)
from .pkce import PkceChallenge, challenge_for, generate_state, generate_verifier
from .tokens import (
    TokenSet,
    decode_claims,
    delete_refresh_token,
    describe_stored_session,
    load_refresh_token,
    save_refresh_token,
)

__all__ = [
    "DEFAULT_CALLBACK_PORTS",
    "AuthorizationError",
    "CallbackResult",
    "CognitoOidcClient",
    "LoginOutcome",
    "LoopbackError",
    "LoopbackReceiver",
    "OidcConfig",
    "PkceChallenge",
    "SessionExpiredError",
    "StateMismatchError",
    "TokenSet",
    "build_authorize_url",
    "challenge_for",
    "decode_claims",
    "delete_refresh_token",
    "describe_stored_session",
    "find_free_port",
    "generate_state",
    "generate_verifier",
    "load_refresh_token",
    "perform_login",
    "resume_session",
    "save_refresh_token",
]
