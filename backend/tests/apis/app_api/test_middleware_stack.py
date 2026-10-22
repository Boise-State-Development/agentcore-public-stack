"""The app-api middleware stack is ordered on purpose — lock the order down.

`main.py` documents the intended request-side order:

    ProxiedRedirect → GZip → SessionRefresh → CSRF → AgentCoreContext
    → CORS → router

Starlette's `add_middleware` prepends, so that order is the reverse of the
call order in the module and easy to break by moving a call. Two placements
in particular carry a reason:

* ProxiedRedirect stays outermost, as its own docstring requires — it has to
  see the final response headers.
* GZip sits directly inside it, so it compresses everything the app emits
  (including the error bodies CSRF and SessionRefresh return) while leaving
  ProxiedRedirect's `Location` rewriting untouched.
"""

from __future__ import annotations

from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from apis.app_api.main import app
from apis.shared.middleware.agentcore_context import AgentCoreContextMiddleware
from apis.shared.middleware.compression import StreamSafeGZipMiddleware
from apis.shared.middleware.csrf import CSRFMiddleware
from apis.shared.middleware.proxied_redirect import (
    FORWARDED_PREFIX_HEADER,
    ProxiedRedirectMiddleware,
)
from apis.shared.middleware.session_refresh import SessionRefreshMiddleware

#: Outermost first — `app.user_middleware` is in request order.
EXPECTED_ORDER = [
    ProxiedRedirectMiddleware,
    StreamSafeGZipMiddleware,
    SessionRefreshMiddleware,
    CSRFMiddleware,
    AgentCoreContextMiddleware,
    CORSMiddleware,
]


def test_middleware_order_matches_the_documented_stack() -> None:
    assert [m.cls for m in app.user_middleware] == EXPECTED_ORDER


def test_compression_is_configured_for_json_not_for_cpu_burn() -> None:
    gzip_middleware = next(
        m for m in app.user_middleware if m.cls is StreamSafeGZipMiddleware
    )

    # Level 9 (Starlette's default) costs ~4x the CPU of level 6 for ~3%
    # more compression on this repo's largest payloads.
    assert gzip_middleware.kwargs["compresslevel"] == 6
    assert gzip_middleware.kwargs["minimum_size"] == 500


def test_redirect_rewriting_survives_the_compression_layer() -> None:
    """ProxiedRedirect still owns `Location` with GZip underneath it."""
    client = TestClient(app, follow_redirects=False)

    response = client.get(
        "/agents/",
        headers={
            "Host": "api.dev.boisestate.ai",
            "Accept-Encoding": "gzip",
            FORWARDED_PREFIX_HEADER: "/api",
        },
    )

    assert response.status_code == 307
    assert response.headers["location"] == "/api/agents"
