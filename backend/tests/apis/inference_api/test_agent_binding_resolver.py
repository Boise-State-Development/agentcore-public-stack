"""Agent Designer Phase 3 (PR-A) — run-time model resolution + D5 block.

The resolver re-checks the Agent's modelConfig against the INVOKING user, reusing the
harness's AppRoleService.can_access_model gate (mocked here). Verifies: pinned+allowed →
model_override; pinned+denied → block; no modelConfig → empty plan (today's behavior).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from apis.inference_api.chat.agent_binding_resolver import (
    AgentBindingBlockedError,
    resolve_agent_invocation,
)
from apis.shared.assistants.models import AgentBinding, AgentModelConfig, Assistant
from apis.shared.auth.models import User

MODULE = "apis.inference_api.chat.agent_binding_resolver"


def _user() -> User:
    return User(email="bob@x.edu", user_id="u-bob", name="Bob", roles=[])


def _assistant(model_settings=None, bindings=None) -> Assistant:
    return Assistant(
        assistantId="ast-1",
        ownerId="u-alice",
        ownerName="Alice",
        name="Oliver",
        description="d",
        instructions="i",
        vectorIndexId="idx",
        visibility="SHARED",
        createdAt="t",
        updatedAt="t",
        status="COMPLETE",
        model_settings=model_settings,
        bindings=bindings,
    )


def _patch_memory(monkeypatch, *, enabled=True, space=None, role=None):
    monkeypatch.setattr(f"{MODULE}.memory_spaces_enabled", lambda: enabled)
    svc = MagicMock()
    svc.resolve_permission = MagicMock(return_value=(space, role))
    monkeypatch.setattr(f"{MODULE}.MemorySpaceService", lambda: svc)
    return svc


def _mem_binding(access="read", ref="spc_1", always_load=None):
    config = {"access": access}
    if always_load is not None:
        config["alwaysLoad"] = always_load
    return AgentBinding(kind="memory_space", ref=ref, config=config)


def _patch_access(monkeypatch, allowed: bool) -> MagicMock:
    svc = MagicMock()
    svc.can_access_model = AsyncMock(return_value=allowed)
    monkeypatch.setattr(f"{MODULE}.get_app_role_service", lambda: svc)
    return svc


def _patch_tool_access(monkeypatch, allowed) -> MagicMock:
    """Patch the AppRole gate for tool resolution. ``allowed`` is a bool (uniform answer)
    or a set of tool ids the invoker may access."""
    svc = MagicMock()
    if isinstance(allowed, bool):
        svc.can_access_tool = AsyncMock(return_value=allowed)
    else:
        svc.can_access_tool = AsyncMock(side_effect=lambda user, tid: tid in allowed)
    monkeypatch.setattr(f"{MODULE}.get_app_role_service", lambda: svc)
    return svc


def _tool_binding(ref: str) -> AgentBinding:
    return AgentBinding(kind="tool", ref=ref)


def _patch_skill_access(monkeypatch, allowed, *, enabled=True) -> MagicMock:
    """Patch the skills flag + AppRole gate for skill resolution. ``allowed`` is a bool or a
    set of skill ids the invoker may access."""
    monkeypatch.setattr(f"{MODULE}.skills_enabled", lambda: enabled)
    svc = MagicMock()
    if isinstance(allowed, bool):
        svc.can_access_skill = AsyncMock(return_value=allowed)
    else:
        svc.can_access_skill = AsyncMock(side_effect=lambda user, sid: sid in allowed)
    monkeypatch.setattr(f"{MODULE}.get_app_role_service", lambda: svc)
    return svc


def _skill_binding(ref: str) -> AgentBinding:
    return AgentBinding(kind="skill", ref=ref)


class TestModelResolution:
    @pytest.mark.asyncio
    async def test_no_modelconfig_is_empty_plan(self, monkeypatch):
        svc = _patch_access(monkeypatch, True)
        plan = await resolve_agent_invocation(_assistant(model_settings=None), _user())
        assert plan.model_override is None
        # No modelConfig ⇒ we must not even consult model RBAC (today's behavior).
        svc.can_access_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allowed_model_sets_override(self, monkeypatch):
        _patch_access(monkeypatch, True)
        cfg = AgentModelConfig(model_id="us.anthropic.opus", provider="bedrock", params={"temperature": 0.5})
        plan = await resolve_agent_invocation(_assistant(model_settings=cfg), _user())
        assert plan.model_override.model_id == "us.anthropic.opus"
        assert plan.model_override.provider == "bedrock"
        assert plan.model_override.params == {"temperature": 0.5}

    @pytest.mark.asyncio
    async def test_denied_model_blocks_with_message(self, monkeypatch):
        svc = _patch_access(monkeypatch, False)
        cfg = AgentModelConfig(model_id="us.anthropic.opus")
        with pytest.raises(AgentBindingBlockedError) as ei:
            await resolve_agent_invocation(_assistant(model_settings=cfg), _user())
        # The block message names the model and is invoker-facing markdown (D5).
        assert "us.anthropic.opus" in ei.value.message
        # Checked against the INVOKING user, not the author.
        assert svc.can_access_model.await_args.args[0].user_id == "u-bob"

    @pytest.mark.asyncio
    async def test_legacy_assistant_never_blocks(self, monkeypatch):
        # A legacy row (no model_settings) resolves to an empty plan regardless.
        _patch_access(monkeypatch, False)
        plan = await resolve_agent_invocation(_assistant(model_settings=None), _user())
        assert plan.model_override is None


class TestMemoryResolution:
    _SPACE = SimpleNamespace(name="Oliver's Brain", space_id="spc_1")

    @pytest.mark.asyncio
    async def test_no_binding_is_none(self, monkeypatch):
        svc = _patch_memory(monkeypatch)
        plan = await resolve_agent_invocation(_assistant(bindings=[]), _user())
        assert plan.memory is None
        svc.resolve_permission.assert_not_called()

    @pytest.mark.asyncio
    async def test_flag_off_blocks(self, monkeypatch):
        _patch_memory(monkeypatch, enabled=False)
        with pytest.raises(AgentBindingBlockedError):
            await resolve_agent_invocation(_assistant(bindings=[_mem_binding()]), _user())

    @pytest.mark.asyncio
    async def test_missing_space_blocks(self, monkeypatch):
        _patch_memory(monkeypatch, space=None, role=None)
        with pytest.raises(AgentBindingBlockedError) as ei:
            await resolve_agent_invocation(_assistant(bindings=[_mem_binding()]), _user())
        assert "no longer exists" in ei.value.message

    @pytest.mark.asyncio
    async def test_read_viewer_resolves(self, monkeypatch):
        _patch_memory(monkeypatch, space=self._SPACE, role="viewer")
        plan = await resolve_agent_invocation(
            _assistant(bindings=[_mem_binding(access="read", always_load=["MEMORY.md"])]), _user()
        )
        assert plan.memory.space_id == "spc_1"
        assert plan.memory.space_name == "Oliver's Brain"
        assert plan.memory.access == "read" and plan.memory.role == "viewer"
        assert plan.memory.always_load == ["MEMORY.md"]

    @pytest.mark.asyncio
    async def test_readwrite_requires_editor(self, monkeypatch):
        _patch_memory(monkeypatch, space=self._SPACE, role="viewer")
        with pytest.raises(AgentBindingBlockedError) as ei:
            await resolve_agent_invocation(
                _assistant(bindings=[_mem_binding(access="readwrite")]), _user()
            )
        assert "editor" in ei.value.message

    @pytest.mark.asyncio
    async def test_readwrite_editor_resolves(self, monkeypatch):
        _patch_memory(monkeypatch, space=self._SPACE, role="editor")
        plan = await resolve_agent_invocation(
            _assistant(bindings=[_mem_binding(access="readwrite")]), _user()
        )
        assert plan.memory.access == "readwrite" and plan.memory.role == "editor"

    @pytest.mark.asyncio
    async def test_permission_checked_against_invoker(self, monkeypatch):
        svc = _patch_memory(monkeypatch, space=self._SPACE, role="viewer")
        await resolve_agent_invocation(_assistant(bindings=[_mem_binding()]), _user())
        # resolve_permission(space_id, user_id, user_email) — invoker's identity.
        args = svc.resolve_permission.call_args.args
        assert args[0] == "spc_1" and args[1] == "u-bob" and args[2] == "bob@x.edu"


class TestToolResolution:
    @pytest.mark.asyncio
    async def test_no_tool_binding_is_none(self, monkeypatch):
        # No tool binding ⇒ plan.tools is None (request's enabled_tools stay in force) and
        # we never even consult tool RBAC — the service is fetched lazily.
        svc = _patch_tool_access(monkeypatch, True)
        plan = await resolve_agent_invocation(_assistant(bindings=[]), _user())
        assert plan.tools is None
        svc.can_access_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_accessible_tools_become_override(self, monkeypatch):
        _patch_tool_access(monkeypatch, True)
        plan = await resolve_agent_invocation(
            _assistant(bindings=[_tool_binding("web_search"), _tool_binding("calculator")]),
            _user(),
        )
        assert plan.tools is not None
        assert plan.tools.tool_ids == ["web_search", "calculator"]

    @pytest.mark.asyncio
    async def test_empty_tool_ids_distinct_from_none(self, monkeypatch):
        # An Agent may bind tools but none of the other kinds — the resolved list drives the
        # turn (replace). (A deliberately-empty toolset is expressed by binding no tools =>
        # None; a non-empty binding list always yields a non-None ResolvedTools.)
        _patch_tool_access(monkeypatch, True)
        plan = await resolve_agent_invocation(
            _assistant(bindings=[_tool_binding("web_search")]), _user()
        )
        assert plan.tools is not None and plan.tools.tool_ids == ["web_search"]

    @pytest.mark.asyncio
    async def test_duplicate_refs_deduped(self, monkeypatch):
        _patch_tool_access(monkeypatch, True)
        plan = await resolve_agent_invocation(
            _assistant(bindings=[_tool_binding("web_search"), _tool_binding("web_search")]),
            _user(),
        )
        assert plan.tools.tool_ids == ["web_search"]

    @pytest.mark.asyncio
    async def test_missing_tool_blocks_with_message(self, monkeypatch):
        # Invoker has calculator but not web_search ⇒ block naming the missing tool (D5).
        _patch_tool_access(monkeypatch, {"calculator"})
        with pytest.raises(AgentBindingBlockedError) as ei:
            await resolve_agent_invocation(
                _assistant(bindings=[_tool_binding("web_search"), _tool_binding("calculator")]),
                _user(),
            )
        assert "web_search" in ei.value.message

    @pytest.mark.asyncio
    async def test_tool_access_checked_against_invoker(self, monkeypatch):
        svc = _patch_tool_access(monkeypatch, True)
        await resolve_agent_invocation(_assistant(bindings=[_tool_binding("web_search")]), _user())
        # can_access_tool(invoker, tool_id) — the INVOKING user, not the author.
        args = svc.can_access_tool.await_args.args
        assert args[0].user_id == "u-bob" and args[1] == "web_search"


class TestSkillResolution:
    @pytest.mark.asyncio
    async def test_no_skill_binding_is_none(self, monkeypatch):
        # No skill binding ⇒ plan.skills is None and we never consult the flag or RBAC.
        svc = _patch_skill_access(monkeypatch, True)
        plan = await resolve_agent_invocation(_assistant(bindings=[]), _user())
        assert plan.skills is None
        svc.can_access_skill.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_accessible_skills_become_override(self, monkeypatch):
        _patch_skill_access(monkeypatch, True)
        plan = await resolve_agent_invocation(
            _assistant(bindings=[_skill_binding("research"), _skill_binding("writing")]), _user()
        )
        assert plan.skills is not None
        assert plan.skills.skill_ids == ["research", "writing"]

    @pytest.mark.asyncio
    async def test_duplicate_refs_deduped(self, monkeypatch):
        _patch_skill_access(monkeypatch, True)
        plan = await resolve_agent_invocation(
            _assistant(bindings=[_skill_binding("research"), _skill_binding("research")]), _user()
        )
        assert plan.skills.skill_ids == ["research"]

    @pytest.mark.asyncio
    async def test_flag_off_blocks(self, monkeypatch):
        # Skills disabled in this environment but the Agent binds one ⇒ block (env drift, D5).
        _patch_skill_access(monkeypatch, True, enabled=False)
        with pytest.raises(AgentBindingBlockedError) as ei:
            await resolve_agent_invocation(_assistant(bindings=[_skill_binding("research")]), _user())
        assert "enabled" in ei.value.message

    @pytest.mark.asyncio
    async def test_missing_skill_blocks_with_message(self, monkeypatch):
        _patch_skill_access(monkeypatch, {"writing"})
        with pytest.raises(AgentBindingBlockedError) as ei:
            await resolve_agent_invocation(
                _assistant(bindings=[_skill_binding("research"), _skill_binding("writing")]), _user()
            )
        assert "research" in ei.value.message

    @pytest.mark.asyncio
    async def test_skill_access_checked_against_invoker(self, monkeypatch):
        svc = _patch_skill_access(monkeypatch, True)
        await resolve_agent_invocation(_assistant(bindings=[_skill_binding("research")]), _user())
        # can_access_skill(invoker, skill_id) — the INVOKING user, not the author.
        args = svc.can_access_skill.await_args.args
        assert args[0].user_id == "u-bob" and args[1] == "research"
