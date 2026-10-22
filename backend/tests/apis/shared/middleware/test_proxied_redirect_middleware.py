"""Tests for ProxiedRedirectMiddleware.

The regression these lock down: loading a conversation page on dev logged

    Mixed Content: ... requested an insecure resource
    'http://api.dev.boisestate.ai/agents'. This request has been blocked.

That URL is not built anywhere in the SPA — it is Starlette's own
`redirect_slashes` answer to `GET /api/agents/`, rendered from what app-api
can see behind CloudFront: the ALB's hostname, plain HTTP, and a path with
the `/api` prefix already stripped off.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import RedirectResponse

from apis.shared.middleware.proxied_redirect import (
    FORWARDED_PREFIX_HEADER,
    ProxiedRedirectMiddleware,
)

PROXY_HOST = "api.dev.boisestate.ai"


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(ProxiedRedirectMiddleware)

    # Declared without a trailing slash, exactly like the real `/agents`
    # router — so `GET /agents/` is answered by `redirect_slashes`.
    @app.get("/agents")
    def list_agents() -> dict:
        return {"agents": []}

    @app.get("/agents/pins")
    def list_pins() -> dict:
        return {"pins": []}

    @app.get("/auth/login")
    def login() -> RedirectResponse:
        return RedirectResponse(
            url="https://login.microsoftonline.com/oauth2/authorize?client_id=x",
            status_code=302,
        )

    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, follow_redirects=False)


def _proxied(path: str) -> dict:
    """Headers as CloudFront's `/api/*` behaviour delivers them to the ALB."""
    return {"host": PROXY_HOST, FORWARDED_PREFIX_HEADER: "/api"}


class TestTrailingSlashRedirects:
    def test_agents_redirect_is_relative_and_keeps_the_api_prefix(
        self, client: TestClient
    ) -> None:
        response = client.get("/agents/", headers=_proxied("/agents/"))

        assert response.status_code == 307
        assert response.headers["location"] == "/api/agents"

    def test_location_never_names_the_origin_host_or_plain_http(
        self, client: TestClient
    ) -> None:
        response = client.get("/agents/", headers=_proxied("/agents/"))

        location = response.headers["location"]
        assert "http://" not in location
        assert PROXY_HOST not in location

    def test_nested_path_keeps_its_full_path(self, client: TestClient) -> None:
        response = client.get("/agents/pins/", headers=_proxied("/agents/pins/"))

        assert response.headers["location"] == "/api/agents/pins"

    def test_query_string_survives_the_rewrite(self, client: TestClient) -> None:
        response = client.get(
            "/agents/?include_drafts=false", headers=_proxied("/agents/")
        )

        assert response.headers["location"] == "/api/agents?include_drafts=false"


class TestWithoutTheProxy:
    """Local dev and direct-to-ALB calls: no prefix was stripped, so none is added."""

    def test_redirect_is_root_relative_when_no_prefix_header_is_present(
        self, client: TestClient
    ) -> None:
        response = client.get("/agents/", headers={"host": "localhost:8000"})

        assert response.headers["location"] == "/agents"


class TestRedirectsWeMustNotTouch:
    def test_cross_host_redirect_is_left_alone(self, client: TestClient) -> None:
        """The BFF's OAuth bounce to Entra/Cognito must stay absolute."""
        response = client.get("/auth/login", headers=_proxied("/auth/login"))

        assert response.status_code == 302
        assert response.headers["location"] == (
            "https://login.microsoftonline.com/oauth2/authorize?client_id=x"
        )

    def test_non_redirect_responses_are_untouched(self, client: TestClient) -> None:
        response = client.get("/agents", headers=_proxied("/agents"))

        assert response.status_code == 200
        assert "location" not in response.headers


class TestPrefixSanitization:
    """The header is overwritten by CloudFront, but a `Location` is worth guarding."""

    @pytest.mark.parametrize(
        "spoofed",
        ["//evil.example", "evil.example", "https://evil.example", ""],
    )
    def test_a_prefix_that_could_escape_the_origin_is_dropped(
        self, client: TestClient, spoofed: str
    ) -> None:
        response = client.get(
            "/agents/",
            headers={"host": PROXY_HOST, FORWARDED_PREFIX_HEADER: spoofed},
        )

        location = response.headers["location"]
        assert location == "/agents"
        assert not location.startswith("//")

    def test_trailing_slash_on_the_prefix_does_not_double_up(
        self, client: TestClient
    ) -> None:
        response = client.get(
            "/agents/", headers={"host": PROXY_HOST, FORWARDED_PREFIX_HEADER: "/api/"}
        )

        assert response.headers["location"] == "/api/agents"
