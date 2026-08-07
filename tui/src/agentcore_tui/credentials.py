"""What the client uses to prove who it is, and what that buys.

Phase 1 has exactly one answer: an API key. Phase 2 adds a second: an OIDC
session. They are **not** interchangeable, and that asymmetry is the reason this
module exists rather than a boolean somewhere.

``POST /chat/api-converse`` is the only API-key endpoint in all of app-api, and
it is a bare Bedrock Converse wrapper — no tools, no memory, no server-side
session. Everything else (``/sessions``, ``/models``, ``/tools``, the tool-using
agent behind ``/chat/stream``) is cookie-session authenticated. So "am I
authenticated?" is the wrong question; "which credential do I hold, and what can
it reach?" is the right one.

Before this existed, ``Config.is_complete`` meant "an API key is present". Under
OIDC an absent API key is *normal*, and a correctly signed-in user would have
been shown the not-configured help text.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum


class Capability(StrEnum):
    """A coarse feature area, gated by which credential the client holds.

    Coarse on purpose. The point is to let the UI say "sign in to browse
    conversations" instead of issuing a request that will 401, not to mirror
    every endpoint.
    """

    #: One stateless turn against a model. No tools, no memory, no persistence.
    CHAT = "chat"
    #: The tool-using agent, with memory and a server-side session.
    AGENT = "agent"
    #: Server-side conversation list and history.
    SESSIONS = "sessions"
    #: Discovery of models, tools and assistants.
    CATALOG = "catalog"


class CredentialSource(StrEnum):
    """Where the client's authority comes from."""

    #: Nothing usable. The UI shows setup help.
    NONE = "none"
    #: An API key in the keyring, the environment, or (with a warning) the config file.
    API_KEY = "api_key"
    #: An OIDC session: a refresh token in the keyring, exchanged for bearer tokens.
    SSO_SESSION = "sso_session"

    @property
    def usable(self) -> bool:
        return self is not CredentialSource.NONE

    @property
    def label(self) -> str:
        """Human-readable, for `status` output and the status bar."""
        return {
            CredentialSource.NONE: "not signed in",
            CredentialSource.API_KEY: "API key",
            CredentialSource.SSO_SESSION: "SSO session",
        }[self]

    def can(self, capability: Capability) -> bool:
        """True when this credential can reach ``capability``."""
        return capability in CAPABILITIES[self]


#: What each credential actually reaches. Derived from app-api's auth
#: dependencies, not from aspiration — see the module docstring.
CAPABILITIES: dict[CredentialSource, frozenset[Capability]] = {
    CredentialSource.NONE: frozenset(),
    CredentialSource.API_KEY: frozenset({Capability.CHAT}),
    CredentialSource.SSO_SESSION: frozenset({Capability.CHAT, Capability.AGENT, Capability.SESSIONS, Capability.CATALOG}),
}

#: Probes whether a stored SSO session exists for a base URL.
SessionProbe = Callable[[str], bool]


def _keyring_session_probe(base_url: str) -> bool:
    """Default probe: is there a refresh token in the keyring for this host?

    Imported lazily to keep ``config`` free of a dependency on ``auth``, and
    because the keyring import itself is slow and fails on headless hosts.
    """
    from .auth.tokens import load_refresh_token

    token, _unavailable = load_refresh_token(base_url)
    return bool(token)


def resolve_source(
    *,
    base_url: str,
    api_key: str | None,
    session_probe: SessionProbe | None = None,
) -> CredentialSource:
    """Decide which credential this client will present.

    An API key wins over a stored session today. That is not because it is
    better — a session is strictly more capable — but because no bearer
    transport exists yet, so choosing a session would select a path that cannot
    issue a request. Flip this when the app-api bearer branch lands; the test
    asserting the precedence is the place to record that decision.
    """
    if not base_url:
        # Without a host, no credential can be used against anything.
        return CredentialSource.NONE
    if api_key:
        return CredentialSource.API_KEY
    probe = session_probe or _keyring_session_probe
    if probe(base_url):
        return CredentialSource.SSO_SESSION
    return CredentialSource.NONE
