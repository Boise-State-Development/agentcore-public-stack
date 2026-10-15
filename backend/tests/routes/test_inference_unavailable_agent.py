"""A turn on an agent the caller cannot use is one conversational refusal (B7).

A deleted agent used to answer 404 and an inaccessible one 403, so a caller
could probe whether any agent or project harness id existed. Both now stream the
same assistant message, byte for byte, and neither status is an HTTP error.

Driven through the real `/invocations` handler; only the services behind it are
stubbed.
"""

import json
from typing import List
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import apis.inference_api.chat.routes as routes_module
from apis.inference_api.chat.routes import AGENT_UNAVAILABLE_MESSAGE, router
from apis.shared.auth.dependencies import get_current_user_trusted


def _frames(body: str, kind: str) -> List[dict]:
    prefix = f"event: {kind}\ndata: "
    return [json.loads(f[len(prefix):]) for f in body.split("\n\n") if f.startswith(prefix)]


@pytest.fixture
def client(make_user):
    app = FastAPI()
    app.include_router(router)
    user = make_user(raw_token="fake-jwt-token", user_id="user-1")
    app.dependency_overrides[get_current_user_trusted] = lambda: user
    return TestClient(app)


def _post(client: TestClient, *, exists: bool):
    get_agent = AsyncMock(side_effect=AssertionError("no agent is built for a refused turn"))
    with patch.object(routes_module, "get_agent", get_agent), patch.object(
        routes_module, "is_quota_enforcement_enabled", return_value=False
    ), patch.object(routes_module, "agents_enabled", return_value=False), patch.object(
        routes_module, "_session_has_messages", AsyncMock(return_value=False)
    ), patch(
        "apis.shared.assistants.service.get_assistant_with_access_check",
        AsyncMock(return_value=(None, None)),
    ), patch(
        "apis.shared.assistants.service.is_disabled_project_harness", AsyncMock(return_value=False)
    ), patch(
        "apis.shared.assistants.service.assistant_exists", AsyncMock(return_value=exists)
    ), patch(
        "apis.shared.sessions.metadata.get_session_metadata", AsyncMock(return_value=None)
    ):
        return client.post(
            "/invocations",
            json={"session_id": "preview-b7", "message": "hello", "rag_assistant_id": "ast-x"},
        )


def test_gone_and_forbidden_are_indistinguishable(client):
    gone, forbidden = _post(client, exists=False), _post(client, exists=True)

    assert gone.status_code == forbidden.status_code == 200
    assert gone.text == forbidden.text


def test_the_refusal_is_a_streamed_assistant_message(client):
    body = _post(client, exists=False).text

    [delta] = _frames(body, "content_block_delta")
    assert delta["text"] == AGENT_UNAVAILABLE_MESSAGE
    [error] = _frames(body, "stream_error")
    assert (error["code"], error["recoverable"]) == ("forbidden", False)
    assert "ast-x" not in body
