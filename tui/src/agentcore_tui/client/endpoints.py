"""URL construction for app-api.

One place that knows app-api's paths. This used to be a ``converse_url``
property on ``Config``, which put an HTTP path in the package's lowest-level
module and would have grown one property per endpoint.

Only paths the client actually calls, or is about to, are listed. Each is
covered by a test, so none of them is untested dead weight.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote


@dataclass(frozen=True, slots=True)
class Endpoints:
    """Absolute URLs derived from a deployment's base URL."""

    base_url: str

    @property
    def base(self) -> str:
        """The base URL without a trailing slash, so joins never double up."""
        return self.base_url.rstrip("/")

    def _join(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"

    # -- unauthenticated -----------------------------------------------------

    @property
    def health(self) -> str:
        return self._join("health")

    # -- CLI device authorization --------------------------------------------
    #
    # This is app-api's own flow, not Cognito's: Cognito does not support RFC
    # 8628. Both are pre-authentication by necessity, so neither takes an auth
    # header. `verify` is deliberately absent — it is opened in the user's
    # browser from the `verification_uri_complete` the server returns, and the
    # client must never construct that URL itself.

    @property
    def cli_authorize(self) -> str:
        """Start a device authorization and get a device code plus a user code."""
        return self._join("auth/cli/authorize")

    @property
    def cli_token(self) -> str:
        """Poll for the sealed session. RFC 8628 semantics: 400 means "not yet"."""
        return self._join("auth/cli/token")

    # -- session -------------------------------------------------------------

    @property
    def auth_session(self) -> str:
        """Who the current credential belongs to. Used to confirm a sign-in."""
        return self._join("auth/session")

    # -- chat ----------------------------------------------------------------

    @property
    def api_converse(self) -> str:
        """API-key authenticated single-turn chat. No tools, memory or session."""
        return self._join("chat/api-converse")

    @property
    def chat_stream(self) -> str:
        """Session authenticated agent stream, relayed to inference-api."""
        return self._join("chat/stream")

    # -- sessions ------------------------------------------------------------

    @property
    def sessions(self) -> str:
        return self._join("sessions")

    def session(self, session_id: str) -> str:
        return f"{self.sessions}/{quote(session_id, safe='')}"

    def session_messages(self, session_id: str) -> str:
        return f"{self.session(session_id)}/messages"

    def session_metadata(self, session_id: str) -> str:
        """Rename lives here — ``PUT`` with ``{"title": ...}``."""
        return f"{self.session(session_id)}/metadata"

    def session_read(self, session_id: str) -> str:
        return f"{self.session(session_id)}/read"

    def session_unread(self, session_id: str) -> str:
        return f"{self.session(session_id)}/unread"

    @property
    def sessions_bulk_delete(self) -> str:
        return self._join("sessions/bulk-delete")

    def session_interrupt(self, session_id: str) -> str:
        """The authoritative carrier of stop intent for an in-flight agent turn.

        Cancelling only the local stream leaves the server generating and
        holding the session lease, so this has to be called on cancel.
        """
        return f"{self.session(session_id)}/interrupt"

    def session_pending_interrupts(self, session_id: str) -> str:
        """How a client discovers a turn that paused while it was disconnected."""
        return f"{self.session(session_id)}/pending-interrupts"

    # -- discovery and preferences -------------------------------------------

    @property
    def models(self) -> str:
        return self._join("models")

    @property
    def tools(self) -> str:
        # Trailing slash: the router mounts it that way and the bare path 307s.
        return self._join("tools/")

    @property
    def tool_preferences(self) -> str:
        """``PUT`` the user's per-tool on/off choices."""
        return self._join("tools/preferences")

    @property
    def skills(self) -> str:
        return self._join("skills/")

    @property
    def skill_preferences(self) -> str:
        return self._join("skills/preferences")

    @property
    def system_prompts(self) -> str:
        """ "Conversation modes" in the web UI. Selected by ``selected_prompt_id``."""
        return self._join("system-prompts/")

    # -- chat helpers --------------------------------------------------------

    @property
    def generate_title(self) -> str:
        """Names a conversation. The SPA fires this alongside the first turn."""
        return self._join("chat/generate-title")
