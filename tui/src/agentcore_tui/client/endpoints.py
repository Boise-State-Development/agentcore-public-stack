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

    def session_interrupt(self, session_id: str) -> str:
        """The authoritative carrier of stop intent for an in-flight agent turn.

        Cancelling only the local stream leaves the server generating and
        holding the session lease, so this has to be called on cancel.
        """
        return f"{self.session(session_id)}/interrupt"

    # -- discovery -----------------------------------------------------------

    @property
    def models(self) -> str:
        return self._join("models")

    @property
    def tools(self) -> str:
        return self._join("tools")
