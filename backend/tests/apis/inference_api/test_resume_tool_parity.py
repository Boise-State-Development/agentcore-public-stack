"""A resumed turn carries the paused turn's tools, byte for byte.

Found on dev (2026-11-26): a scheduled run that paused on approving ``my_skill_create`` and was
answered from the inbox resumed with only ``report``, because the resume rebuilt client tools
and ``report`` and nothing else. A watch run's agent is never cached, so every one of its resumes
took that path, and so does any resume once the Runtime's agent cache has let the paused agent go.
The approved call came back "Unknown tool", and the changed ``toolConfig`` re-wrote the prefix.

Each test pauses a turn through the real recording path (the turn's ``_build_turn_tools``, the
``_record_*_on`` stamps, the coordinator's snapshot write) and resumes it with a request shaped as
the inbox worker sends it: a session id, an empty message and the answers, nothing else.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apis.inference_api.chat import routes
from apis.inference_api.chat.models import InvocationRequest
from apis.shared.auth.models import User

HEADLESS = "headless-abc123"
USER = User(email="user@example.com", user_id="user-1", name="User One", roles=[])


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", "true")
    monkeypatch.delenv("SKILLS_ENABLED", raising=False)
    monkeypatch.delenv("SKILL_AUTHORING_ENABLED", raising=False)


def _names(tools) -> list:
    return [t.tool_name for t in tools]


def _digest(tools) -> str:
    specs = [t.tool_spec for t in tools]
    return hashlib.sha256(json.dumps(specs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def _pause(request: InvocationRequest, enabled_tools: list, *, project_memory=None, project_id=None):
    """The paused turn's tools and the snapshot its pause writes."""
    from agents.main_agent.streaming.stream_coordinator import StreamCoordinator

    paused = await routes._build_turn_tools(
        request,
        USER,
        USER.user_id,
        enabled_tools,
        project_memory=project_memory,
        turn_has_document=False,
        turn_project_id=project_id,
    )
    paused_tools = paused.extra_tools + routes._build_client_tools(request)

    # What BaseAgent and get_agent stamp, then what `_build_main_agent` records.
    construction = {
        "enabled_tools": enabled_tools,
        "assistant_id": request.rag_assistant_id,
        "memory_binding": paused.memory_binding_key,
        "memory_context": project_memory.memory_context if project_memory else None,
    }
    wrapper = SimpleNamespace(_construction_snapshot=construction)
    routes._record_client_tools_on(wrapper, request)
    routes._record_run_mode_on(wrapper, request)
    routes._record_project_on(wrapper, project_id)

    agent = SimpleNamespace(_interrupt_state=SimpleNamespace(activated=True))
    with patch("apis.shared.sessions.metadata.set_paused_turn", new=AsyncMock()) as write:
        await StreamCoordinator()._persist_paused_turn_snapshot(agent, request.session_id, USER.user_id, wrapper)
    snapshot = write.await_args.args[2]
    # Through the stored shape, as `get_paused_turn` reads it back.
    snapshot = type(snapshot).model_validate(snapshot.model_dump(by_alias=True, exclude_none=True))
    return paused, paused_tools, snapshot


def _inbox_resume(session_id: str) -> InvocationRequest:
    return InvocationRequest(
        session_id=session_id,
        message="",
        interrupt_responses=[{"interruptId": "v1:approval:tu-1", "response": {"approved": True}}],
    )


@pytest.fixture()
def _documents():
    with patch.object(routes, "_document_tools_gate", new=AsyncMock(return_value="documents")):
        yield


@pytest.mark.asyncio
@pytest.mark.usefixtures("_documents")
async def test_a_resumed_watch_run_has_the_paused_tools_with_report_last():
    paused, paused_tools, snapshot = await _pause(
        InvocationRequest(session_id=HEADLESS, run_mode="watch"), ["my_skill_create"]
    )

    resume_request, resumed = await routes._build_resume_tools(_inbox_resume(HEADLESS), snapshot, USER, USER.user_id)

    assert "my_skill_create" in _names(resumed.extra_tools), "the approved call needs its tool"
    assert _names(resumed.extra_tools) == _names(paused_tools)
    assert _digest(resumed.extra_tools) == _digest(paused_tools)
    assert _names(resumed.extra_tools)[-1] == "report"
    assert resumed.extra_tools_key_described is paused.extra_tools_key_described is False
    assert resumed.document_tools_kind == paused.document_tools_kind
    assert routes._is_watch_run(resume_request)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_documents")
