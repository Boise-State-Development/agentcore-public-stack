"""Route-level tests for ``/sessions/{id}/folders`` (drive-folder spec §4.2).

The connector resolution is the browse route's, so the provider repository,
role service, identity client and adapter registry are stubbed the same way
``test_file_import_route.py`` does. The folder service is real, over an
in-memory row store and a stubbed upload service, with the expansion task
replaced by a recorder: these tests prove the gating, the row's lifecycle and
that the task is scheduled with the adapter and the vaulted token, not the
walk itself (``test_folder_expansion.py``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.file_sources import service as file_source_service
from apis.app_api.file_sources.adapter import AdapterMetadata, FileSourceAdapter
from apis.app_api.file_sources.models import BrowseResult, DownloadedFile, SourceRoot
from apis.app_api.file_sources.registry import AdapterRegistry
from apis.app_api.files.folders import routes
from apis.app_api.files.folders import service as folder_service_module
from apis.app_api.files.folders.service import FolderAttachmentService
from apis.shared.auth.models import User
from apis.shared.files.folder_attachments import FolderAttachment, FolderCounts, FolderFileEntry, RecentFolder
from apis.shared.oauth.agentcore_identity import TokenResult, WorkloadTokenUnavailableError
from apis.shared.oauth.models import OAuthProvider, OAuthProviderType
from apis.shared.oauth.provider_repository import get_provider_repository
from apis.shared.rbac.models import UserEffectivePermissions
from apis.shared.rbac.service import get_app_role_service

ADAPTER_KEY = "stub-drive"


class _StubAdapter(FileSourceAdapter):
    @property
    def metadata(self) -> AdapterMetadata:
        return AdapterMetadata(key=ADAPTER_KEY, display_name="Stub Drive", icon="stub",
                               compatible_provider_types=(OAuthProviderType.GOOGLE,), required_scopes=())

    async def list_roots(self, access_token):  # type: ignore[no-untyped-def]
        return [SourceRoot(id="root", name="My Drive")]

    async def browse(self, access_token, folder_id, cursor=None):  # type: ignore[no-untyped-def]
        return BrowseResult()

    async def search(self, access_token, query, cursor=None):  # type: ignore[no-untyped-def]
        return BrowseResult()

    async def download(self, access_token, file_id, max_bytes=None):  # type: ignore[no-untyped-def]
        return DownloadedFile(content=b"", filename="x", content_type="text/plain")


class _Rows:
    """In-memory folder rows keyed like the table."""

    def __init__(self) -> None:
        self.rows: Dict[tuple, FolderAttachment] = {}
        self.recent: Dict[str, list] = {}
        self.recent_calls: List[tuple] = []

    async def create(self, folder):  # type: ignore[no-untyped-def]
        self.rows[(folder.user_id, folder.folder_attach_id)] = folder.model_copy(deep=True)
        return folder

    async def get(self, user_id, folder_attach_id):  # type: ignore[no-untyped-def]
        row = self.rows.get((user_id, folder_attach_id))
        return row.model_copy(deep=True) if row else None

    async def update(self, user_id, folder_attach_id, **fields):  # type: ignore[no-untyped-def]
        row = self.rows.get((user_id, folder_attach_id))
        if row is None:
            return None
        data = row.model_dump()
        for key in ("status", "error", "refreshing"):
            if fields.get(key) is not None:
                data[key] = fields[key]
        if fields.get("clear_error"):
            data["error"] = None
        self.rows[(user_id, folder_attach_id)] = FolderAttachment.model_validate(data)
        return self.rows[(user_id, folder_attach_id)].model_copy(deep=True)

    async def delete(self, user_id, folder_attach_id):  # type: ignore[no-untyped-def]
        return self.rows.pop((user_id, folder_attach_id), None)

    async def list_session_folders(self, session_id):  # type: ignore[no-untyped-def]
        return [r.model_copy(deep=True) for r in self.rows.values() if r.session_id == session_id]

    async def list_recent_folders(self, user_id, limit):  # type: ignore[no-untyped-def]
        self.recent_calls.append((user_id, limit))
        return list(self.recent.get(user_id, []))[:limit]


def _user(user_id: str = "alice") -> User:
    return User(user_id=user_id, email=f"{user_id}@example.com", name=user_id.capitalize(), roles=[], raw_token="t")


def _provider(provider_id: str = "google", *, file_source_adapter_id: Optional[str] = ADAPTER_KEY, allowed_roles=None) -> OAuthProvider:
    now = datetime.now(timezone.utc).isoformat() + "Z"
    return OAuthProvider(provider_id=provider_id, display_name="Google Drive", provider_type=OAuthProviderType.GOOGLE,
                         scopes=["openid"], allowed_roles=allowed_roles or [], enabled=True, custom_parameters=None,
                         created_at=now, updated_at=now, file_source_adapter_id=file_source_adapter_id)


def _row(**overrides) -> FolderAttachment:
    fields = dict(folder_attach_id="fa-1", session_id="sess-1", user_id="alice", connector_id="google",
                  provider_folder_id="drive-folder-1", folder_name="BIO 101", folder_path="My Drive/BIO 101")
    fields.update(overrides)
    return FolderAttachment(**fields)


@pytest.fixture
def build(monkeypatch):
    monkeypatch.setenv("FOLDER_ATTACHMENTS_ENABLED", "true")
    monkeypatch.setattr(FolderAttachmentService, "_running", set())
    monkeypatch.setattr(FolderAttachmentService, "_tasks", set())

    def _build(*, providers=None, identity_result=None, identity_error=None, rows=None):
        app = FastAPI()
        app.include_router(routes.router)
        app.include_router(routes.recent_router)
        app.dependency_overrides[routes.get_current_user_from_session] = _user

        providers = providers if providers is not None else [_provider()]
        by_id = {p.provider_id: p for p in providers}
        repo = MagicMock()
        repo.get_provider = AsyncMock(side_effect=lambda pid: by_id.get(pid))
        app.dependency_overrides[get_provider_repository] = lambda: repo

        role_service = MagicMock()
        role_service.resolve_user_permissions = AsyncMock(return_value=UserEffectivePermissions(
            user_id="alice", app_roles=[], tools=[], models=[], quota_tier=None,
            resolved_at=datetime.now(timezone.utc).isoformat() + "Z"))
        app.dependency_overrides[get_app_role_service] = lambda: role_service

        identity = MagicMock()
        if identity_error is not None:
            identity.get_token_for_user = AsyncMock(side_effect=identity_error)
        else:
            identity.get_token_for_user = AsyncMock(return_value=identity_result or TokenResult(access_token="vault-token"))
        monkeypatch.setattr(file_source_service, "get_agentcore_identity_client", lambda: identity)

        reg = AdapterRegistry()
        adapter = _StubAdapter()
        reg.register(adapter)
        monkeypatch.setattr(file_source_service, "registry", reg)

        rows = rows or _Rows()
        upload_service = MagicMock()
        upload_service.delete_file = AsyncMock(return_value=True)
        service = FolderAttachmentService(repository=rows, upload_service=upload_service)
        app.dependency_overrides[routes.get_folder_attachment_service] = lambda: service

        scheduled: List[tuple] = []
        running_when_scheduled: List[bool] = []

        def fake_expand(folder, deps):  # type: ignore[no-untyped-def]
            scheduled.append((folder, deps))

            async def _done():
                # The task holds the running mark for its lifetime.
                running_when_scheduled.append(FolderAttachmentService.is_running(folder.folder_attach_id))
                return None

            return _done()

        scheduled_running = running_when_scheduled

        monkeypatch.setattr(folder_service_module, "expand_folder", fake_expand)

        client = TestClient(app)
        client.scheduled_running = scheduled_running  # type: ignore[attr-defined]
        return client, rows, upload_service, scheduled, adapter

    return _build


BODY = {"connectorId": "google", "folderId": "drive-folder-1", "folderName": "BIO 101", "folderPath": "My Drive/BIO 101"}


class TestFlag:
    @pytest.mark.parametrize("value", ["false", " FALSE "])
    def test_every_route_404s_while_off(self, build, monkeypatch, value):
        client, rows, _, scheduled, _ = build()
        monkeypatch.setenv("FOLDER_ATTACHMENTS_ENABLED", value)
        assert client.post("/sessions/sess-1/folders", json=BODY).status_code == 404
        assert client.get("/sessions/sess-1/folders/fa-1").status_code == 404
        assert client.delete("/sessions/sess-1/folders/fa-1").status_code == 404
        assert client.post("/sessions/sess-1/folders/fa-1/resume").status_code == 404
        assert client.get("/folders/recent").status_code == 404
        assert rows.rows == {} and scheduled == [] and rows.recent_calls == []


class TestAttach:
    def test_creates_the_row_expanding_and_schedules_the_walk(self, build):
        client, rows, _, scheduled, adapter = build()

        response = client.post("/sessions/sess-1/folders", json=BODY)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "expanding" and body["stale"] is False
        assert body["sessionId"] == "sess-1" and body["connectorId"] == "google"
        assert body["connectorName"] == "Google Drive"  # the manifest's `source`, frozen at attach
        assert body["providerFolderId"] == "drive-folder-1"
        assert body["folderName"] == "BIO 101" and body["folderPath"] == "My Drive/BIO 101"
        assert body["counts"] == {"listed": 0, "attached": 0, "skippedType": 0, "skippedSize": 0, "failed": 0, "truncated": False}
        assert body["files"] == []
        assert "ttl" not in body
        assert "vault-token" not in response.text
        fid = body["folderAttachId"]
        assert ("alice", fid) in rows.rows
        # The task got the row, the adapter and the vaulted token.
        assert len(scheduled) == 1
        folder, deps = scheduled[0]
        assert folder.folder_attach_id == fid
        assert deps.adapter is adapter and deps.access_token == "vault-token"
        assert "vault-token" not in repr(deps)
        # Marked running while the task lives, and released once it is done.
        assert client.scheduled_running == [True]
        assert not FolderAttachmentService.is_running(fid)

    def test_needs_consent_is_409_and_creates_nothing(self, build):
        client, rows, _, scheduled, _ = build(identity_result=TokenResult(authorization_url="https://auth.example/x"))
        assert client.post("/sessions/sess-1/folders", json=BODY).status_code == 409
        assert rows.rows == {} and scheduled == []

    def test_connector_without_an_adapter_is_404(self, build):
        client, *_ = build(providers=[_provider(file_source_adapter_id=None)])
        assert client.post("/sessions/sess-1/folders", json=BODY).status_code == 404

    def test_unknown_connector_is_404(self, build):
        client, *_ = build(providers=[])
        assert client.post("/sessions/sess-1/folders", json=BODY).status_code == 404

    def test_connector_hidden_from_the_user_is_403(self, build):
        client, *_ = build(providers=[_provider(allowed_roles=["faculty"])])
        assert client.post("/sessions/sess-1/folders", json=BODY).status_code == 403

    def test_identity_unavailable_is_503(self, build):
        client, *_ = build(identity_error=WorkloadTokenUnavailableError("no workload"))
        assert client.post("/sessions/sess-1/folders", json=BODY).status_code == 503

    def test_body_is_validated(self, build):
        client, *_ = build()
        assert client.post("/sessions/sess-1/folders", json={"connectorId": "google"}).status_code == 422


class TestGet:
    def test_returns_the_owners_row_for_the_session(self, build):
        rows = _Rows()
        rows.rows[("alice", "fa-1")] = _row(status="partial", error="quota_exceeded",
                                           counts=FolderCounts(listed=3, attached=2, truncated=True),
                                           files=[FolderFileEntry(provider_file_id="d1", name="a.pdf", relative_path="week 1/a.pdf",
                                                                  mime_type="application/pdf", size_bytes=5, upload_id="up-1", status="attached")])
        client, *_ = build(rows=rows)

        body = client.get("/sessions/sess-1/folders/fa-1").json()
        assert body["status"] == "partial" and body["error"] == "quota_exceeded"
        assert body["counts"]["truncated"] is True
        assert body["files"][0] == {"providerFileId": "d1", "name": "a.pdf", "relativePath": "week 1/a.pdf", "mimeType": "application/pdf",
                                    "sizeBytes": 5, "uploadId": "up-1", "status": "attached", "error": None,
                                    "etag": None, "contentHash": None, "removedFromSource": False}
        assert body["stale"] is False

    def test_404_for_a_missing_row_or_another_session(self, build):
        rows = _Rows()
        rows.rows[("alice", "fa-1")] = _row()
        rows.rows[("bob", "fa-2")] = _row(folder_attach_id="fa-2", user_id="bob")
        client, *_ = build(rows=rows)
        assert client.get("/sessions/sess-1/folders/nope").status_code == 404
        assert client.get("/sessions/other/folders/fa-1").status_code == 404
        assert client.get("/sessions/sess-1/folders/fa-2").status_code == 404  # another user's row

    def test_an_expanding_row_gone_quiet_is_stale(self, build):
        rows = _Rows()
        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        rows.rows[("alice", "fa-1")] = _row(updated_at=old)
        rows.rows[("alice", "fa-2")] = _row(folder_attach_id="fa-2", status="ready", updated_at=old)
        client, *_ = build(rows=rows)
        assert client.get("/sessions/sess-1/folders/fa-1").json()["stale"] is True
        assert client.get("/sessions/sess-1/folders/fa-2").json()["stale"] is False


class TestDelete:
    def test_deletes_the_files_through_the_upload_service_then_the_row(self, build):
        rows = _Rows()
        rows.rows[("alice", "fa-1")] = _row(files=[
            FolderFileEntry(provider_file_id="d1", name="a.pdf", relative_path="a.pdf", mime_type="application/pdf", upload_id="up-1", status="attached"),
            FolderFileEntry(provider_file_id="d2", name="b.pdf", relative_path="b.pdf", mime_type="application/pdf", status="failed", error="x"),
            FolderFileEntry(provider_file_id="d3", name="c.pdf", relative_path="c.pdf", mime_type="application/pdf", upload_id="up-3", status="attached"),
        ])
        client, rows, upload_service, _, _ = build(rows=rows)

        response = client.delete("/sessions/sess-1/folders/fa-1")

        assert response.status_code == 204
        assert [c.args for c in upload_service.delete_file.await_args_list] == [("alice", "up-1"), ("alice", "up-3")]
        assert rows.rows == {}

    def test_a_file_delete_failure_does_not_keep_the_row(self, build):
        rows = _Rows()
        rows.rows[("alice", "fa-1")] = _row(files=[FolderFileEntry(provider_file_id="d1", name="a.pdf", relative_path="a.pdf",
                                                                  mime_type="application/pdf", upload_id="up-1", status="attached")])
        client, rows, upload_service, _, _ = build(rows=rows)
        upload_service.delete_file = AsyncMock(side_effect=RuntimeError("s3"))
        assert client.delete("/sessions/sess-1/folders/fa-1").status_code == 204
        assert rows.rows == {}

    def test_404_when_missing(self, build):
        client, *_ = build()
        assert client.delete("/sessions/sess-1/folders/fa-1").status_code == 404


class TestResume:
    def _rows(self) -> _Rows:
        rows = _Rows()
        rows.rows[("alice", "fa-1")] = _row(status="failed", error="needs_consent", files=[
            FolderFileEntry(provider_file_id="d1", name="a.pdf", relative_path="a.pdf", mime_type="application/pdf", upload_id="up-1", status="attached")])
        return rows

    def test_reruns_the_walk_from_the_row(self, build):
        client, rows, _, scheduled, adapter = build(rows=self._rows())

        body = client.post("/sessions/sess-1/folders/fa-1/resume").json()

        assert body["status"] == "expanding" and body["error"] is None
        assert rows.rows[("alice", "fa-1")].status == "expanding"
        folder, deps = scheduled[0]
        assert folder.attached_provider_ids == {"d1"}  # what the walk will skip
        assert deps.adapter is adapter and deps.access_token == "vault-token"

    def test_409_while_this_process_is_still_expanding(self, build):
        client, _, _, scheduled, _ = build(rows=self._rows())
        FolderAttachmentService._running.add("fa-1")
        assert client.post("/sessions/sess-1/folders/fa-1/resume").status_code == 409
        assert scheduled == []

    def test_needs_consent_is_409(self, build):
        client, rows, _, scheduled, _ = build(rows=self._rows(), identity_result=TokenResult(authorization_url="https://auth.example/x"))
        assert client.post("/sessions/sess-1/folders/fa-1/resume").status_code == 409
        assert rows.rows[("alice", "fa-1")].status == "failed" and scheduled == []

    def test_404_when_missing(self, build):
        client, *_ = build()
        assert client.post("/sessions/sess-1/folders/fa-1/resume").status_code == 404


class TestRefreshAndList:
    def _rows(self) -> _Rows:
        rows = _Rows()
        rows.rows[("alice", "fa-1")] = _row(status="ready", files=[
            FolderFileEntry(provider_file_id="d1", name="a.pdf", relative_path="a.pdf", mime_type="application/pdf", upload_id="up-1", status="attached")])
        rows.rows[("alice", "fa-2")] = _row(folder_attach_id="fa-2", session_id="other")
        rows.rows[("bob", "fa-3")] = _row(folder_attach_id="fa-3", user_id="bob")
        return rows

    def test_refresh_marks_the_row_refreshing_and_schedules_the_walk(self, build, monkeypatch):
        refreshed: list = []

        def fake_refresh(folder, deps):  # type: ignore[no-untyped-def]
            refreshed.append((folder, deps))

            async def _done():
                return None

            return _done()

        monkeypatch.setattr(folder_service_module, "refresh_folder", fake_refresh)
        client, rows, _, scheduled, adapter = build(rows=self._rows())

        body = client.post("/sessions/sess-1/folders/fa-1/refresh").json()

        assert body["status"] == "expanding" and body["refreshing"] is True
        assert scheduled == []  # the refresh walk, not a first expansion
        folder, deps = refreshed[0]
        assert folder.folder_attach_id == "fa-1" and deps.adapter is adapter and deps.access_token == "vault-token"

    def test_refresh_is_409_while_running_and_needs_consent(self, build):
        client, *_ = build(rows=self._rows())
        FolderAttachmentService._running.add("fa-1")
        assert client.post("/sessions/sess-1/folders/fa-1/refresh").status_code == 409
        FolderAttachmentService._running.discard("fa-1")
        client, *_ = build(rows=self._rows(), identity_result=TokenResult(authorization_url="https://auth.example/x"))
        assert client.post("/sessions/sess-1/folders/fa-1/refresh").status_code == 409

    def test_refresh_404_for_another_session_or_user(self, build):
        client, *_ = build(rows=self._rows())
        assert client.post("/sessions/sess-1/folders/fa-2/refresh").status_code == 404
        assert client.post("/sessions/sess-1/folders/fa-3/refresh").status_code == 404

    def test_list_returns_this_users_folders_in_this_conversation(self, build):
        client, *_ = build(rows=self._rows())
        body = client.get("/sessions/sess-1/folders").json()
        assert [f["folderAttachId"] for f in body["folders"]] == ["fa-1"]
        assert body["folders"][0]["files"][0]["uploadId"] == "up-1"


class TestRecent:
    def _recent(self, folder_id: str, name: str) -> RecentFolder:
        return RecentFolder(connector_id="google", connector_name="Google Drive", provider_folder_id=folder_id,
                            folder_name=name, folder_path=f"My Drive/{name}", last_attached_at="2026-10-14T00:00:00Z")

    def test_returns_the_callers_recent_folders_and_nothing_of_their_conversations(self, build):
        rows = _Rows()
        rows.recent["alice"] = [self._recent("drive-a", "Syllabi"), self._recent("drive-b", "Contracts")]
        rows.recent["bob"] = [self._recent("drive-z", "Bob's")]
        client, *_ = build(rows=rows)

        response = client.get("/folders/recent")

        assert response.status_code == 200, response.text
        folders = response.json()["folders"]
        assert [f["providerFolderId"] for f in folders] == ["drive-a", "drive-b"]
        assert folders[0] == {
            "connectorId": "google",
            "connectorName": "Google Drive",
            "providerFolderId": "drive-a",
            "folderName": "Syllabi",
            "folderPath": "My Drive/Syllabi",
            "lastAttachedAt": "2026-10-14T00:00:00Z",
        }
        assert rows.recent_calls == [("alice", 8)]

    def test_limit_is_passed_through_and_bounded(self, build):
        rows = _Rows()
        client, *_ = build(rows=rows)
        assert client.get("/folders/recent?limit=3").status_code == 200
        assert rows.recent_calls == [("alice", 3)]
        assert client.get("/folders/recent?limit=0").status_code == 422
        assert client.get("/folders/recent?limit=21").status_code == 422

