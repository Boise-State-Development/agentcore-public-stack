"""Tests for the `save_conversation` agent tool, its route gating, and the
OAuth consent-gate wiring that lets a direct (non-MCP) tool surface
`oauth_required`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import List, Optional

import pytest
from unittest.mock import AsyncMock, MagicMock

from agents.builtin_tools.save_conversation import make_save_conversation_tool
from agents.builtin_tools.save_conversation import tools as sc_tools
from agents.main_agent.base_agent import BaseAgent
from apis.shared.export_targets.adapter import (
    ExportTargetAdapter,
    ExportTargetMetadata,
)
from apis.shared.export_targets.models import (
    CreatedFile,
    ExportFormat,
    ExportTargetError,
)
from apis.shared.oauth.models import OAuthProvider, OAuthProviderType


# --------------------------------------------------------------------------- #
# Fixtures / stubs
# --------------------------------------------------------------------------- #


class _StubAdapter(ExportTargetAdapter):
    """Records create_document calls; can be told to raise."""

    def __init__(self) -> None:
        self.created_with: Optional[SimpleNamespace] = None
        self.raise_error: Optional[ExportTargetError] = None

    @property
    def metadata(self) -> ExportTargetMetadata:
        return ExportTargetMetadata(
            key="stub-drive",
            display_name="Stub Drive",
            icon="stub",
            compatible_provider_types=(OAuthProviderType.GOOGLE,),
            required_scopes=(),
            supported_formats=(ExportFormat.GOOGLE_DOC, ExportFormat.MARKDOWN),
        )

    async def list_destinations(self, access_token):  # type: ignore[no-untyped-def]
        return []

    async def create_document(  # type: ignore[no-untyped-def]
        self, access_token, *, content, name, source_mime_type, target_format, parent_id=None
    ):
        if self.raise_error:
            raise self.raise_error
        self.created_with = SimpleNamespace(
            access_token=access_token,
            name=name,
            source_mime_type=source_mime_type,
            target_format=target_format,
            parent_id=parent_id,
        )
        return CreatedFile(file_id="f1", name=name, web_view_link="https://drive/f1")


def _provider(provider_id: str = "gdrive") -> OAuthProvider:
    return OAuthProvider(
        provider_id=provider_id,
        display_name="Google Drive",
        provider_type=OAuthProviderType.GOOGLE,
        scopes=["https://www.googleapis.com/auth/drive.file"],
        allowed_roles=[],
        enabled=True,
        export_target_adapter_id="google-drive",
    )


def _unwrap(tool):
    """Reach the raw async function behind a Strands @tool wrapper."""
    inner = getattr(tool, "_tool_func", None) or getattr(tool, "func", None) or tool
    return getattr(inner, "__wrapped__", inner)


def _patch_happy_path(monkeypatch, *, requires_consent=False, access_token="tok"):
    monkeypatch.setattr(
        sc_tools,
        "resolve_export_target_token",
        AsyncMock(
            return_value=SimpleNamespace(
                requires_consent=requires_consent, access_token=access_token
            )
        ),
    )
    monkeypatch.setattr(sc_tools, "collect_transcript", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        sc_tools,
        "get_session_metadata",
        AsyncMock(return_value=SimpleNamespace(title="My Chat")),
    )
    receipts: list = []
    monkeypatch.setattr(
        sc_tools,
        "add_export_receipt",
        AsyncMock(side_effect=lambda s, u, r: receipts.append(r)),
    )
    return receipts


# --------------------------------------------------------------------------- #
# Tool behavior
# --------------------------------------------------------------------------- #


class TestSaveConversationTool:
    @pytest.mark.asyncio
    async def test_success_creates_doc_and_records_receipt(self, monkeypatch):
        receipts = _patch_happy_path(monkeypatch)
        adapter = _StubAdapter()
        tool = make_save_conversation_tool("s1", "u1", _provider(), adapter)

        result = await _unwrap(tool)("google_doc")

        assert result["status"] == "success"
        assert "https://drive/f1" in result["content"][0]["text"]
        # Agent tool writes to the app default folder (no picker mid-chat).
        assert adapter.created_with.parent_id is None
        assert adapter.created_with.target_format == ExportFormat.GOOGLE_DOC
        assert len(receipts) == 1
        assert receipts[0].file_id == "f1"
        assert receipts[0].web_view_link == "https://drive/f1"

    @pytest.mark.asyncio
    async def test_rejects_unknown_format(self, monkeypatch):
        _patch_happy_path(monkeypatch)
        tool = make_save_conversation_tool("s1", "u1", _provider(), _StubAdapter())

        result = await _unwrap(tool)("docx")

        assert result["status"] == "error"
        assert "supported format" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_rejects_format_adapter_cannot_produce(self, monkeypatch):
        _patch_happy_path(monkeypatch)
        # 'pdf' is a valid ExportFormat but the stub adapter doesn't support it.
        tool = make_save_conversation_tool("s1", "u1", _provider(), _StubAdapter())

        result = await _unwrap(tool)("pdf")

        assert result["status"] == "error"
        assert "cannot export to 'pdf'" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_consent_required_returns_actionable_error(self, monkeypatch):
        _patch_happy_path(monkeypatch, requires_consent=True, access_token=None)
        tool = make_save_conversation_tool("s1", "u1", _provider(), _StubAdapter())

        result = await _unwrap(tool)("google_doc")

        assert result["status"] == "error"
        assert "Connect" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_adapter_error_surfaces_cleanly(self, monkeypatch):
        _patch_happy_path(monkeypatch)
        adapter = _StubAdapter()
        adapter.raise_error = ExportTargetError("boom")
        tool = make_save_conversation_tool("s1", "u1", _provider(), adapter)

        result = await _unwrap(tool)("google_doc")

        assert result["status"] == "error"
        assert "rejected" in result["content"][0]["text"]


# --------------------------------------------------------------------------- #
# Route gating: _build_save_conversation_tool
# --------------------------------------------------------------------------- #


def _route_mocks(monkeypatch, providers: List[OAuthProvider], roles: List[str]):
    from apis.inference_api.chat import routes
    import apis.shared.oauth.provider_repository as provider_repo_mod

    repo = MagicMock()
    repo.list_providers = AsyncMock(return_value=providers)
    monkeypatch.setattr(provider_repo_mod, "get_provider_repository", lambda: repo)

    role_service = MagicMock()
    role_service.resolve_user_permissions = AsyncMock(
        return_value=SimpleNamespace(app_roles=roles)
    )
    monkeypatch.setattr(routes, "get_app_role_service", lambda: role_service)
    return routes


class TestBuildSaveConversationTool:
    @pytest.mark.asyncio
    async def test_absent_when_not_enabled(self, monkeypatch):
        routes = _route_mocks(monkeypatch, [_provider()], [])
        tools, providers = await routes._build_save_conversation_tool(
            ["other"], "s1", "u1", _user()
        )
        assert tools == [] and providers == {}

    @pytest.mark.asyncio
    async def test_absent_when_no_export_target(self, monkeypatch):
        # Provider exists but isn't mapped as an export target.
        prov = _provider()
        prov.export_target_adapter_id = None
        routes = _route_mocks(monkeypatch, [prov], [])
        tools, providers = await routes._build_save_conversation_tool(
            ["save_conversation"], "s1", "u1", _user()
        )
        assert tools == [] and providers == {}

    @pytest.mark.asyncio
    async def test_built_when_enabled_with_visible_target(self, monkeypatch):
        routes = _route_mocks(monkeypatch, [_provider("gdrive")], [])
        tools, providers = await routes._build_save_conversation_tool(
            ["save_conversation"], "s1", "u1", _user()
        )
        assert len(tools) == 1
        assert providers == {"save_conversation": "gdrive"}

    @pytest.mark.asyncio
    async def test_multiple_targets_pick_first(self, monkeypatch):
        routes = _route_mocks(
            monkeypatch, [_provider("first"), _provider("second")], []
        )
        tools, providers = await routes._build_save_conversation_tool(
            ["save_conversation"], "s1", "u1", _user()
        )
        assert providers == {"save_conversation": "first"}


def _user():
    from apis.shared.auth.models import User

    return User(user_id="u1", email="u1@x.com", name="U", roles=[], raw_token="t")


# --------------------------------------------------------------------------- #
# Consent-gate wiring: tool_use_provider_lookup
# --------------------------------------------------------------------------- #


class TestToolUseProviderLookup:
    def test_base_maps_tool_name_to_provider(self):
        fake = SimpleNamespace(oauth_tool_providers={"save_conversation": "gdrive"})
        lookup = BaseAgent._build_tool_use_provider_lookup(fake)
        assert lookup is not None
        assert lookup({"name": "save_conversation"}) == "gdrive"
        assert lookup({"name": "other_tool"}) is None
        assert lookup({}) is None

    def test_base_returns_none_without_mappings(self):
        fake = SimpleNamespace(oauth_tool_providers={})
        assert BaseAgent._build_tool_use_provider_lookup(fake) is None

    def test_skill_agent_composes_with_base(self, monkeypatch):
        # The skill override must fall through to the base name→provider map so
        # save_conversation still gates under skill mode (folded lookup misses
        # a non-skill tool).
        from agents.main_agent import skill_agent
        from agents.main_agent.skills import mcp_binding
        from agents.main_agent.integrations import external_mcp_client

        monkeypatch.setattr(
            external_mcp_client,
            "get_external_mcp_integration",
            lambda: SimpleNamespace(provider_for_client=lambda c: None),
        )
        # Folded lookup never matches a direct tool.
        monkeypatch.setattr(
            mcp_binding,
            "make_folded_tool_provider_lookup",
            lambda registry, provider_for_client: (lambda tu: None),
        )

        inst = skill_agent.SkillAgent.__new__(skill_agent.SkillAgent)
        inst._registry = None
        inst.oauth_tool_providers = {"save_conversation": "gdrive"}

        lookup = inst._build_tool_use_provider_lookup()
        assert lookup({"name": "save_conversation"}) == "gdrive"
        assert lookup({"name": "skill_executor"}) is None
