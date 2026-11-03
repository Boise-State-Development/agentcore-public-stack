"""The turn path's Agent resolution and prompt composition, as callable seams.

A handoff brief (agent-handoffs-and-consults.md §4.5.1) re-sends a source turn's request to
read its prompt cache, so it must resolve the Agent and compose the system prompt through the
same functions the turn does, not a copy. These tests pin those functions' contracts; the
route-level suites keep covering the turn that calls them.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.responses import StreamingResponse

import apis.inference_api.chat.routes as routes_module
from apis.inference_api.chat.models import InvocationRequest
from apis.inference_api.chat.routes import (
    AGENT_UNAVAILABLE_MESSAGE,
    ResolvedTurnAgent,
    apply_turn_prompt_extras,
    compose_turn_system_prompt,
    resolve_turn_agent,
)


def _request(**overrides) -> InvocationRequest:
    body = {"session_id": "sess-1", "message": "hello", "rag_assistant_id": "ast-1"}
    body.update(overrides)
    return InvocationRequest(**body)


def _agent(**overrides) -> SimpleNamespace:
    fields = {"owner_id": "someone-else", "instructions": "Grade with the rubric.", "kind": None, "project_id": None}
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _patched(assistant, permission="viewer", *, plan=None):
    """The services behind the resolution, stubbed at their source modules."""
    mark_share = AsyncMock()
    bump = AsyncMock(return_value=False)
    patches = [
        patch(
            "apis.shared.assistants.service.get_assistant_with_access_check",
            AsyncMock(return_value=(assistant, permission)),
        ),
        patch(
            "apis.shared.assistants.version_resolution.resolve_invocation_agent",
            AsyncMock(return_value=(assistant, None)),
        ),
        patch("apis.shared.assistants.service.mark_share_as_interacted", mark_share),
        patch("apis.shared.assistants.service.bump_last_used_at", bump),
        patch("apis.shared.assistants.service.is_disabled_project_harness", AsyncMock(return_value=False)),
        patch("apis.shared.assistants.service.assistant_exists", AsyncMock(return_value=True)),
        patch.object(routes_module, "agents_enabled", return_value=plan is not None),
        patch.object(routes_module, "assistant_kb_full_context_enabled", return_value=False),
    ]
    if plan is not None:
        patches.append(patch.object(routes_module, "resolve_agent_invocation", AsyncMock(return_value=plan)))
    return patches, mark_share, bump


class _Applied:
    def __init__(self, patches):
        self._patches = patches

    def __enter__(self):
        for p in self._patches:
            p.start()

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()


@pytest.mark.asyncio
async def test_an_agent_resolves_with_its_governed_capabilities(make_user):
    agent = _agent()
    plan = SimpleNamespace(model_override="model-x", tools=["web_search"], skills=["rubric"], unavailable=None)
    patches, _, _ = _patched(agent, plan=plan)

    with _Applied(patches):
        resolved = await resolve_turn_agent(_request(), make_user(user_id="u-1"), "u-1", is_continuation=False)

    assert isinstance(resolved, ResolvedTurnAgent)
    assert (resolved.assistant, resolved.permission) == (agent, "viewer")
    assert (resolved.model_override, resolved.tools_override, resolved.skills_override) == (
        "model-x",
        ["web_search"],
        ["rubric"],
    )
    assert resolved.project_id is None and resolved.notice_event is None


@pytest.mark.asyncio
async def test_a_turn_records_use_and_a_brief_does_not(make_user):
    agent = _agent()
    user = make_user(user_id="u-1")

    patches, mark_share, bump = _patched(agent)
    with _Applied(patches):
        as_turn = await resolve_turn_agent(_request(), user, "u-1", is_continuation=False)
    assert mark_share.await_count == 1 and bump.await_count == 1

    patches, mark_share, bump = _patched(agent)
    with _Applied(patches):
        as_brief = await resolve_turn_agent(_request(), user, "u-1", is_continuation=False, record_use=False)
    assert mark_share.await_count == 0 and bump.await_count == 0

    # Skipping the bookkeeping changes nothing about what runs.
    assert (as_brief.assistant, as_brief.permission, as_brief.model_override) == (
        as_turn.assistant,
        as_turn.permission,
        as_turn.model_override,
    )


@pytest.mark.asyncio
async def test_an_unavailable_agent_is_a_refusal_not_an_exception(make_user):
    patches, _, _ = _patched(None, None)

    with _Applied(patches):
        resolved = await resolve_turn_agent(
            _request(session_id="preview-gone"), make_user(user_id="u-1"), "u-1", is_continuation=False
        )

    assert isinstance(resolved, StreamingResponse)
    chunks = [chunk async for chunk in resolved.body_iterator]
    body = "".join(c.decode() if isinstance(c, bytes) else c for c in chunks)
    assert AGENT_UNAVAILABLE_MESSAGE in body


def test_composition_is_byte_stable_for_the_same_inputs():
    request = _request()
    agent = _agent()
    compose = lambda: compose_turn_system_prompt(  # noqa: E731
        request, agent, system_prompt=None, project_id=None, personal_instructions="Be brief."
    )

    first, second = compose(), compose()

    assert first == second
    assert "Grade with the rubric." in first
    assert "Be brief." in first
    assert first.index("Grade with the rubric.") < first.index("Be brief.")


def test_a_preview_session_composes_the_requests_live_instructions():
    request = _request(session_id="preview-abc", system_prompt="Live draft instructions.")

    prompt = compose_turn_system_prompt(
        request, _agent(), system_prompt=request.system_prompt, project_id=None, personal_instructions=None
    )

    assert "Live draft instructions." in prompt
    assert "Grade with the rubric." not in prompt


@pytest.mark.asyncio
async def test_extras_add_personal_instructions_only_without_an_agent():
    with patch.object(routes_module, "should_resolve_custom_prompt", return_value=False):
        plain = await apply_turn_prompt_extras(
            _request(rag_assistant_id=None), "u-1", "Base.", "Be brief.", is_resume=False, is_continuation=False
        )
        with_agent = await apply_turn_prompt_extras(
            _request(), "u-1", "Base.", "Be brief.", is_resume=False, is_continuation=False
        )

    assert "Be brief." in plain
    assert with_agent == "Base."


@pytest.mark.asyncio
async def test_the_turn_resolves_its_agent_through_the_shared_function():
    """The route calls `resolve_turn_agent`, so a brief calling it gets what a turn gets."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apis.shared.auth.dependencies import get_current_user_trusted
    from apis.shared.auth.models import User

    app = FastAPI()
    app.include_router(routes_module.router)
    app.dependency_overrides[get_current_user_trusted] = lambda: User(
        email="u@example.edu", user_id="u-1", name="U", roles=["User"], raw_token="t"
    )
    refusal = routes_module._forbidden_turn(_request(session_id="preview-x"), "u-1", "stop here", persist=False)
    resolve = AsyncMock(return_value=refusal)

    with patch.object(routes_module, "resolve_turn_agent", resolve), patch.object(
        routes_module, "is_quota_enforcement_enabled", return_value=False
    ), patch.object(routes_module, "_session_has_messages", AsyncMock(return_value=False)), patch(
        "apis.shared.sessions.metadata.get_session_metadata", AsyncMock(return_value=None)
    ):
        response = TestClient(app).post(
            "/invocations", json={"session_id": "preview-x", "message": "hi", "rag_assistant_id": "ast-1"}
        )

    assert response.status_code == 200
    assert "stop here" in response.text
    assert resolve.await_count == 1
    assert resolve.await_args.kwargs == {"is_continuation": False}
