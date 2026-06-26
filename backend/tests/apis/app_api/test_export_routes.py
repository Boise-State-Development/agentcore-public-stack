"""Route-level tests for the user-facing export-target endpoints.

Covers the catalog (`GET /export-targets`) and the save action
(`POST /sessions/{id}/export`). External boundaries — the provider repository,
role service, disconnect repository, AgentCore identity client, the adapter
registry, transcript retrieval, and receipt persistence — are stubbed; we test
our gating, ordering, error mapping, and response shape, not the downstream
provider calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.export_targets import routes, service
from apis.app_api.export_targets.adapter import (
    ExportTargetAdapter,
    ExportTargetMetadata,
)
from apis.app_api.export_targets.models import (
    CreatedFile,
    ExportFormat,
    ExportTargetAuthError,
    ExportTargetError,
    ExportTargetNotFoundError,
)
from apis.app_api.export_targets.registry import ExportTargetRegistry
from apis.shared.auth.models import User
from apis.shared.oauth.agentcore_identity import (
    TokenResult,
    WorkloadTokenUnavailableError,
)
from apis.shared.oauth.disconnect_repository import get_disconnect_repository
from apis.shared.oauth.models import OAuthProvider, OAuthProviderType
from apis.shared.oauth.provider_repository import get_provider_repository
from apis.shared.rbac.models import UserEffectivePermissions
from apis.shared.rbac.service import get_app_role_service
from apis.shared.sessions.models import (
    MessageContent,
    MessageResponse,
    MessagesListResponse,
    SessionMetadata,
)

ADAPTER_KEY = "stub-drive"


class _StubExportAdapter(ExportTargetAdapter):
    """Adapter that records create_document calls or raises on demand."""

    def __init__(self) -> None:
        self.supported_formats = (ExportFormat.GOOGLE_DOC, ExportFormat.MARKDOWN)
        self.created_with: Optional[SimpleNamespace] = None
        self.raise_error: Optional[ExportTargetError] = None

    @property
    def metadata(self) -> ExportTargetMetadata:
        return ExportTargetMetadata(
            key=ADAPTER_KEY,
            display_name="Stub Drive",
            icon="stub",
            compatible_provider_types=(OAuthProviderType.GOOGLE,),
            required_scopes=(),
            supported_formats=self.supported_formats,
        )

    async def list_destinations(self, access_token):  # type: ignore[no-untyped-def]
        return []

    async def create_document(  # type: ignore[no-untyped-def]
        self,
        access_token,
        *,
        content,
        name,
        source_mime_type,
        target_format,
        parent_id=None,
    ):
        if self.raise_error:
            raise self.raise_error
        self.created_with = SimpleNamespace(
            access_token=access_token,
            content=content,
            name=name,
            source_mime_type=source_mime_type,
            target_format=target_format,
            parent_id=parent_id,
        )
        return CreatedFile(
            file_id="file-123",
            name=name,
            web_view_link="https://drive.example/file-123",
        )


def _make_user(user_id: str = "alice") -> User:
    return User(
        user_id=user_id,
        email=f"{user_id}@example.com",
        name=user_id.capitalize(),
        roles=[],
        raw_token="test-token",
    )


def _make_provider(
    provider_id: str = "gdrive",
    *,
    enabled: bool = True,
    allowed_roles: Optional[list] = None,
    export_target_adapter_id: Optional[str] = ADAPTER_KEY,
    file_source_adapter_id: Optional[str] = None,
) -> OAuthProvider:
    now = datetime.now(timezone.utc).isoformat() + "Z"
    return OAuthProvider(
        provider_id=provider_id,
        display_name="Google Drive",
        provider_type=OAuthProviderType.GOOGLE,
        scopes=["https://www.googleapis.com/auth/drive.file"],
        allowed_roles=allowed_roles or [],
        enabled=enabled,
        custom_parameters=None,
        created_at=now,
        updated_at=now,
        export_target_adapter_id=export_target_adapter_id,
        file_source_adapter_id=file_source_adapter_id,
    )


def _make_permissions(
    user_id: str = "alice", *, roles: Optional[list] = None
) -> UserEffectivePermissions:
    return UserEffectivePermissions(
        user_id=user_id,
        app_roles=roles or [],
        tools=[],
        models=[],
        quota_tier=None,
        resolved_at=datetime.now(timezone.utc).isoformat() + "Z",
    )


def _make_metadata(title: str = "My Chat", user_id: str = "alice") -> SessionMetadata:
    now = datetime.now(timezone.utc).isoformat() + "Z"
    return SessionMetadata(
        session_id="sess-1",
        user_id=user_id,
        title=title,
        status="active",
        created_at=now,
        last_message_at=now,
        message_count=2,
    )


def _msg(mid: str, role: str, text: str) -> MessageResponse:
    return MessageResponse(
        id=mid,
        role=role,
        content=[MessageContent(type="text", text=text)],
        created_at="2026-06-25T00:00:00Z",
    )


class _FakeDisconnectRepo:
    def __init__(self) -> None:
        self.disconnected: set = set()

    async def is_disconnected(self, user_id: str, provider_id: str) -> bool:
        return (user_id, provider_id) in self.disconnected


@pytest.fixture
def app_with_deps(monkeypatch):
    """Mount the router and stub every external boundary."""

    def _build(
        user_id: str = "alice",
        *,
        providers: Optional[List[OAuthProvider]] = None,
        permissions: Optional[UserEffectivePermissions] = None,
        identity_result: Optional[TokenResult] = None,
        identity_raises: Optional[Exception] = None,
        adapter: Optional[ExportTargetAdapter] = None,
        metadata: Optional[SessionMetadata] = _make_metadata(),
        message_pages: Optional[List[List[MessageResponse]]] = None,
        disconnect_repo: Optional[_FakeDisconnectRepo] = None,
    ):
        providers = [_make_provider()] if providers is None else providers
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[routes.get_current_user_from_session] = (
            lambda: _make_user(user_id)
        )

        by_id = {p.provider_id: p for p in providers}
        repo = MagicMock()
        repo.list_providers = AsyncMock(return_value=list(providers))
        repo.get_provider = AsyncMock(side_effect=lambda pid: by_id.get(pid))
        app.dependency_overrides[get_provider_repository] = lambda: repo

        role_service = MagicMock()
        role_service.resolve_user_permissions = AsyncMock(
            return_value=permissions or _make_permissions(user_id),
        )
        app.dependency_overrides[get_app_role_service] = lambda: role_service

        disconnect_repo = disconnect_repo or _FakeDisconnectRepo()
        app.dependency_overrides[get_disconnect_repository] = lambda: disconnect_repo

        identity = MagicMock()
        if identity_raises is not None:
            identity.get_token_for_user = AsyncMock(side_effect=identity_raises)
        else:
            identity.get_token_for_user = AsyncMock(
                return_value=identity_result or TokenResult(access_token="vault-token"),
            )
        monkeypatch.setattr(service, "get_agentcore_identity_client", lambda: identity)

        the_adapter = adapter if adapter is not None else _StubExportAdapter()
        reg = ExportTargetRegistry()
        reg.register(the_adapter)
        # resolve_export_target reads service.registry; the catalog reads
        # routes.registry — both point at the same stub.
        monkeypatch.setattr(service, "registry", reg)
        monkeypatch.setattr(routes, "registry", reg)

        async def fake_get_session_metadata(session_id, user_id):
            return metadata

        monkeypatch.setattr(routes, "get_session_metadata", fake_get_session_metadata)

        pages = message_pages if message_pages is not None else [[_msg("m1", "user", "hi")]]

        async def fake_get_messages(session_id, user_id, limit=None, next_token=None):
            idx = int(next_token) if next_token else 0
            page = pages[idx]
            nxt = str(idx + 1) if idx + 1 < len(pages) else None
            return MessagesListResponse(messages=page, next_token=nxt)

        monkeypatch.setattr(routes, "get_messages", fake_get_messages)

        receipts: list = []

        async def fake_add_export_receipt(session_id, user_id, receipt):
            receipts.append(receipt)

        monkeypatch.setattr(routes, "add_export_receipt", fake_add_export_receipt)

        return SimpleNamespace(
            app=app,
            adapter=the_adapter,
            identity=identity,
            receipts=receipts,
            disconnect_repo=disconnect_repo,
        )

    return _build


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class TestListExportTargets:
    def test_lists_only_mapped_visible_connectors(self, app_with_deps):
        ctx = app_with_deps(
            providers=[
                _make_provider("gdrive"),
                _make_provider("slack", export_target_adapter_id=None),
                _make_provider("secret", allowed_roles=["admins"]),
            ],
        )
        response = TestClient(ctx.app).get("/export-targets")

        assert response.status_code == 200
        targets = response.json()["exportTargets"]
        assert [t["providerId"] for t in targets] == ["gdrive"]
        assert targets[0]["connected"] is True
        assert targets[0]["supportedFormats"] == ["google_doc", "markdown"]
        # Export-only connector (no file_source_adapter_id) → no folder picker.
        assert targets[0]["browsable"] is False

    def test_browsable_true_for_combined_scope_connector(self, app_with_deps):
        # A connector also mapped to a shipped file-source adapter backs the
        # destination folder picker via the reused import browse endpoints.
        ctx = app_with_deps(
            providers=[_make_provider("gdrive", file_source_adapter_id="google-drive")],
        )
        response = TestClient(ctx.app).get("/export-targets")

        assert response.status_code == 200
        targets = response.json()["exportTargets"]
        assert targets[0]["browsable"] is True

    def test_browsable_false_for_unshipped_file_source_adapter(self, app_with_deps):
        # An admin can map a file_source_adapter_id that no longer ships; the
        # picker would 404, so the catalog reports it as not browsable.
        ctx = app_with_deps(
            providers=[_make_provider("gdrive", file_source_adapter_id="ghost")],
        )
        response = TestClient(ctx.app).get("/export-targets")

        assert response.status_code == 200
        assert response.json()["exportTargets"][0]["browsable"] is False

    def test_includes_role_gated_connector_when_user_has_role(self, app_with_deps):
        ctx = app_with_deps(
            providers=[_make_provider("secret", allowed_roles=["admins"])],
            permissions=_make_permissions(roles=["admins"]),
        )
        response = TestClient(ctx.app).get("/export-targets")

        assert response.status_code == 200
        assert [t["providerId"] for t in response.json()["exportTargets"]] == ["secret"]

    def test_connected_false_when_consent_required(self, app_with_deps):
        ctx = app_with_deps(
            identity_result=TokenResult(authorization_url="https://auth.example/x"),
        )
        response = TestClient(ctx.app).get("/export-targets")

        assert response.status_code == 200
        assert response.json()["exportTargets"][0]["connected"] is False

    def test_unknown_adapter_key_omitted(self, app_with_deps):
        ctx = app_with_deps(
            providers=[_make_provider("gdrive", export_target_adapter_id="dropbox")],
        )
        response = TestClient(ctx.app).get("/export-targets")

        assert response.status_code == 200
        assert response.json()["exportTargets"] == []


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


class TestExportSession:
    def test_success_returns_file_and_persists_receipt(self, app_with_deps):
        ctx = app_with_deps(
            message_pages=[[_msg("m1", "user", "hello"), _msg("m2", "assistant", "hi there")]],
        )
        response = TestClient(ctx.app).post(
            "/sessions/sess-1/export", json={"connectorId": "gdrive"}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["fileId"] == "file-123"
        assert body["webViewLink"] == "https://drive.example/file-123"
        assert body["receipt"]["connectorId"] == "gdrive"
        assert body["receipt"]["adapterKey"] == ADAPTER_KEY
        assert body["receipt"]["format"] == "google_doc"

        # Default format → native Google Doc → uploads HTML.
        assert ctx.adapter.created_with.target_format == ExportFormat.GOOGLE_DOC
        assert ctx.adapter.created_with.source_mime_type == "text/html"
        assert ctx.adapter.created_with.name == "My Chat"
        # Receipt persisted exactly once.
        assert len(ctx.receipts) == 1
        assert ctx.receipts[0].file_id == "file-123"

    def test_pages_full_transcript(self, app_with_deps):
        ctx = app_with_deps(
            message_pages=[
                [_msg("m1", "user", "page-zero-text")],
                [_msg("m2", "assistant", "page-one-text")],
            ],
        )
        response = TestClient(ctx.app).post(
            "/sessions/sess-1/export",
            json={"connectorId": "gdrive", "format": "markdown"},
        )

        assert response.status_code == 200
        rendered = ctx.adapter.created_with.content.decode("utf-8")
        # Both pages made it into the document, in order.
        assert "page-zero-text" in rendered
        assert "page-one-text" in rendered
        assert rendered.index("page-zero-text") < rendered.index("page-one-text")

    def test_404_when_session_missing(self, app_with_deps):
        ctx = app_with_deps(metadata=None)
        response = TestClient(ctx.app).post(
            "/sessions/sess-1/export", json={"connectorId": "gdrive"}
        )
        assert response.status_code == 404

    def test_404_when_not_an_export_target(self, app_with_deps):
        ctx = app_with_deps(
            providers=[_make_provider("gdrive", export_target_adapter_id=None)],
        )
        response = TestClient(ctx.app).post(
            "/sessions/sess-1/export", json={"connectorId": "gdrive"}
        )
        assert response.status_code == 404

    def test_403_when_user_lacks_role(self, app_with_deps):
        ctx = app_with_deps(
            providers=[_make_provider("gdrive", allowed_roles=["admins"])],
            permissions=_make_permissions(roles=["users"]),
        )
        response = TestClient(ctx.app).post(
            "/sessions/sess-1/export", json={"connectorId": "gdrive"}
        )
        assert response.status_code == 403

    def test_409_when_not_connected(self, app_with_deps):
        ctx = app_with_deps(
            identity_result=TokenResult(authorization_url="https://auth.example/x"),
        )
        response = TestClient(ctx.app).post(
            "/sessions/sess-1/export", json={"connectorId": "gdrive"}
        )
        assert response.status_code == 409
        # No upload attempted, no receipt persisted on a consent miss.
        assert ctx.adapter.created_with is None
        assert ctx.receipts == []

    def test_503_when_workload_unavailable(self, app_with_deps):
        ctx = app_with_deps(
            identity_raises=WorkloadTokenUnavailableError("no workload"),
        )
        response = TestClient(ctx.app).post(
            "/sessions/sess-1/export", json={"connectorId": "gdrive"}
        )
        assert response.status_code == 503

    def test_422_when_format_unsupported(self, app_with_deps):
        ctx = app_with_deps()
        response = TestClient(ctx.app).post(
            "/sessions/sess-1/export",
            json={"connectorId": "gdrive", "format": "pdf"},
        )
        assert response.status_code == 422
        assert ctx.adapter.created_with is None

    def test_502_on_adapter_error(self, app_with_deps):
        adapter = _StubExportAdapter()
        adapter.raise_error = ExportTargetError("drive exploded")
        ctx = app_with_deps(adapter=adapter)
        response = TestClient(ctx.app).post(
            "/sessions/sess-1/export", json={"connectorId": "gdrive"}
        )
        assert response.status_code == 502
        assert ctx.receipts == []

    def test_403_on_adapter_auth_error(self, app_with_deps):
        adapter = _StubExportAdapter()
        adapter.raise_error = ExportTargetAuthError("token rejected")
        ctx = app_with_deps(adapter=adapter)
        response = TestClient(ctx.app).post(
            "/sessions/sess-1/export", json={"connectorId": "gdrive"}
        )
        assert response.status_code == 403

    def test_404_on_adapter_not_found_error(self, app_with_deps):
        adapter = _StubExportAdapter()
        adapter.raise_error = ExportTargetNotFoundError("folder gone")
        ctx = app_with_deps(adapter=adapter)
        response = TestClient(ctx.app).post(
            "/sessions/sess-1/export",
            json={"connectorId": "gdrive", "parentId": "missing"},
        )
        assert response.status_code == 404

    def test_parent_id_passed_to_adapter(self, app_with_deps):
        ctx = app_with_deps()
        response = TestClient(ctx.app).post(
            "/sessions/sess-1/export",
            json={"connectorId": "gdrive", "parentId": "folder-9"},
        )
        assert response.status_code == 200
        assert ctx.adapter.created_with.parent_id == "folder-9"
