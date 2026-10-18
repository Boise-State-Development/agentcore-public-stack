"""The route carries the SPA's panel height to tools, and nowhere else.

``browser_frame_height`` on the request becomes Strands ``invocation_state``
on the agent call (spec D3a). It must not change what the model is sent:
nothing of it is written into the message the agent streams.
"""

from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.inference_api.chat.routes import router
from apis.shared.auth.dependencies import get_current_user_trusted


@pytest.fixture
def client(make_user):
    app = FastAPI()
    app.include_router(router)
    user = make_user(raw_token="fake-jwt-token")
    app.dependency_overrides[get_current_user_trusted] = lambda: user
    return TestClient(app)


def _post(client: TestClient, body: Dict[str, Any]) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    agent = MagicMock()

    async def fake_stream(*args: Any, **kwargs: Any):
        calls.append({"args": args, **kwargs})
        yield "event: done\ndata: {}\n\n"

    agent.stream_async = fake_stream
    with patch("apis.inference_api.chat.routes.get_agent", return_value=agent), patch(
        "apis.inference_api.chat.routes.is_quota_enforcement_enabled",
        return_value=False,
    ):
        resp = client.post("/invocations", json=body)
    assert resp.status_code == 200
    return calls


def test_the_panel_height_reaches_invocation_state(client) -> None:
    calls = _post(
        client,
        {"session_id": "sess-fh-1", "message": "Hello", "browser_frame_height": 778},
    )

    assert len(calls) == 1
    assert calls[0]["invocation_state"] == {"browser_takeover_frame_height": 778}
    # Client data, not prompt data: nothing of it is written into the message.
    assert "778" not in calls[0]["args"][0]


def test_no_height_passes_no_invocation_state(client) -> None:
    calls = _post(client, {"session_id": "sess-fh-2", "message": "Hello"})

    assert calls[0]["invocation_state"] is None


def test_an_unusable_height_is_dropped_not_rejected(client) -> None:
    calls = _post(
        client,
        {"session_id": "sess-fh-3", "message": "Hello", "browser_frame_height": "tall"},
    )

    assert calls[0]["invocation_state"] is None
