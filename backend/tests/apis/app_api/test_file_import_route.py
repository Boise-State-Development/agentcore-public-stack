"""Route-level tests for `POST /files/import` (attach a file-source file to a chat).

The composer's "Add from Google Drive". External boundaries — the provider
repository, role service, AgentCore identity client, adapter registry and the
upload service — are stubbed; the tests cover the gating, the download cap
handed to the adapter, and the error mapping, not the provider or S3 calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.file_sources import service as file_source_service
from apis.app_api.file_sources.adapter import AdapterMetadata, FileSourceAdapter
from apis.app_api.file_sources.models import (
    BrowseResult,
    DownloadedFile,
    FileSourceError,
    FileSourceTooLargeError,
    SourceRoot,
)
from apis.app_api.file_sources.registry import AdapterRegistry
from apis.app_api.files import routes
from apis.app_api.files.service import (
    FileTooLargeError,
    FileUploadError,
    InvalidFileTypeError,
    QuotaExceededError,
)
from apis.shared.auth.models import User
from apis.shared.files.models import FileMetadata, FileStatus
from apis.shared.oauth.agentcore_identity import TokenResult
from apis.shared.oauth.models import OAuthProvider, OAuthProviderType
from apis.shared.oauth.provider_repository import get_provider_repository
from apis.shared.rbac.models import UserEffectivePermissions
from apis.shared.rbac.service import get_app_role_service

ADAPTER_KEY = "stub-drive"
DOWNLOAD_CAP = 25 * 1024 * 1024


class _StubAdapter(FileSourceAdapter):
    """Adapter whose `download` returns a canned file or raises on demand."""

    def __init__(self) -> None:
        self.downloaded = DownloadedFile(
            content=b"%PDF-bytes", filename="report.pdf", content_type="application/pdf"
        )
        self.raise_error: FileSourceError | None = None
        self.calls: list[tuple[str, str, int | None]] = []

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
        return [SourceRoot(id="root", name="My Drive")]

    async def browse(self, access_token, folder_id, cursor=None):  # type: ignore[no-untyped-def]
        return BrowseResult()

    async def search(self, access_token, query, cursor=None):  # type: ignore[no-untyped-def]
        return BrowseResult()

    async def download(self, access_token, file_id, max_bytes=None):  # type: ignore[no-untyped-def]
        self.calls.append((access_token, file_id, max_bytes))
        if self.raise_error:
            raise self.raise_error
        return self.downloaded


def _user(user_id: str = "alice") -> User:
    return User(
        user_id=user_id,
        email=f"{user_id}@example.com",
        name=user_id.capitalize(),
        roles=[],
        raw_token="test-token",
    )


def _provider(
    provider_id: str = "google",
    *,
    file_source_adapter_id: str | None = ADAPTER_KEY,
) -> OAuthProvider:
    now = datetime.now(timezone.utc).isoformat() + "Z"
    return OAuthProvider(
        provider_id=provider_id,
        display_name="Google Drive",
        provider_type=OAuthProviderType.GOOGLE,
        scopes=["openid"],
        allowed_roles=[],
        enabled=True,
        custom_parameters=None,
        created_at=now,
        updated_at=now,
        file_source_adapter_id=file_source_adapter_id,
    )


def _ready_meta(**overrides) -> FileMetadata:
    fields = dict(
        upload_id="up-1",
        user_id="alice",
        session_id="sess-1",
        filename="report.pdf",
        mime_type="application/pdf",
        size_bytes=10,
        s3_key="user-files/alice/sess-1/up-1/report.pdf",
        s3_bucket="bucket",
        status=FileStatus.READY,
        source="connector:google",
    )
    fields.update(overrides)
    return FileMetadata(**fields)


@pytest.fixture
def build(monkeypatch):
    """Mount the files router with every boundary stubbed; returns a builder."""

    def _build(
        *,
        providers: list[OAuthProvider] | None = None,
        identity_result: TokenResult | None = None,
        adapter: _StubAdapter | None = None,
        import_result: FileMetadata | Exception | None = None,
    ) -> tuple[TestClient, _StubAdapter, MagicMock]:
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[routes.get_current_user_from_session] = _user

        providers = providers if providers is not None else [_provider()]
        by_id = {p.provider_id: p for p in providers}
        repo = MagicMock()
        repo.get_provider = AsyncMock(side_effect=lambda pid: by_id.get(pid))
        app.dependency_overrides[get_provider_repository] = lambda: repo

        role_service = MagicMock()
        role_service.resolve_user_permissions = AsyncMock(
            return_value=UserEffectivePermissions(
                user_id="alice",
                app_roles=[],
                tools=[],
                models=[],
                quota_tier=None,
                resolved_at=datetime.now(timezone.utc).isoformat() + "Z",
            )
        )
        app.dependency_overrides[get_app_role_service] = lambda: role_service

        identity = MagicMock()
        identity.get_token_for_user = AsyncMock(
            return_value=identity_result or TokenResult(access_token="vault-token")
        )
        monkeypatch.setattr(
            file_source_service, "get_agentcore_identity_client", lambda: identity
        )

        adapter = adapter or _StubAdapter()
        reg = AdapterRegistry()
        reg.register(adapter)
        monkeypatch.setattr(file_source_service, "registry", reg)

        upload_service = MagicMock()
        upload_service.max_download_bytes.return_value = DOWNLOAD_CAP
        if isinstance(import_result, Exception):
            upload_service.import_file = AsyncMock(side_effect=import_result)
        else:
            upload_service.import_file = AsyncMock(return_value=import_result or _ready_meta())
        app.dependency_overrides[routes.get_file_upload_service] = lambda: upload_service

        return TestClient(app), adapter, upload_service

    return _build


BODY = {"sessionId": "sess-1", "connectorId": "google", "fileId": "drive-file-1"}


class TestImportFile:
    def test_downloads_under_the_widest_cap_and_stores_as_the_user(self, build):
        client, adapter, upload_service = build()

        response = client.post("/files/import", json=BODY)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["uploadId"] == "up-1"
        assert body["filename"] == "report.pdf"
        assert body["mimeType"] == "application/pdf"
        assert body["sessionId"] == "sess-1"
        assert body["status"] == "ready"
        assert body["s3Uri"] == "s3://bucket/user-files/alice/sess-1/up-1/report.pdf"
        # The vaulted token and the service's cap reach the adapter.
        assert adapter.calls == [("vault-token", "drive-file-1", DOWNLOAD_CAP)]
        upload_service.import_file.assert_awaited_once_with(
            user_id="alice",
            session_id="sess-1",
            filename="report.pdf",
            mime_type="application/pdf",
            content=b"%PDF-bytes",
            source="connector:google",
        )

    def test_needs_consent_is_409_and_downloads_nothing(self, build):
        client, adapter, upload_service = build(
            identity_result=TokenResult(authorization_url="https://auth.example/x"),
        )

        response = client.post("/files/import", json=BODY)

        assert response.status_code == 409
        assert adapter.calls == []
        upload_service.import_file.assert_not_awaited()

    def test_connector_without_an_adapter_is_404(self, build):
        client, _, _ = build(providers=[_provider(file_source_adapter_id=None)])

        response = client.post("/files/import", json=BODY)

        assert response.status_code == 404

    def test_unknown_connector_is_404(self, build):
        client, _, _ = build()

        response = client.post("/files/import", json={**BODY, "connectorId": "nope"})

        assert response.status_code == 404

    def test_oversized_download_is_400_without_storing(self, build):
        adapter = _StubAdapter()
        adapter.raise_error = FileSourceTooLargeError("too big")
        client, _, upload_service = build(adapter=adapter)

        response = client.post("/files/import", json=BODY)

        assert response.status_code == 400
        assert "25MB" in response.json()["detail"]
        upload_service.import_file.assert_not_awaited()

    def test_provider_failure_is_502(self, build):
        adapter = _StubAdapter()
        adapter.raise_error = FileSourceError("drive down")
        client, _, upload_service = build(adapter=adapter)

        response = client.post("/files/import", json=BODY)

        assert response.status_code == 502
        upload_service.import_file.assert_not_awaited()

    @pytest.mark.parametrize(
        "error,expected_status,detail_fragment",
        [
            (InvalidFileTypeError("application/zip"), 400, "Unsupported file type"),
            (FileTooLargeError(5 * 1024 * 1024, 4 * 1024 * 1024), 400, "4MB"),
            (FileUploadError("The file could not be stored."), 502, "could not be stored"),
        ],
    )
    def test_upload_service_errors_map_like_presign(
        self, build, error, expected_status, detail_fragment
    ):
        client, _, _ = build(import_result=error)

        response = client.post("/files/import", json=BODY)

        assert response.status_code == expected_status
        assert detail_fragment in response.json()["detail"]

    def test_quota_exceeded_is_403_with_the_quota_body(self, build):
        client, _, _ = build(
            import_result=QuotaExceededError(
                current_usage=900, max_allowed=1000, required_space=200
            )
        )

        response = client.post("/files/import", json=BODY)

        assert response.status_code == 403
        detail = response.json()["detail"]
        assert detail["currentUsage"] == 900
        assert detail["maxAllowed"] == 1000
        assert detail["requiredSpace"] == 200

    def test_body_must_name_a_file(self, build):
        client, _, _ = build()

        response = client.post("/files/import", json={"sessionId": "s", "connectorId": "google"})

        assert response.status_code == 422
