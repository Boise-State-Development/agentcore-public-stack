"""A chat turn on a Shared Project's harness (shared-projects PR-1.4a).

Covers what changes when the turn's agent is a project's hidden harness:

  - **Degrade with notice (§9.6).** A member missing a bound tool, skill, model or memory
    space still gets a turn; the missing piece is dropped and named in an
    ``agent_notice`` event. Ordinary shared agents keep block-with-message (D5).
  - **The prompt.** ``## Project Instructions`` replaces the agent heading, nothing about
    the invoking member enters the text, and every other agent's text is unchanged.
  - **Archived projects** refuse new turns.
  - **Cost.** Each call's row carries ``projectId``, and the call is added to the
    project's month and to that member's share of it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from moto import mock_aws

from apis.inference_api.chat.agent_binding_resolver import (
    AgentBindingBlockedError,
    AgentNoticeEvent,
    resolve_agent_invocation,
)
from apis.inference_api.chat.routes import _project_turn_refusal, compose_agent_system_prompt
from apis.shared.assistants.models import AgentModelConfig
from apis.shared.projects.models import Project
from apis.shared.projects.repository import ProjectRepository
from apis.shared.sessions.metadata import _update_project_rollup_async
from apis.shared.sessions.models import MessageMetadata, TokenUsage

from tests.apis.inference_api.test_agent_binding_resolver import (
    _assistant,
    _mem_binding,
    _patch_access,
    _patch_memory,
    _patch_skill_access,
    _patch_tool_access,
    _skill_binding,
    _tool_binding,
    _user,
)

PROJECTS_TABLE = "test-projects-invocation"


# ── degrade with notice ─────────────────────────────────────────────────


class TestDegrade:
    @pytest.mark.asyncio
    async def test_a_denied_server_drops_every_ref_of_it_and_keeps_the_rest(self, monkeypatch):
        # The gate is asked once per server, with that server's first bound ref.
        _patch_tool_access(monkeypatch, {"canvas::list_courses", "web_search"})
        agent = _assistant(
            bindings=[
                _tool_binding("canvas::list_courses"),
                _tool_binding("sis::lookup_student"),
                _tool_binding("web_search"),
                _tool_binding("sis::lookup_course"),  # same denied server, later ref
            ]
        )

        plan = await resolve_agent_invocation(agent, _user(), degrade=True)

        assert plan.tools.tool_ids == ["canvas::list_courses", "web_search"]
        assert plan.unavailable.tools == ["sis"]

    @pytest.mark.asyncio
    async def test_all_tools_dropped_is_still_an_empty_toolset_not_the_requests(self, monkeypatch):
        _patch_tool_access(monkeypatch, False)
        plan = await resolve_agent_invocation(
            _assistant(bindings=[_tool_binding("sis")]), _user(), degrade=True
        )
        assert plan.tools is not None and plan.tools.tool_ids == []

    @pytest.mark.asyncio
    async def test_a_denied_model_falls_through_to_the_members_default(self, monkeypatch):
        _patch_access(monkeypatch, False)
        agent = _assistant(model_settings=AgentModelConfig(model_id="us.anthropic.claude-opus-4-7"))

        plan = await resolve_agent_invocation(agent, _user(), degrade=True)

        assert plan.model_override is None
        assert plan.unavailable.model_id == "us.anthropic.claude-opus-4-7"

    @pytest.mark.asyncio
    async def test_skills_are_filtered_and_none_left_means_no_skill_binding(self, monkeypatch):
        _patch_skill_access(monkeypatch, {"rubric-writer"})
        agent = _assistant(bindings=[_skill_binding("rubric-writer"), _skill_binding("grant-budget")])
        plan = await resolve_agent_invocation(agent, _user(), degrade=True)
        assert plan.skills.skill_ids == ["rubric-writer"]
        assert plan.unavailable.skills == ["grant-budget"]

        _patch_skill_access(monkeypatch, set())
        plan = await resolve_agent_invocation(_assistant(bindings=[_skill_binding("grant-budget")]), _user(), degrade=True)
        assert plan.skills is None

    @pytest.mark.asyncio
    async def test_skills_disabled_drops_them_all(self, monkeypatch):
        _patch_skill_access(monkeypatch, True, enabled=False)
        plan = await resolve_agent_invocation(
            _assistant(bindings=[_skill_binding("a"), _skill_binding("b")]), _user(), degrade=True
        )
        assert plan.skills is None and plan.unavailable.skills == ["a", "b"]

    @pytest.mark.asyncio
    async def test_an_unreachable_memory_space_is_skipped(self, monkeypatch):
        from types import SimpleNamespace

        _patch_memory(monkeypatch, space=SimpleNamespace(name="Team notes", is_project_space=False), role="viewer")
        plan = await resolve_agent_invocation(
            _assistant(bindings=[_mem_binding(access="readwrite")]), _user(), degrade=True
        )
        assert plan.memory is None and plan.unavailable.memory == "Team notes"

    @pytest.mark.asyncio
    async def test_nothing_missing_means_no_notice(self, monkeypatch):
        _patch_tool_access(monkeypatch, True)
        plan = await resolve_agent_invocation(_assistant(bindings=[_tool_binding("sis")]), _user(), degrade=True)
        assert not plan.unavailable

    @pytest.mark.asyncio
    async def test_ordinary_agents_still_block(self, monkeypatch):
        _patch_tool_access(monkeypatch, False)
        with pytest.raises(AgentBindingBlockedError):
            await resolve_agent_invocation(_assistant(bindings=[_tool_binding("sis")]), _user())


class TestNotice:
    @pytest.mark.asyncio
    async def test_the_event_names_everything_left_out(self, monkeypatch):
        _patch_access(monkeypatch, False)
        _patch_tool_access(monkeypatch, set())
        # _patch_tool_access replaced the service; re-deny the model on the same object.
        from apis.inference_api.chat import agent_binding_resolver as resolver

        resolver.get_app_role_service().can_access_model = AsyncMock(return_value=False)
        agent = _assistant(
            model_settings=AgentModelConfig(model_id="model-x"),
            bindings=[_tool_binding("sis")],
        )
        plan = await resolve_agent_invocation(agent, _user(), degrade=True)

        event = AgentNoticeEvent.from_unavailable(
            plan.unavailable, session_id="s1", agent_id="ast-1", project_id="prj_1"
        )
        sse = event.to_sse_format()
        assert sse.startswith("event: agent_notice\ndata: ")
        payload = event.model_dump(by_alias=True, exclude_none=True)
        assert payload["type"] == "agent_notice"
        assert (payload["sessionId"], payload["agentId"], payload["projectId"]) == ("s1", "ast-1", "prj_1")
        assert payload["unavailableModelId"] == "model-x"
        assert payload["unavailableTools"] == ["sis"]
        assert "model-x" in payload["message"] and "sis" in payload["message"]
        assert "default model" in payload["message"]


# ── prompt ──────────────────────────────────────────────────────────────


class TestPrompt:
    def test_project_heading_and_unchanged_agent_heading(self):
        assert compose_agent_system_prompt("BASE", "Do X.", project_harness=True) == (
            "BASE\n\n## Project Instructions\n\nDo X."
        )
        # Every existing agent's prefix is byte-for-byte what it was before projects.
        assert compose_agent_system_prompt("BASE", "Do X.", project_harness=False) == (
            "BASE\n\n## Assistant-Specific Instructions\n\nDo X."
        )

    def test_two_members_render_the_same_prefix(self):
        """Nothing about the invoker can reach this text — the signature takes no user —
        so the Bedrock prefix cache is shared across a project's members."""
        import inspect

        params = inspect.signature(compose_agent_system_prompt).parameters
        assert set(params) == {"base_prompt", "instructions", "project_harness"}
        first = compose_agent_system_prompt("BASE", "Use the SIS glossary.", project_harness=True)
        second = compose_agent_system_prompt("BASE", "Use the SIS glossary.", project_harness=True)
        assert first == second


