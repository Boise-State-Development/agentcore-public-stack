"""Agent Designer Phase 2 — the bindable-primitives catalog (D4).

Unit tests for ``list_bindable``: each primitive's list/access service is mocked so
these stay fast. Asserts the uniform ``BindableItem`` projection per kind, that ``ref``
carries the correct identifier (Bedrock model_id for models — not the internal UUID),
the feature-flag gating for skills/memory_space, the welded-KB empty result, and the
best-effort degradation on a sub-service failure.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from apis.app_api.agent_designer.services import bindable_catalog as bc
from apis.shared.auth.models import User
from apis.shared.tools.models import ToolStatus

MODULE = "apis.app_api.agent_designer.services.bindable_catalog"


def _user() -> User:
    return User(email="alice@x.edu", user_id="u1", name="Alice", roles=[])


def _model(**kw):
    base = dict(
        model_id="us.anthropic.claude", model_name="Claude", provider="bedrock",
        provider_name="Bedrock", is_default=True, max_input_tokens=200000,
        max_output_tokens=8192, supports_caching=True, input_modalities=["text"],
        output_modalities=["text"], supported_params=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _model_svc(models):
    svc = MagicMock()
    svc.filter_accessible_models = AsyncMock(return_value=models)
    return svc


# --------------------------------------------------------------------------- model
class TestModels:
    @pytest.mark.asyncio
    async def test_model_ref_is_bedrock_model_id(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.list_all_managed_models", AsyncMock(return_value=[_model()]))
        items = await bc.list_bindable("model", _user(), model_access_service=_model_svc([_model()]))
        assert len(items) == 1
        it = items[0]
        assert it.kind == "model"
        assert it.ref == "us.anthropic.claude"  # NOT the internal UUID id
        assert it.label == "Claude"
        assert it.meta["provider"] == "bedrock"
        assert it.meta["isDefault"] is True

    @pytest.mark.asyncio
    async def test_model_service_failure_degrades_to_empty(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.list_all_managed_models", AsyncMock(side_effect=RuntimeError("boom")))
        items = await bc.list_bindable("model", _user(), model_access_service=_model_svc([]))
        assert items == []


# --------------------------------------------------------------------------- tool
def _tool(**kw):
    base = dict(
        tool_id="wikipedia", display_name="Wikipedia", description="Search Wikipedia",
        category="research", protocol="mcp", status=ToolStatus.ACTIVE,
        retirement_note=None, retires_on=None,
        requires_oauth_provider=None,
        server_tools=[SimpleNamespace(name="search", description="d", needs_approval=False, enabled=True)],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _tool_svc(tools):
    svc = MagicMock()
    svc.get_user_accessible_tools = AsyncMock(return_value=tools)
    return svc


class TestTools:
    @pytest.mark.asyncio
    async def test_tool_projection_with_server_tools(self):
        items = await bc.list_bindable("tool", _user(), tool_service=_tool_svc([_tool()]))
        assert items[0].ref == "wikipedia"
        assert items[0].meta["serverTools"][0]["name"] == "search"

    @pytest.mark.asyncio
    async def test_status_rides_on_meta(self):
        """The Designer picker's only signal that a tool is being retired.

        Without it the palette cannot tell a retiring tool from a live one, and
        §7 of docs/specs/mcp-server-retirement.md has nothing to gate on.
        """
        items = await bc.list_bindable(
            "tool", _user(), tool_service=_tool_svc([_tool(status=ToolStatus.DEPRECATED)])
        )
        assert items[0].meta["status"] == "deprecated"

    @pytest.mark.asyncio
    async def test_status_serializes_as_a_plain_string(self):
        """The SPA compares it to `'active'`, so an enum member on the wire would
        make every tool read as retiring."""
        items = await bc.list_bindable("tool", _user(), tool_service=_tool_svc([_tool()]))
        assert items[0].model_dump(mode="json")["meta"]["status"] == "active"

    @pytest.mark.asyncio
    async def test_retirement_metadata_rides_on_meta(self):
        """Without these the Designer notice can say a tool is going away but not
        what to use instead — which is the question an author actually has."""
        items = await bc.list_bindable(
            "tool",
            _user(),
            tool_service=_tool_svc([
                _tool(
                    status=ToolStatus.DEPRECATED,
                    retirement_note="Replaced by Canvas for Faculty",
                    retires_on="2026-10-31",
                )
            ]),
        )
        assert items[0].meta["retirementNote"] == "Replaced by Canvas for Faculty"
        assert items[0].meta["retiresOn"] == "2026-10-31"

    @pytest.mark.asyncio
    async def test_retirement_metadata_is_none_on_an_ordinary_tool(self):
        items = await bc.list_bindable("tool", _user(), tool_service=_tool_svc([_tool()]))
        assert items[0].meta["retirementNote"] is None
        assert items[0].meta["retiresOn"] is None

    @pytest.mark.asyncio
    async def test_a_retiring_tool_is_still_listed(self):
        """Retirement hides a tool from NEW selection, in the SPA. It must not be
        filtered out here: ``binding_validation._validate_tool`` reads the same
        ``get_user_accessible_tools``, so dropping it would 403 an author editing
        an Agent that keeps the binding — for a tool that Agent can still run.
        """
        items = await bc.list_bindable(
            "tool",
            _user(),
            tool_service=_tool_svc([
                _tool(tool_id="live"),
                _tool(tool_id="retiring", status=ToolStatus.DEPRECATED),
                _tool(tool_id="gone", status=ToolStatus.DISABLED),
            ]),
        )
        assert [i.ref for i in items] == ["live", "retiring", "gone"]


# --------------------------------------------------------------------------- skill
class TestSkills:
    @pytest.mark.asyncio
    async def test_skills_empty_when_flag_off(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.skills_enabled", lambda: False)
        items = await bc.list_bindable("skill", _user())
        assert items == []

    @pytest.mark.asyncio
    async def test_skills_hydrated_when_flag_on(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.skills_enabled", lambda: True)
        monkeypatch.setattr(f"{MODULE}.resolve_accessible_skill_ids", AsyncMock(return_value=["pdf"]))
        skill = SimpleNamespace(skill_id="pdf", display_name="PDF", description="PDF tools",
                                compose=[])
        repo = MagicMock()
        repo.batch_get_skills = AsyncMock(return_value=[skill])
        monkeypatch.setattr(f"{MODULE}.get_skill_catalog_repository", lambda: repo)
        items = await bc.list_bindable("skill", _user())
        assert items[0].ref == "pdf"
        assert items[0].meta["compose"] == []


# --------------------------------------------------------------------------- kb
class TestKnowledgeBase:
    @pytest.mark.asyncio
    async def test_kb_always_empty(self):
        # Welded to the agent, synthesized on read, never author-settable.
        assert await bc.list_bindable("knowledge_base", _user()) == []


# --------------------------------------------------------------------------- memory
class TestMemorySpaces:
    @pytest.mark.asyncio
    async def test_empty_when_flag_off(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.memory_spaces_enabled", lambda: False)
        items = await bc.list_bindable("memory_space", _user(), memory_service=MagicMock())
        assert items == []

    @pytest.mark.asyncio
    async def test_projection_when_flag_on(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.memory_spaces_enabled", lambda: True)
        space = SimpleNamespace(space_id="spc_1", name="Oliver", template="chief-of-staff", owner_id="u1")
        svc = MagicMock()
        svc.list_spaces_for_user = MagicMock(return_value=[(space, "owner")])
        items = await bc.list_bindable("memory_space", _user(), memory_service=svc)
        assert items[0].ref == "spc_1"
        assert items[0].label == "Oliver"
        assert items[0].meta["role"] == "owner"


# --------------------------------------------------------------------------- misc
class TestUnknownKind:
    @pytest.mark.asyncio
    async def test_unknown_kind_raises(self):
        with pytest.raises(ValueError):
            await bc.list_bindable("nonsense", _user())
