"""What the client uses to prove who it is, and what that buys.

Phase 1 has exactly one answer: an API key. Phase 2 adds a second: a real BFF
session, obtained by app-api's own device-authorization flow. They are **not**
interchangeable, and that asymmetry is the reason this module exists rather than
a boolean somewhere.

``POST /chat/api-converse`` is the only API-key endpoint in all of app-api, and
it is a bare Bedrock Converse wrapper — no tools, no memory, no server-side
session. Everything else (``/sessions``, ``/models``, ``/tools``, the tool-using
agent behind ``/chat/stream``) is cookie-session authenticated. So "am I
authenticated?" is the wrong question; "which credential do I hold, and what can
it reach?" is the right one.

Before this existed, ``Config.is_complete`` meant "an API key is present". Under
a session an absent API key is *normal*, and a correctly signed-in user would
have been shown the not-configured help text.
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
    #: A real BFF session obtained by the CLI device-authorization flow and
    #: presented as `Authorization: BFF <sealed>`. Not an OIDC token: the client
    #: never talks to Cognito, and the value is opaque to it.
    BFF_SESSION = "bff_session"

    @property
    def usable(self) -> bool:
        return self is not CredentialSource.NONE

    @property
    def label(self) -> str:
        """Human-readable, for `status` output and the status bar."""
        return {
            CredentialSource.NONE: "not signed in",
            CredentialSource.API_KEY: "API key",
            CredentialSource.BFF_SESSION: "signed in",
        }[self]

    def can(self, capability: Capability) -> bool:
        """True when this credential can reach ``capability``."""
        return capability in CAPABILITIES[self]


#: What each credential actually reaches. Derived from app-api's auth
#: dependencies, not from aspiration — see the module docstring.
CAPABILITIES: dict[CredentialSource, frozenset[Capability]] = {
    CredentialSource.NONE: frozenset(),
    CredentialSource.API_KEY: frozenset({Capability.CHAT}),
    CredentialSource.BFF_SESSION: frozenset({Capability.CHAT, Capability.AGENT, Capability.SESSIONS, Capability.CATALOG}),
}

#: Probes whether a stored BFF session exists for a base URL.
SessionProbe = Callable[[str], bool]


def _keyring_session_probe(base_url: str) -> bool:
    """Default probe: is there a sealed session in the keyring for this host?

    Cheap on purpose — presence, not validity. The client cannot decrypt a
    sealed session to check its expiry, so the only real test is a request, and
    that is too expensive for a startup path. A stale session therefore shows as
    signed in until the first 401, which is the same behaviour a browser has
    with an expired cookie.
    """
    from . import keyring_store

    session, _unavailable = keyring_store.load(keyring_store.SESSION_SERVICE, base_url)
    return bool(session)


def resolve_source(
    *,
    base_url: str,
    api_key: str | None,
    session_probe: SessionProbe | None = None,
) -> CredentialSource:
    """Decide which credential this client will present.

    An API key still wins over a stored session, and that is now a *temporary*
    inversion of merit rather than a permanent one. A session is strictly more
    capable — it reaches the agent, sessions and catalogs that an API key cannot
    — but the only transport built today is ``converse.py``, which speaks
    ``POST /chat/api-converse``, and that endpoint authenticates with
    ``X-API-Key`` and nothing else. Preferring a session would therefore select
    a credential the working transport cannot present.

    **Flip this when ``client/agent_stream.py`` lands.** The test asserting this
    precedence is the place that records the decision, so changing the order
    means changing a test that explains why.
    """
    if not base_url:
        # Without a host, no credential can be used against anything.
        return CredentialSource.NONE
    if api_key:
        return CredentialSource.API_KEY
    probe = session_probe or _keyring_session_probe
    if probe(base_url):
        return CredentialSource.BFF_SESSION
    return CredentialSource.NONE