# ── archived / missing projects ─────────────────────────────────────────


@pytest.fixture()
def projects_repo(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "DYNAMODB_PROJECTS_TABLE_NAME": PROJECTS_TABLE,
    }.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        boto3.client("dynamodb").create_table(
            TableName=PROJECTS_TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield ProjectRepository()


def _project(status: str = "active") -> Project:
    return Project(
        project_id="prj_1", name="Enrollment Sync", owner_id="u-owner", owner_email="o@x.edu",
        harness_agent_id="ast-1", status=status, created_at="t", updated_at="t",
    )


class TestRefusal:
    def test_active_project_runs(self, projects_repo):
        projects_repo.create_project(_project())
        assert asyncio.run(_project_turn_refusal("prj_1")) is None

    def test_archived_project_refuses_with_its_name(self, projects_repo):
        projects_repo.create_project(_project(status="archived"))
        refusal = asyncio.run(_project_turn_refusal("prj_1"))
        assert "Enrollment Sync" in refusal and "archived" in refusal

    def test_missing_project_refuses(self, projects_repo):
        assert "no longer exists" in asyncio.run(_project_turn_refusal("prj_gone"))
        assert asyncio.run(_project_turn_refusal(None)) is not None


# ── cost ────────────────────────────────────────────────────────────────


def _metadata(project_id=None, cost=0.25) -> MessageMetadata:
    extra = {"projectId": project_id} if project_id else {}
    return MessageMetadata(
        token_usage=TokenUsage(input_tokens=1000, output_tokens=200, total_tokens=1200),
        cost={"total": cost, "inputCost": cost},
        **extra,
    )


class TestCost:
    def test_a_project_call_adds_to_the_month_and_the_members_share(self, projects_repo):
        for _ in range(2):
            asyncio.run(_update_project_rollup_async("u-ed", "2026-09-23T10:00:00+00:00", _metadata("prj_1")))
        asyncio.run(_update_project_rollup_async("u-vi", "2026-09-24T10:00:00+00:00", _metadata("prj_1", 0.5)))

        table = boto3.resource("dynamodb").Table(PROJECTS_TABLE)
        month = table.get_item(Key={"PK": "PROJECT#prj_1", "SK": "COST#2026-09"})["Item"]
        assert (float(month["totalCost"]), int(month["calls"]), int(month["inputTokens"])) == (1.0, 3, 3000)
        ed = table.get_item(Key={"PK": "PROJECT#prj_1", "SK": "COST#2026-09#USER#u-ed"})["Item"]
        assert (float(ed["totalCost"]), int(ed["calls"]), ed["userId"]) == (0.5, 2, "u-ed")

    def test_a_call_outside_any_project_writes_nothing(self, projects_repo):
        asyncio.run(_update_project_rollup_async("u-ed", "2026-09-23T10:00:00+00:00", _metadata()))
        items = boto3.resource("dynamodb").Table(PROJECTS_TABLE).scan()["Items"]
        assert items == []

    def test_a_failed_rollup_never_raises(self, projects_repo, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("throttled")

        monkeypatch.setattr(ProjectRepository, "add_call_cost", boom)
        asyncio.run(_update_project_rollup_async("u-ed", "2026-09-23T10:00:00+00:00", _metadata("prj_1")))

    @pytest.mark.asyncio
    async def test_the_cost_row_carries_the_project(self):
        from agents.main_agent.streaming.stream_coordinator import StreamCoordinator

        store = AsyncMock()
        with patch("apis.shared.sessions.metadata.store_message_metadata", store):
            await object.__new__(StreamCoordinator)._store_message_metadata(
                session_id="s1",
                user_id="u1",
                message_id=3,
                accumulated_metadata={"usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120}},
                stream_start_time=0.0,
                stream_end_time=1.0,
                first_token_time=0.5,
                agent=None,
                call_index=0,
                turn_agent_id="ast-1",
                turn_project_id="prj_1",
            )
        stored = store.await_args.kwargs["message_metadata"]
        assert (stored.model_extra["turnAgentId"], stored.model_extra["projectId"]) == ("ast-1", "prj_1")
