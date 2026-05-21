"""Route-level tests for the user-facing file-source endpoints.

Covers the catalog (`GET /file-sources`) and the browsing surface
(`GET /connectors/{id}/{roots,browse,search}`). External boundaries — the
provider repository, role service, disconnect repository, AgentCore identity
client, and the adapter registry — are stubbed; we test our gating, error
mapping, and response shape, not the downstream provider calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.file_sources import routes, service
from apis.app_api.file_sources.adapter import AdapterMetadata, FileSourceAdapter
from apis.app_api.file_sources.models import (
    BrowseResult,
    DownloadedFile,
    FileEntry,
    FileEntryType,
    FileSourceAuthError,
    FileSourceError,
    FileSourceNotFoundError,
    SourceRoot,
)
from apis.app_api.file_sources.registry import AdapterRegistry
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

ADAPTER_KEY = "stub-drive"


class _StubAdapter(FileSourceAdapter):
    """Adapter whose browse methods return canned values or raise on demand."""

    def __init__(self) -> None:
        self.roots = [SourceRoot(id="root", name="My Drive")]
        self.browse_result = BrowseResult(
            entries=[FileEntry(id="f1", name="notes.txt", type=FileEntryType.FILE)]
        )
        self.search_result = BrowseResult(
            entries=[FileEntry(id="f2", name="hit.txt", type=FileEntryType.FILE)]
        )
        self.raise_error: FileSourceError | None = None

    @property
    def metadata(self) -> AdapterMetadata:
        return AdapterMetadata(
            key=ADAPTER_KEY,
            display_name="Stub Drive",
            icon="stub",
            compatible_provider_types=(OAuthProviderType.GOOGLE,),
            required_scopes=(),
        )

    async def list_roots(self, access_token):  # type: ignore[no-untyped-def]
        if self.raise_error:
            raise self.raise_error
        return self.roots

    async def browse(self, access_token, folder_id, cursor=None):  # type: ignore[no-untyped-def]
        if self.raise_error:
            raise self.raise_error
        return self.browse_result

    async def search(self, access_token, query, cursor=None):  # type: ignore[no-untyped-def]
        if self.raise_error:
            raise self.raise_error
        return self.search_result

    async def download(self, access_token, file_id):  # type: ignore[no-untyped-def]
        return DownloadedFile(content=b"", filename="f", content_type="text/plain")


def _make_user(user_id: str) -> User:
    return User(
        user_id=user_id,
        email=f"{user_id}@example.com",
        name=user_id.capitalize(),
        roles=[],
        raw_token="test-token",
    )


def _make_provider(
    provider_id: str = "google",
    *,
    enabled: bool = True,
    allowed_roles: list[str] | None = None,
    file_source_adapter_id: str | None = ADAPTER_KEY,
) -> OAuthProvider:
    now = datetime.now(timezone.utc).isoformat() + "Z"
    return OAuthProvider(
        provider_id=provider_id,
        display_name=provider_id.capitalize(),
        provider_type=OAuthProviderType.GOOGLE,
        scopes=["openid", "email"],
        allowed_roles=allowed_roles or [],
        enabled=enabled,
        custom_parameters=None,
        created_at=now,
        updated_at=now,
        file_source_adapter_id=file_source_adapter_id,
    )


def _make_permissions(
    user_id: str, *, roles: list[str] | None = None
) -> UserEffectivePermissions:
    return UserEffectivePermissions(
        user_id=user_id,
        app_roles=roles or [],
        tools=[],
        models=[],
        quota_tier=None,
        resolved_at=datetime.now(timezone.utc).isoformat() + "Z",
    )


class _FakeDisconnectRepo:
    """In-memory stand-in for the durable DDB-backed disconnect repository."""

    def __init__(self) -> None:
        self.disconnected: set[tuple[str, str]] = set()

    async def is_disconnected(self, user_id: str, provider_id: str) -> bool:
        return (user_id, provider_id) in self.disconnected

    async def mark_disconnected(self, user_id: str, provider_id: str) -> None:
        self.disconnected.add((user_id, provider_id))

    async def clear_disconnected(self, user_id: str, provider_id: str) -> None:
        self.disconnected.discard((user_id, provider_id))


@pytest.fixture
def app_with_deps(monkeypatch):
    """Mount the router and stub every external boundary.

    Returns a builder so each test wires the specific responses it needs.
    """

    def _build(
        user_id: str,
        *,
        providers: list[OAuthProvider],
        permissions: UserEffectivePermissions | None = None,
        identity_result: TokenResult | None = None,
        identity_raises: Exception | None = None,
        adapter: FileSourceAdapter | None = None,
        disconnect_repo: _FakeDisconnectRepo | None = None,
    ) -> tuple[FastAPI, MagicMock, _FakeDisconnectRepo]:
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

        reg = AdapterRegistry()
        reg.register(adapter if adapter is not None else _StubAdapter())
        monkeypatch.setattr(service, "registry", reg)

        return app, identity, disconnect_repo

    return _build


class TestListFileSources:
    def test_lists_only_mapped_visible_connectors(self, app_with_deps):
        # google: mapped → included. slack: no adapter → excluded.
        # secret: mapped but role-gated and the user lacks the role → excluded.
        app, _, _ = app_with_deps(
            "alice",
            providers=[
                _make_provider("google"),
                _make_provider("slack", file_source_adapter_id=None),
                _make_provider("secret", allowed_roles=["admins"]),
            ],
        )
        response = TestClient(app).get("/file-sources")

        assert response.status_code == 200
        sources = response.json()["fileSources"]
        assert [s["providerId"] for s in sources] == ["google"]
        assert sources[0]["connected"] is True
        assert sources[0]["displayName"] == "Google"

    def test_includes_role_gated_connector_when_user_has_role(self, app_with_deps):
        app, _, _ = app_with_deps(
            "alice",
            providers=[_make_provider("secret", allowed_roles=["admins"])],
            permissions=_make_permissions("alice", roles=["admins"]),
        )
        response = TestClient(app).get("/file-sources")

        assert response.status_code == 200
        assert [s["providerId"] for s in response.json()["fileSources"]] == ["secret"]

    def test_connected_false_when_consent_required(self, app_with_deps):
        app, _, _ = app_with_deps(
            "alice",
            providers=[_make_provider("google")],
            identity_result=TokenResult(authorization_url="https://auth.example/x"),
        )
        response = TestClient(app).get("/file-sources")

        assert response.status_code == 200
        assert response.json()["fileSources"][0]["connected"] is False

    def test_disconnect_overrides_connected(self, app_with_deps):
        repo = _FakeDisconnectRepo()
        repo.disconnected.add(("alice", "google"))
        app, identity, _ = app_with_deps(
            "alice",
            providers=[_make_provider("google")],
            identity_result=TokenResult(access_token="vault-token"),
            disconnect_repo=repo,
        )
        response = TestClient(app).get("/file-sources")

        assert response.status_code == 200
        assert response.json()["fileSources"][0]["connected"] is False
        # A disconnected connector never consults AgentCore for its badge.
        identity.get_token_for_user.assert_not_called()

    def test_connected_false_when_workload_context_unavailable(self, app_with_deps):
        # An environment misconfiguration must not 500 the whole catalog —
        # the user just sees "Connect" and gets the actionable 503 then.
        app, _, _ = app_with_deps(
            "alice",
            providers=[_make_provider("google")],
            identity_raises=WorkloadTokenUnavailableError("no workload token"),
        )
        response = TestClient(app).get("/file-sources")

        assert response.status_code == 200
        assert response.json()["fileSources"][0]["connected"] is False


class TestListRoots:
    def test_returns_roots(self, app_with_deps):
        app, _, _ = app_with_deps("alice", providers=[_make_provider("google")])
        response = TestClient(app).get("/connectors/google/roots")

        assert response.status_code == 200
        assert response.json() == {"roots": [{"id": "root", "name": "My Drive"}]}

    def test_404_when_connector_not_a_file_source(self, app_with_deps):
        app, _, _ = app_with_deps(
            "alice",
            providers=[_make_provider("google", file_source_adapter_id=None)],
        )
        response = TestClient(app).get("/connectors/google/roots")

        assert response.status_code == 404

    def test_404_when_connector_missing(self, app_with_deps):
        app, _, _ = app_with_deps("alice", providers=[])
        response = TestClient(app).get("/connectors/google/roots")

        assert response.status_code == 404

    def test_409_when_not_connected(self, app_with_deps):
        app, _, _ = app_with_deps(
            "alice",
            providers=[_make_provider("google")],
            identity_result=TokenResult(authorization_url="https://auth.example/x"),
        )
        response = TestClient(app).get("/connectors/google/roots")

        assert response.status_code == 409

    def test_502_on_file_source_error(self, app_with_deps):
        adapter = _StubAdapter()
        adapter.raise_error = FileSourceError("provider exploded")
        app, _, _ = app_with_deps(
            "alice", providers=[_make_provider("google")], adapter=adapter
        )
        response = TestClient(app).get("/connectors/google/roots")

        assert response.status_code == 502

    def test_403_on_auth_error(self, app_with_deps):
        adapter = _StubAdapter()
        adapter.raise_error = FileSourceAuthError("token rejected")
        app, _, _ = app_with_deps(
            "alice", providers=[_make_provider("google")], adapter=adapter
        )
        response = TestClient(app).get("/connectors/google/roots")

        assert response.status_code == 403


class TestBrowse:
    def test_returns_browse_page(self, app_with_deps):
        app, _, _ = app_with_deps("alice", providers=[_make_provider("google")])
        response = TestClient(app).get(
            "/connectors/google/browse", params={"folder_id": "root"}
        )

        assert response.status_code == 200
        entries = response.json()["entries"]
        assert entries[0]["id"] == "f1"

    def test_422_when_folder_id_missing(self, app_with_deps):
        app, _, _ = app_with_deps("alice", providers=[_make_provider("google")])
        response = TestClient(app).get("/connectors/google/browse")

        assert response.status_code == 422

    def test_404_on_not_found_error(self, app_with_deps):
        adapter = _StubAdapter()
        adapter.raise_error = FileSourceNotFoundError("folder gone")
        app, _, _ = app_with_deps(
            "alice", providers=[_make_provider("google")], adapter=adapter
        )
        response = TestClient(app).get(
            "/connectors/google/browse", params={"folder_id": "missing"}
        )

        assert response.status_code == 404

    def test_403_when_user_lacks_role(self, app_with_deps):
        app, _, _ = app_with_deps(
            "alice",
            providers=[_make_provider("google", allowed_roles=["admins"])],
            permissions=_make_permissions("alice", roles=["users"]),
        )
        response = TestClient(app).get(
            "/connectors/google/browse", params={"folder_id": "root"}
        )

        assert response.status_code == 403


class TestSearch:
    def test_returns_search_page(self, app_with_deps):
        app, _, _ = app_with_deps("alice", providers=[_make_provider("google")])
        response = TestClient(app).get(
            "/connectors/google/search", params={"query": "hit"}
        )

        assert response.status_code == 200
        assert response.json()["entries"][0]["id"] == "f2"

    def test_422_when_query_missing(self, app_with_deps):
        app, _, _ = app_with_deps("alice", providers=[_make_provider("google")])
        response = TestClient(app).get("/connectors/google/search")

        assert response.status_code == 422

    def test_503_when_workload_context_unavailable(self, app_with_deps):
        app, _, _ = app_with_deps(
            "alice",
            providers=[_make_provider("google")],
            identity_raises=WorkloadTokenUnavailableError("no workload token"),
        )
        response = TestClient(app).get(
            "/connectors/google/search", params={"query": "hit"}
        )

        assert response.status_code == 503
