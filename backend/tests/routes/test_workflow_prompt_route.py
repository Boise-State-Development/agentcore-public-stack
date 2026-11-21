"""``workflow_prompt`` on ``/invocations`` (workflow-designer.md §21.3): dispatched beside the brief.

The property a turn depends on is that nothing about it moved: a body without the field builds
the agent it built before, and a body with the field never reaches the turn path at all (no agent
build, no lease, no tools, no system prompt).
"""

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import apis.inference_api.chat.routes as routes_module
import apis.inference_api.chat.workflow_prompt as workflow_prompt_module
from apis.inference_api.chat.models import InvocationRequest
from apis.inference_api.chat.routes import router
from apis.shared.auth.dependencies import get_current_user_trusted

BODY = {"session_id": "src", "message": "draft a rubric for this", "model_id": None, "enabled_tools": ["web_search"]}


@pytest.fixture
def user(make_user):
    return make_user(raw_token="fake-jwt-token", user_id="alice")


@pytest.fixture
def client(user):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user_trusted] = lambda: user
    return TestClient(app)


def _agent() -> MagicMock:
    agent = MagicMock()

    async def fake_stream(*args, **kwargs):
        yield "event: done\ndata: {}\n\n"

    agent.stream_async = fake_stream
    return agent


def _post(client, body) -> Dict[str, Any]:
    get_agent = AsyncMock(return_value=_agent())
    seen: Dict[str, Any] = {}

    async def fake_stream(request, user, session_id, **deps):
        seen.update(request=request, session_id=session_id)
        yield 'event: done\ndata: {"type": "done", "runId": "r1"}\n\n'

    with patch.object(routes_module, "get_agent", get_agent), patch.object(
        routes_module, "is_quota_enforcement_enabled", return_value=False
    ), patch.object(workflow_prompt_module, "stream_workflow_prompt", fake_stream):
        response = client.post("/invocations", json=body)
    return {"response": response, "get_agent": get_agent, "seen": seen}


def _kwargs(call) -> Dict[str, Any]:
    return {k: v for k, v in call.kwargs.items() if k not in ("build_stage_recorder", "build_detail_recorder")}


def test_a_turn_without_the_field_builds_exactly_the_agent_it_did(client):
    """Absent and explicit null are one request: same agent build, same system prompt and tools."""
    absent = _post(client, BODY)
    null = _post(client, {**BODY, "workflow_prompt": None})

    assert absent["get_agent"].await_count == null["get_agent"].await_count == 1
    assert _kwargs(absent["get_agent"].await_args) == _kwargs(null["get_agent"].await_args)
    assert absent["seen"] == {} and null["seen"] == {}


def test_the_request_without_the_field_serializes_as_before():
    request = InvocationRequest(**BODY)

    assert request.workflow_prompt is None
    assert "workflow_prompt" not in request.model_dump(exclude_none=True)


def test_a_workflow_prompt_never_reaches_the_turn_path(client):
    result = _post(client, {**BODY, "workflow_prompt": {"runId": "r1", "stepIds": ["s1", "s2"]}})

    assert result["response"].status_code == 200
    assert result["response"].headers["content-type"].startswith("text/event-stream")
    assert result["get_agent"].await_count == 0
    assert result["seen"]["session_id"] == "src"
    assert result["seen"]["request"].step_ids == ["s1", "s2"]


def test_more_than_eight_step_ids_is_a_bad_request(client):
    result = _post(client, {**BODY, "workflow_prompt": {"runId": "r1", "stepIds": [f"s{n}" for n in range(9)]}})

    assert result["response"].status_code == 422
    assert result["get_agent"].await_count == 0
