"""Carry an app-tool error across the AgentCore Runtime boundary.

inference-api runs behind AgentCore Runtime, which rewrites **any** non-2xx
container response to a generic ``424``::

    {"message": "Received error (409) from runtime. Please check your
     CloudWatch logs for more information."}

Both the status *and* the human-readable message are destroyed. So an
app-initiated `tools/call` that needs OAuth consent — which
`dispatch_app_tool_call` reports as a deliberate 409 carrying "Connect the
account, then try again" — reached the SPA as a 424 whose body told the user
to go read CloudWatch logs. Verified live on dev 2026-09-08.

The fix: inference-api answers **200** with the error in the body instead,
and app-api translates it back to the real HTTP status before replying to
the SPA. The SPA's contract is unchanged — it still sees 403 / 409 / 502
with ``{"error": "<message>"}`` — so only the one hop that crosses AgentCore
changes shape.

**Never widen `_RELAYABLE_STATUSES` to include 401.** The SPA's
`error.interceptor` treats any 401 as an expired BFF session and redirects
to login, so relaying a 401 here would sign the user out over an unconnected
connector.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from fastapi.responses import JSONResponse

# The key is deliberately distinct from the plain ``error`` key a direct
# (non-AgentCore) inference-api response uses, so app-api can tell an
# enveloped error from an ordinary error body without guessing.
ENVELOPE_KEY = "appToolError"

# Statuses app-api will re-emit from an envelope. An upstream that names
# anything else collapses to 502 rather than letting a malformed payload
# choose app-api's status code.
_RELAYABLE_STATUSES = frozenset({400, 403, 404, 409, 422, 502})

_FALLBACK_STATUS = 502
_FALLBACK_MESSAGE = "Tool call failed"


def build_error_envelope(message: str, code: int) -> Dict[str, Any]:
    """Body for a 200 response that actually reports an error.

    Returned by inference-api in place of ``JSONResponse({...}, code)``,
    which AgentCore would flatten.
    """
    return {ENVELOPE_KEY: {"code": int(code), "message": str(message)}}


def app_tool_error_response(message: str, code: int) -> "JSONResponse":
    """inference-api's reply for a failed app-tool call: **200** + envelope.

    The 200 is the whole point — returning `code` directly is what AgentCore
    flattens into a 424. Use this instead of `JSONResponse({...}, code)` in
    any inference-api handler whose response crosses the Runtime boundary.
    """
    from fastapi.responses import JSONResponse

    return JSONResponse(build_error_envelope(message, code), status_code=200)


def read_error_envelope(
    payload: Any,
) -> Optional[Tuple[str, int]]:
    """Return ``(message, status)`` if `payload` carries an envelope.

    ``None`` means the payload is an ordinary result and the caller should
    relay it unchanged. A malformed or unlisted status falls back to 502 so
    a bad upstream can never pick app-api's status code — in particular it
    can never produce a 401.
    """
    if not isinstance(payload, dict):
        return None
    envelope = payload.get(ENVELOPE_KEY)
    if not isinstance(envelope, dict):
        return None

    raw_message = envelope.get("message")
    message = (
        raw_message.strip()
        if isinstance(raw_message, str) and raw_message.strip()
        else _FALLBACK_MESSAGE
    )

    raw_code = envelope.get("code")
    try:
        code = int(raw_code)
    except (TypeError, ValueError):
        code = _FALLBACK_STATUS
    if code not in _RELAYABLE_STATUSES:
        code = _FALLBACK_STATUS

    return message, code