async def test_a_resumed_standard_run_has_the_paused_tools_and_its_cache_key_material():
    paused, paused_tools, snapshot = await _pause(
        InvocationRequest(session_id=HEADLESS, rag_assistant_id="agent-1"), ["my_skill_create", "create_artifact"]
    )

    resume_request, resumed = await routes._build_resume_tools(_inbox_resume(HEADLESS), snapshot, USER, USER.user_id)

    assert "my_skill_create" in _names(resumed.extra_tools)
    assert "report" not in _names(resumed.extra_tools)
    assert _names(resumed.extra_tools) == _names(paused_tools)
    assert _digest(resumed.extra_tools) == _digest(paused_tools)
    # Same key elements as the paused build, so a warm resume still lands on its slot.
    assert resumed.extra_tools_key_described == paused.extra_tools_key_described
    assert resumed.memory_binding_key == paused.memory_binding_key
    assert resume_request.rag_assistant_id == "agent-1"
    assert not routes._is_watch_run(resume_request)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_documents")
async def test_a_project_harness_resumes_with_project_files_without_memory_spaces(monkeypatch):
    """The binding names the project only while memory spaces are on; the snapshot names it always."""
    monkeypatch.delenv("MEMORY_SPACES_ENABLED", raising=False)
    paused, paused_tools, snapshot = await _pause(
        InvocationRequest(session_id=HEADLESS, rag_assistant_id="harness-1"), ["create_artifact"], project_id="proj-1"
    )

    assert snapshot.project_id == "proj-1"
    _, resumed = await routes._build_resume_tools(_inbox_resume(HEADLESS), snapshot, USER, USER.user_id)

    assert "project_files" in _names(paused_tools)
    assert _names(resumed.extra_tools) == _names(paused_tools)
    assert _digest(resumed.extra_tools) == _digest(paused_tools)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_documents")
async def test_a_project_harness_with_memory_resumes_with_its_memory_tools():
    from apis.inference_api.chat.project_memory import ProjectMemoryTurn

    memory = ProjectMemoryTurn(
        project_id="proj-1", shared_space_id="space-s", personal_space_id="space-p", memory_context="# memory"
    )
    paused, paused_tools, snapshot = await _pause(
        InvocationRequest(session_id=HEADLESS, rag_assistant_id="harness-1"),
        ["create_artifact"],
        project_memory=memory,
        project_id="proj-1",
    )

    _, resumed = await routes._build_resume_tools(_inbox_resume(HEADLESS), snapshot, USER, USER.user_id)

    assert resumed.memory_binding_key == paused.memory_binding_key == snapshot.memory_binding
    assert _names(resumed.extra_tools) == _names(paused_tools)
    assert _digest(resumed.extra_tools) == _digest(paused_tools)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_documents")
async def test_client_tools_come_after_every_family_as_on_the_paused_turn():
    spec = {"name": "shell", "description": "Run a command", "inputSchema": {"type": "object", "properties": {}}}
    paused, paused_tools, snapshot = await _pause(
        InvocationRequest(session_id="sess-1", client_tools=[spec]), ["create_artifact"]
    )

    resume_request, resumed = await routes._build_resume_tools(_inbox_resume("sess-1"), snapshot, USER, USER.user_id)

    assert _names(resumed.extra_tools)[-1] == "local_shell"
    assert _digest(resumed.extra_tools) == _digest(paused_tools)
    assert routes._client_tools_digest(resume_request) == routes._client_tools_digest(
        InvocationRequest(session_id="sess-1", client_tools=[spec])
    )


@pytest.mark.asyncio
async def test_a_second_pause_records_the_same_shape():
    """A rebuilt agent's construction dict has only what get_agent stamps, so the resume re-records."""
    with patch.object(routes, "_document_tools_gate", new=AsyncMock(return_value="")):
        _, _, snapshot = await _pause(
            InvocationRequest(session_id=HEADLESS, run_mode="watch", rag_assistant_id="harness-1"),
            ["my_skill_create"],
            project_id="proj-1",
        )
    resume_request = routes._resume_request(_inbox_resume(HEADLESS), snapshot)
    rebuilt = SimpleNamespace(_construction_snapshot={})
    routes._record_client_tools_on(rebuilt, resume_request)
    routes._record_run_mode_on(rebuilt, resume_request)
    routes._record_project_on(rebuilt, routes._project_id_from_snapshot(snapshot))
    assert rebuilt._construction_snapshot == {"run_mode": "watch", "project_id": "proj-1"}


def test_the_resume_branch_builds_through_the_turns_phase_and_skips_tool_summaries():
    """Pinned at the source: the route is too large to drive end to end here."""
    source = inspect.getsource(routes)
    assert "resume_request, resume_tools = await _build_resume_tools(" in source
    assert "watch_run = _is_watch_run(resume_request)" in source
    assert "extra_tools=resume_tools.extra_tools or None," in source
    assert "extra_tools_key_described=resume_tools.extra_tools_key_described," in source
    assert "has_document_tools=resume_tools.document_tools_kind," in source
    assert "turn_tool_summaries=not watch_run," in source
    # The resume branch's `watch_run` is assigned before the stream reads it.
    assert source.index("watch_run = _is_watch_run(resume_request)") < source.index("turn_tool_summaries=not watch_run,")
