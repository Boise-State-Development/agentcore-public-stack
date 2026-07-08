"""Agent Designer Phase 3 (PR-A) — run-time model resolution + D5 block.

The resolver re-checks the Agent's modelConfig against the INVOKING user, reusing the
harness's AppRoleService.can_access_model gate (mocked here). Verifies: pinned+allowed →
model_override; pinned+denied → block; no modelConfig → empty plan (today's behavior).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from apis.inference_api.chat.agent_binding_resolver import (
    AgentBindingBlockedError,
    resolve_agent_invocation,
)
from apis.shared.assistants.models import AgentModelConfig, Assistant
from apis.shared.auth.models import User

MODULE = "apis.inference_api.chat.agent_binding_resolver"


def _user() -> User:
    return User(email="bob@x.edu", user_id="u-bob", name="Bob", roles=[])


def _assistant(model_settings=None) -> Assistant:
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
    )


def _patch_access(monkeypatch, allowed: bool) -> MagicMock:
    svc = MagicMock()
    svc.can_access_model = AsyncMock(return_value=allowed)
    monkeypatch.setattr(f"{MODULE}.get_app_role_service", lambda: svc)
    return svc


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
