"""Tests for POST /assistants/{assistant_id}/documents/import.

The endpoint creates one document record per selected file (with provenance
populated) and schedules a fire-and-forget download task. External boundaries
— assistant ownership, file-source resolution, token resolution, the DynamoDB
write, and the async import task — are patched; we test the gating, the
provenance wiring, and the response shape.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient

from apis.app_api.documents.models import Document
from apis.app_api.documents.routes import router
from apis.shared.auth import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.oauth.provider_repository import get_provider_repository
from apis.shared.rbac.service import get_app_role_service

ROUTES_MODULE = "apis.app_api.documents.routes"
ASSISTANT_ID = "ast-001"
USER_ID = "user-001"


def _owner_resolve(user_id: str = USER_ID):
    """Build a resolve_assistant_permission return value for an owner."""
    return (SimpleNamespace(owner_id=user_id), "owner")


def _make_user(user_id: str = USER_ID) -> User:
    return User(
        user_id=user_id,
        email=f"{user_id}@example.com",
        name="User",
        roles=[],
        raw_token="test-token",
    )


def _make_document(document_id: str, filename: str) -> Document:
    return Document.model_validate(
        {
            "documentId": document_id,
            "assistantId": ASSISTANT_ID,
            "filename": filename,
            "contentType": "application/octet-stream",
            "sizeBytes": 0,
            "s3Key": f"assistants/{ASSISTANT_ID}/documents/{document_id}/{filename}",
            "status": "uploading",
            "createdAt": "2026-05-21T00:00:00Z",
            "updatedAt": "2026-05-21T00:00:00Z",
        }
    )


@pytest.fixture
def app():
    _app = FastAPI()
    _app.include_router(router)
    _app.dependency_overrides[get_current_user_from_session] = lambda: _make_user()
    # resolve_file_source is patched per-test, so the repo/role deps are inert.
    _app.dependency_overrides[get_provider_repository] = lambda: MagicMock()
    _app.dependency_overrides[get_app_role_service] = lambda: MagicMock()
    return _app


def _import_body() -> dict:
    return {
        "connectorId": "google",
        "files": [
            {"fileId": "drive-f1", "name": "report.pdf"},
            {"fileId": "drive-f2", "name": "notes.txt"},
        ],
    }


class TestImportDocuments:
    def test_creates_documents_and_schedules_task(self, app):
        provider = MagicMock()
        provider.provider_id = "google"
        adapter = MagicMock()
        adapter.metadata.key = "google-drive"
        created = [
            _make_document("DOC-1", "report.pdf"),
            _make_document("DOC-2", "notes.txt"),
        ]

        with patch(
            f"{ROUTES_MODULE}._generate_document_id",
            side_effect=["DOC-1", "DOC-2"],
        ), patch(
            f"{ROUTES_MODULE}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=_owner_resolve(),
        ), patch(
            f"{ROUTES_MODULE}.resolve_file_source",
            new_callable=AsyncMock,
            return_value=(provider, adapter),
        ), patch(
            f"{ROUTES_MODULE}.require_file_source_token",
            new_callable=AsyncMock,
            return_value="vault-token",
        ), patch(
            f"{ROUTES_MODULE}.create_document",
            new_callable=AsyncMock,
            side_effect=created,
        ), patch(
            f"{ROUTES_MODULE}.run_import", new_callable=AsyncMock
        ) as mock_run:
            resp = TestClient(app).post(
                f"/assistants/{ASSISTANT_ID}/documents/import", json=_import_body()
            )

        assert resp.status_code == 202
        body = resp.json()
        assert [d["documentId"] for d in body["documents"]] == ["DOC-1", "DOC-2"]
        # The download/stage work is scheduled, not awaited inline.
        mock_run.assert_called_once()
        kwargs = mock_run.call_args.kwargs
        assert kwargs["assistant_id"] == ASSISTANT_ID
        assert kwargs["access_token"] == "vault-token"
        assert kwargs["items"] == [("DOC-1", "drive-f1"), ("DOC-2", "drive-f2")]

    def test_populates_provenance_on_each_document(self, app):
        provider = MagicMock()
        provider.provider_id = "google"
        adapter = MagicMock()
        adapter.metadata.key = "google-drive"

        with patch(
            f"{ROUTES_MODULE}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=_owner_resolve(),
        ), patch(
            f"{ROUTES_MODULE}.resolve_file_source",
            new_callable=AsyncMock,
            return_value=(provider, adapter),
        ), patch(
            f"{ROUTES_MODULE}.require_file_source_token",
            new_callable=AsyncMock,
            return_value="vault-token",
        ), patch(
            f"{ROUTES_MODULE}.create_document",
            new_callable=AsyncMock,
            side_effect=[
                _make_document("DOC-1", "report.pdf"),
                _make_document("DOC-2", "notes.txt"),
            ],
        ) as mock_create, patch(
            f"{ROUTES_MODULE}.run_import", new_callable=AsyncMock
        ):
            resp = TestClient(app).post(
                f"/assistants/{ASSISTANT_ID}/documents/import", json=_import_body()
            )

        assert resp.status_code == 202
        first = mock_create.call_args_list[0].kwargs["provenance"]
        assert first.source_connector_id == "google"
        assert first.source_adapter_key == "google-drive"
        assert first.source_file_id == "drive-f1"
        assert first.imported_by_user_id == USER_ID

    def test_an_import_can_found_a_born_managed_knowledge_base(self, app):
        """An agent's first knowledge can come from an import, so it starts born-managed
        provisioning, and every file waits on the knowledge base being built."""
        provider = MagicMock()
        provider.provider_id = "google"
        adapter = MagicMock()
        adapter.metadata.key = "google-drive"

        with patch(
            f"{ROUTES_MODULE}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=_owner_resolve("owner-1"),
        ), patch(
            f"{ROUTES_MODULE}.resolve_file_source",
            new_callable=AsyncMock,
            return_value=(provider, adapter),
        ), patch(
            f"{ROUTES_MODULE}.require_file_source_token",
            new_callable=AsyncMock,
            return_value="vault-token",
        ), patch(
            f"{ROUTES_MODULE}.initial_document_status",
            new_callable=AsyncMock,
            return_value="provisioning",
        ) as initial, patch(
            f"{ROUTES_MODULE}.create_document",
            new_callable=AsyncMock,
            side_effect=[
                _make_document("DOC-1", "report.pdf"),
                _make_document("DOC-2", "notes.txt"),
            ],
        ) as mock_create, patch(
            f"{ROUTES_MODULE}.run_import", new_callable=AsyncMock
        ):
            resp = TestClient(app).post(
                f"/assistants/{ASSISTANT_ID}/documents/import", json=_import_body()
            )

        assert resp.status_code == 202
        initial.assert_awaited_once_with(ASSISTANT_ID, owner_user_id="owner-1")
        assert [c.kwargs["status"] for c in mock_create.call_args_list] == ["provisioning", "provisioning"]

    def test_404_when_assistant_not_owned(self, app):
        # Caller is neither owner nor editor — resolve returns (assistant, None)
        # which our _require_edit_permission helper maps to 403.
        not_permitted = (SimpleNamespace(owner_id="someone-else"), None)
        with patch(
            f"{ROUTES_MODULE}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=not_permitted,
        ), patch(
            f"{ROUTES_MODULE}.create_document", new_callable=AsyncMock
        ) as mock_create:
            resp = TestClient(app).post(
                f"/assistants/{ASSISTANT_ID}/documents/import", json=_import_body()
            )

        assert resp.status_code == 403
        mock_create.assert_not_called()

    def test_404_when_assistant_not_found(self, app):
        with patch(
            f"{ROUTES_MODULE}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=(None, None),
        ), patch(
            f"{ROUTES_MODULE}.create_document", new_callable=AsyncMock
        ) as mock_create:
            resp = TestClient(app).post(
                f"/assistants/{ASSISTANT_ID}/documents/import", json=_import_body()
            )

        assert resp.status_code == 404
        mock_create.assert_not_called()

    def test_409_propagates_when_connector_not_connected(self, app):
        provider = MagicMock()
        provider.provider_id = "google"

        with patch(
            f"{ROUTES_MODULE}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=_owner_resolve(),
        ), patch(
            f"{ROUTES_MODULE}.resolve_file_source",
            new_callable=AsyncMock,
            return_value=(provider, MagicMock()),
        ), patch(
            f"{ROUTES_MODULE}.require_file_source_token",
            new_callable=AsyncMock,
            side_effect=HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="not connected"
            ),
        ), patch(
            f"{ROUTES_MODULE}.create_document", new_callable=AsyncMock
        ) as mock_create:
            resp = TestClient(app).post(
                f"/assistants/{ASSISTANT_ID}/documents/import", json=_import_body()
            )

        assert resp.status_code == 409
        mock_create.assert_not_called()

    def test_404_propagates_when_not_a_file_source(self, app):
        with patch(
            f"{ROUTES_MODULE}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=_owner_resolve(),
        ), patch(
            f"{ROUTES_MODULE}.resolve_file_source",
            new_callable=AsyncMock,
            side_effect=HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="not a file source"
            ),
        ):
            resp = TestClient(app).post(
                f"/assistants/{ASSISTANT_ID}/documents/import", json=_import_body()
            )

        assert resp.status_code == 404

    def test_422_when_files_empty(self, app):
        resp = TestClient(app).post(
            f"/assistants/{ASSISTANT_ID}/documents/import",
            json={"connectorId": "google", "files": []},
        )
        assert resp.status_code == 422


# --------------------------------------------------------------------------- import-folder
def _walked(file_id: str, name: str):
    from apis.app_api.file_sources.folder_walk import WalkedFile

    return WalkedFile(entry=SimpleNamespace(id=file_id, name=name), relative_path=name, mime_type="application/pdf")


def _held(file_id: str, connector: str = "google") -> SimpleNamespace:
    return SimpleNamespace(source_file_id=file_id, source_connector_id=connector)


class TestImportFolder:
    def _patches(self, *, walk, held=(), created=()):
        from contextlib import ExitStack

        provider = MagicMock()
        provider.provider_id = "google"
        adapter = MagicMock()
        adapter.metadata.key = "google-drive"
        stack = ExitStack()
        stack.enter_context(patch(f"{ROUTES_MODULE}.resolve_assistant_permission", new_callable=AsyncMock, return_value=_owner_resolve()))
        stack.enter_context(patch(f"{ROUTES_MODULE}.resolve_file_source", new_callable=AsyncMock, return_value=(provider, adapter)))
        stack.enter_context(patch(f"{ROUTES_MODULE}.require_file_source_token", new_callable=AsyncMock, return_value="vault-token"))
        stack.enter_context(patch(f"{ROUTES_MODULE}.list_assistant_documents", new_callable=AsyncMock, return_value=(list(held), None)))
        walk_mock = stack.enter_context(patch(f"{ROUTES_MODULE}.walk_folder", new_callable=AsyncMock, return_value=walk))
        stack.enter_context(patch(f"{ROUTES_MODULE}._generate_document_id", side_effect=[d.document_id for d in created]))
        stack.enter_context(patch(f"{ROUTES_MODULE}.initial_document_status", new_callable=AsyncMock, return_value="uploading"))
        stack.enter_context(patch(f"{ROUTES_MODULE}._import_max_file_bytes", new_callable=AsyncMock, return_value=None))
        stack.enter_context(patch(f"{ROUTES_MODULE}.create_document", new_callable=AsyncMock, side_effect=list(created)))
        run = stack.enter_context(patch(f"{ROUTES_MODULE}.run_import", new_callable=AsyncMock))
        return stack, walk_mock, run

    def test_imports_every_walked_file_and_skips_what_is_already_held(self, app):
        from apis.app_api.file_sources.folder_walk import FolderWalk

        walk = FolderWalk(files=[_walked("f1", "a.pdf"), _walked("f2", "b.pdf")], listed=4, skipped_type=1)
        created = [_make_document("DOC-1", "a.pdf"), _make_document("DOC-2", "b.pdf")]
        stack, walk_mock, run = self._patches(walk=walk, held=[_held("f0"), _held("other", "dropbox")], created=created)
        with stack:
            resp = TestClient(app).post(
                f"/assistants/{ASSISTANT_ID}/documents/import-folder",
                json={"connectorId": "google", "folderId": "fold-1"},
            )

        assert resp.status_code == 202
        body = resp.json()
        assert [d["documentId"] for d in body["documents"]] == ["DOC-1", "DOC-2"]
        assert body["listed"] == 4
        assert body["skippedType"] == 1
        assert body["alreadyImported"] == 1
        assert body["truncated"] is False
        # Only this connector's files are skipped: a same-id file from another source is not.
        assert walk_mock.call_args.kwargs["skip_ids"] == {"f0"}
        assert walk_mock.call_args.args[2] == "fold-1"
        assert run.call_args.kwargs["items"] == [("DOC-1", "f1"), ("DOC-2", "f2")]

    def test_an_empty_folder_creates_nothing_and_schedules_nothing(self, app):
        from apis.app_api.file_sources.folder_walk import FolderWalk

        stack, _, run = self._patches(walk=FolderWalk(listed=0))
        with stack:
            resp = TestClient(app).post(
                f"/assistants/{ASSISTANT_ID}/documents/import-folder",
                json={"connectorId": "google", "folderId": "fold-1"},
            )
        assert resp.status_code == 202
        assert resp.json()["documents"] == []
        run.assert_not_called()

    @pytest.mark.parametrize(
        "error,expected",
        [("auth", 409), ("not_found", 404), ("failed", 502)],
    )
    def test_a_failed_walk_imports_nothing(self, app, error, expected):
        from apis.app_api.file_sources.folder_walk import FolderWalk

        stack, _, run = self._patches(walk=FolderWalk(error=error))
        with stack:
            resp = TestClient(app).post(
                f"/assistants/{ASSISTANT_ID}/documents/import-folder",
                json={"connectorId": "google", "folderId": "fold-1"},
            )
        assert resp.status_code == expected
        run.assert_not_called()
