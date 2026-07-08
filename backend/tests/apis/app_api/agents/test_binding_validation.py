"""Agent Designer Phase 1 — design-time binding/model validation (D4/D5).

Composes existing per-primitive access checks; the primitive services are mocked so
these stay fast unit tests. Asserts the inert guarantee for tool/skill (no RBAC/catalog
call is made), the memory_space grant matrix, and the implicit-KB rejection.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from apis.app_api.agents.services.binding_validation import (
    BindingValidationError,
    validate_agent_write,
)
from apis.shared.assistants.models import AgentBinding, AgentModelConfig
from apis.shared.auth.models import User

MODULE = "apis.app_api.agents.services.binding_validation"


def _user() -> User:
    return User(email="alice@x.edu", user_id="u1", name="Alice", roles=[])


def _model_svc(allowed: bool) -> MagicMock:
    svc = MagicMock()
    svc.can_access_model = AsyncMock(return_value=allowed)
    return svc


def _mem_svc(space, role) -> MagicMock:
    svc = MagicMock()
    svc.resolve_permission = MagicMock(return_value=(space, role))
    return svc


# --------------------------------------------------------------------------- model
class TestModelValidation:
    @pytest.mark.asyncio
    async def test_accessible_model_passes(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.get_managed_model", AsyncMock(return_value=SimpleNamespace(model_id="m1")))
        await validate_agent_write(
            _user(),
            model_settings=AgentModelConfig(model_id="m1"),
            model_access_service=_model_svc(True),
        )

    @pytest.mark.asyncio
    async def test_unknown_model_400(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.get_managed_model", AsyncMock(return_value=None))
        with pytest.raises(BindingValidationError) as ei:
            await validate_agent_write(
                _user(), model_settings=AgentModelConfig(model_id="ghost"), model_access_service=_model_svc(True)
            )
        assert ei.value.status_code == 400

    @pytest.mark.asyncio
    async def test_forbidden_model_403(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.get_managed_model", AsyncMock(return_value=SimpleNamespace(model_id="m1")))
        with pytest.raises(BindingValidationError) as ei:
            await validate_agent_write(
                _user(), model_settings=AgentModelConfig(model_id="m1"), model_access_service=_model_svc(False)
            )
        assert ei.value.status_code == 403


# --------------------------------------------------------------------------- inert
class TestInertKinds:
    @pytest.mark.asyncio
    async def test_tool_and_skill_stored_without_rbac(self):
        # The inert guarantee: no memory/model service is consulted for tool/skill.
        mem = _mem_svc(space=None, role=None)
        await validate_agent_write(
            _user(),
            bindings=[
                AgentBinding(kind="tool", ref="gateway_x", config={"enabledTools": []}),
                AgentBinding(kind="skill", ref="skill_1"),
            ],
            memory_service=mem,
        )
        mem.resolve_permission.assert_not_called()

    @pytest.mark.asyncio
    async def test_inert_kind_requires_ref(self):
        with pytest.raises(BindingValidationError) as ei:
            await validate_agent_write(_user(), bindings=[AgentBinding(kind="tool", ref="  ")])
        assert ei.value.status_code == 400

    @pytest.mark.asyncio
    async def test_unknown_kind_rejected(self):
        with pytest.raises(BindingValidationError) as ei:
            await validate_agent_write(_user(), bindings=[AgentBinding(kind="bogus", ref="x")])
        assert ei.value.status_code == 400


# --------------------------------------------------------------------------- KB
class TestKnowledgeBase:
    @pytest.mark.asyncio
    async def test_explicit_kb_binding_rejected(self):
        # Phase 1: KB is managed implicitly (synthesized on read), not author-settable.
        with pytest.raises(BindingValidationError) as ei:
            await validate_agent_write(_user(), bindings=[AgentBinding(kind="knowledge_base", ref="ast_1")])
        assert ei.value.status_code == 400


# --------------------------------------------------------------------------- memory_space
class TestMemorySpace:
    @pytest.fixture(autouse=True)
    def _flag_on(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.memory_spaces_enabled", lambda: True)

    @pytest.mark.asyncio
    async def test_flag_off_400(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.memory_spaces_enabled", lambda: False)
        with pytest.raises(BindingValidationError) as ei:
            await validate_agent_write(
                _user(),
                bindings=[AgentBinding(kind="memory_space", ref="spc_1", config={"access": "read"})],
                memory_service=_mem_svc(space=object(), role="viewer"),
            )
        assert ei.value.status_code == 400

    @pytest.mark.asyncio
    async def test_readwrite_requires_editor(self):
        with pytest.raises(BindingValidationError) as ei:
            await validate_agent_write(
                _user(),
                bindings=[AgentBinding(kind="memory_space", ref="spc_1", config={"access": "readwrite"})],
                memory_service=_mem_svc(space=object(), role="viewer"),
            )
        assert ei.value.status_code == 403

    @pytest.mark.asyncio
    async def test_readwrite_editor_ok(self):
        await validate_agent_write(
            _user(),
            bindings=[AgentBinding(kind="memory_space", ref="spc_1", config={"access": "readwrite"})],
            memory_service=_mem_svc(space=object(), role="editor"),
        )

    @pytest.mark.asyncio
    async def test_read_viewer_ok(self):
        await validate_agent_write(
            _user(),
            bindings=[AgentBinding(kind="memory_space", ref="spc_1", config={"access": "read"})],
            memory_service=_mem_svc(space=object(), role="viewer"),
        )

    @pytest.mark.asyncio
    async def test_no_grant_403(self):
        with pytest.raises(BindingValidationError) as ei:
            await validate_agent_write(
                _user(),
                bindings=[AgentBinding(kind="memory_space", ref="spc_1", config={"access": "read"})],
                memory_service=_mem_svc(space=object(), role=None),
            )
        assert ei.value.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_space_400(self):
        with pytest.raises(BindingValidationError) as ei:
            await validate_agent_write(
                _user(),
                bindings=[AgentBinding(kind="memory_space", ref="ghost", config={"access": "read"})],
                memory_service=_mem_svc(space=None, role=None),
            )
        assert ei.value.status_code == 400

    @pytest.mark.asyncio
    async def test_bad_access_value_400(self):
        with pytest.raises(BindingValidationError) as ei:
            await validate_agent_write(
                _user(),
                bindings=[AgentBinding(kind="memory_space", ref="spc_1", config={"access": "admin"})],
                memory_service=_mem_svc(space=object(), role="owner"),
            )
        assert ei.value.status_code == 400

    @pytest.mark.asyncio
    async def test_bad_alwaysload_400(self):
        with pytest.raises(BindingValidationError) as ei:
            await validate_agent_write(
                _user(),
                bindings=[
                    AgentBinding(kind="memory_space", ref="spc_1", config={"access": "read", "alwaysLoad": "nope"})
                ],
                memory_service=_mem_svc(space=object(), role="viewer"),
            )
        assert ei.value.status_code == 400
