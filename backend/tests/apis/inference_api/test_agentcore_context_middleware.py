"""Tests for AgentCoreContextMiddleware.

Verifies that Runtime headers are copied into BedrockAgentCoreContext on
each request and that the middleware is a no-op when headers are absent
(local development, unit tests without Runtime).
"""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.inference_api.middleware.agentcore_context import (
    HEADER_OAUTH2_CALLBACK_URL,
    HEADER_REQUEST_ID,
    HEADER_SESSION_ID,
    HEADER_WORKLOAD_ACCESS_TOKEN,
    AgentCoreContextMiddleware,
)


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AgentCoreContextMiddleware)

    @app.get("/echo")
    def echo() -> dict:
        return {"ok": True}

    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


class TestAgentCoreContextMiddleware:
    def test_copies_workload_access_token_to_context(self, client: TestClient) -> None:
        with patch(
            "apis.inference_api.middleware.agentcore_context.BedrockAgentCoreContext"
        ) as ctx:
            response = client.get(
                "/echo", headers={HEADER_WORKLOAD_ACCESS_TOKEN: "wat-abc123"}
            )

        assert response.status_code == 200
        ctx.set_workload_access_token.assert_called_once_with("wat-abc123")

    def test_copies_oauth2_callback_url_to_context(self, client: TestClient) -> None:
        with patch(
            "apis.inference_api.middleware.agentcore_context.BedrockAgentCoreContext"
        ) as ctx:
            client.get(
                "/echo",
                headers={HEADER_OAUTH2_CALLBACK_URL: "https://example.com/cb"},
            )

        ctx.set_oauth2_callback_url.assert_called_once_with("https://example.com/cb")

    def test_copies_session_and_request_id_to_context(
        self, client: TestClient
    ) -> None:
        with patch(
            "apis.inference_api.middleware.agentcore_context.BedrockAgentCoreContext"
        ) as ctx:
            client.get(
                "/echo",
                headers={
                    HEADER_SESSION_ID: "sess-1",
                    HEADER_REQUEST_ID: "req-1",
                },
            )

        ctx.set_request_context.assert_called_once_with(
            request_id="req-1", session_id="sess-1"
        )

    def test_noop_when_headers_absent(self, client: TestClient) -> None:
        """Local dev and tests without AgentCore Runtime must still work."""
        with patch(
            "apis.inference_api.middleware.agentcore_context.BedrockAgentCoreContext"
        ) as ctx:
            response = client.get("/echo")

        assert response.status_code == 200
        ctx.set_workload_access_token.assert_not_called()
        ctx.set_oauth2_callback_url.assert_not_called()
        ctx.set_request_context.assert_not_called()

    def test_session_id_defaults_request_id_to_empty(self, client: TestClient) -> None:
        """When session is present but request-id header is missing, the
        request_id falls back to empty string rather than None."""
        with patch(
            "apis.inference_api.middleware.agentcore_context.BedrockAgentCoreContext"
        ) as ctx:
            client.get("/echo", headers={HEADER_SESSION_ID: "sess-1"})

        ctx.set_request_context.assert_called_once_with(
            request_id="", session_id="sess-1"
        )
