"""Tests for sync-policy routes (KB sync — scheduled re-index).

Endpoints under test (all edit-gated: owner or editor share):
- POST   /assistants/{id}/sync-policies            → create (201; 404/400 source checks; 409 dup; 400 cap)
- GET    /assistants/{id}/sync-policies            → list (200)
- PATCH  /assistants/{id}/sync-policies/{pid}      → interval / pause / resume (409 for reauth pause)
- DELETE /assistants/{id}/sync-policies/{pid}      → delete + source-specific cleanup (204)
- POST   /assistants/{id}/sync-policies/{pid}/run-now → manual sync (202; 429 cooldown)

The service layer is mocked — its DynamoDB semantics are covered by
tests/shared/test_sync_policies.py. These tests pin the HTTP contract:
status codes, permission gate, source validation, and cleanup fan-out.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.sync_policies.routes import router
from apis.shared.auth import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.sync_policies.models import SyncPolicy
from apis.shared.sync_policies.service import (
    DuplicateSyncPolicy,
    RunNowCooldown,
    SyncPolicyLimitExceeded,
)
from tests.routes.conftest import mock_no_auth

ROUTES_MODULE = "apis.app_api.sync_policies.routes"
ASSISTANT_ID = "ast-001"
USER_ID = "user-001"
POLICY_ID = "syn-abc123def456"
NOW = "2026-07-03T00:00:00+00:00Z"


def _make_policy(**overrides) -> SyncPolicy:
    defaults = dict(
        policyId=POLICY_ID,
        assistantId=ASSISTANT_ID,
        sourceType="drive_file",
        sourceRef="doc-001",
        interval="daily",
        state="active",
        nextSyncAt=NOW,
        createdByUserId=USER_ID,
        createdAt=NOW,
        updatedAt=NOW,
    )
    defaults.update(overrides)
    return SyncPolicy.model_validate(defaults)


@pytest.fixture
def app():
    """Minimal FastAPI app mounting only the sync-policies router."""
    _app = FastAPI()
    _app.include_router(router)
    return _app


def _override_user(app: FastAPI, user_id: str = USER_ID) -> None:
    app.dependency_overrides[get_current_user_from_session] = lambda: User(
        user_id=user_id, email=f"{user_id}@example.com", name="Test User", roles=["User"]
    )


def _resolve(permission):
    """Build a resolve_assistant_permission return value."""
    assistant = SimpleNamespace(owner_id=USER_ID) if permission else None
    return AsyncMock(return_value=(assistant, permission))


DRIVE_SOURCE = {"status": "complete", "sourceFileId": "drive-file-1"}
CRAWL_SOURCE = {"status": "complete"}


class TestPermissionGate:
    """Every route shares the owner/editor gate — exercised through create."""

    def test_unauthenticated_401(self, app):
        mock_no_auth(app)
        response = TestClient(app).get(f"/assistants/{ASSISTANT_ID}/sync-policies")
        assert response.status_code == 401

    def test_unknown_assistant_404(self, app):
        _override_user(app)
        with patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve(None)):
            response = TestClient(app).get(f"/assistants/{ASSISTANT_ID}/sync-policies")
        assert response.status_code == 404

    def test_viewer_share_403(self, app):
        _override_user(app)
        with patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("viewer")):
            response = TestClient(app).post(
                f"/assistants/{ASSISTANT_ID}/sync-policies",
                json={"sourceType": "drive_file", "sourceRef": "doc-001", "interval": "daily"},
            )
        assert response.status_code == 403

    def test_editor_share_allowed(self, app):
        _override_user(app, user_id="editor-user")
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("editor")),
            patch(f"{ROUTES_MODULE}.list_sync_policies", AsyncMock(return_value=[])),
        ):
            response = TestClient(app).get(f"/assistants/{ASSISTANT_ID}/sync-policies")
        assert response.status_code == 200


class TestCreatePolicy:
    def _post(self, app, source_type="drive_file", source_ref="doc-001", interval="daily"):
        return TestClient(app).post(
            f"/assistants/{ASSISTANT_ID}/sync-policies",
            json={"sourceType": source_type, "sourceRef": source_ref, "interval": interval},
        )

    def test_create_drive_file_201_and_backpointer(self, app):
        _override_user(app)
        policy = _make_policy()
        update_fields = MagicMock()
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{ROUTES_MODULE}.records.get_source_item", return_value=DRIVE_SOURCE),
            patch(f"{ROUTES_MODULE}.create_sync_policy", AsyncMock(return_value=policy)) as create,
            patch(f"{ROUTES_MODULE}.records.update_document_sync_fields", update_fields),
        ):
            response = self._post(app)

        assert response.status_code == 201
        body = response.json()
        assert body["policyId"] == POLICY_ID
        assert body["sourceType"] == "drive_file"
        assert body["state"] == "active"
        create.assert_awaited_once_with(
            assistant_id=ASSISTANT_ID,
            source_type="drive_file",
            source_ref="doc-001",
            interval="daily",
            created_by_user_id=USER_ID,
        )
        update_fields.assert_called_once_with(ASSISTANT_ID, "doc-001", sync_policy_id=POLICY_ID)

    def test_create_web_crawl_writes_no_document_backpointer(self, app):
        _override_user(app)
        policy = _make_policy(sourceType="web_crawl", sourceRef="crawl-001")
        update_fields = MagicMock()
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{ROUTES_MODULE}.records.get_source_item", return_value=CRAWL_SOURCE),
            patch(f"{ROUTES_MODULE}.create_sync_policy", AsyncMock(return_value=policy)) as create,
            patch(f"{ROUTES_MODULE}.records.update_document_sync_fields", update_fields),
        ):
            response = self._post(app, source_type="web_crawl", source_ref="crawl-001")

        assert response.status_code == 201
        update_fields.assert_not_called()
        # Only a folder's first run is due at creation; a crawl waits one interval.
        assert "due_now" not in create.await_args.kwargs

    def test_missing_source_404(self, app):
        _override_user(app)
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{ROUTES_MODULE}.records.get_source_item", return_value=None),
        ):
            response = self._post(app)
        assert response.status_code == 404

    def test_deleting_source_404(self, app):
        _override_user(app)
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{ROUTES_MODULE}.records.get_source_item", return_value={"status": "deleting"}),
        ):
            response = self._post(app)
        assert response.status_code == 404

    def test_device_uploaded_document_400(self, app):
        """No import provenance → nothing external to sync from."""
        _override_user(app)
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{ROUTES_MODULE}.records.get_source_item", return_value={"status": "complete"}),
        ):
            response = self._post(app)
        assert response.status_code == 400

    def test_duplicate_policy_409(self, app):
        _override_user(app)
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{ROUTES_MODULE}.records.get_source_item", return_value=DRIVE_SOURCE),
            patch(f"{ROUTES_MODULE}.create_sync_policy", AsyncMock(side_effect=DuplicateSyncPolicy("doc-001"))),
        ):
            response = self._post(app)
        assert response.status_code == 409

    def test_policy_cap_400(self, app):
        _override_user(app)
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{ROUTES_MODULE}.records.get_source_item", return_value=DRIVE_SOURCE),
            patch(
                f"{ROUTES_MODULE}.create_sync_policy",
                AsyncMock(side_effect=SyncPolicyLimitExceeded("limit of 10 reached")),
            ),
        ):
            response = self._post(app)
        assert response.status_code == 400

    def test_invalid_interval_422(self, app):
        _override_user(app)
        with patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")):
            response = self._post(app, interval="hourly")
        assert response.status_code == 422


class TestListPolicies:
    def test_list_200_camel_case(self, app):
        _override_user(app)
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(
                f"{ROUTES_MODULE}.list_sync_policies",
                AsyncMock(return_value=[_make_policy(), _make_policy(policyId="syn-2", sourceRef="doc-2")]),
            ),
        ):
            response = TestClient(app).get(f"/assistants/{ASSISTANT_ID}/sync-policies")

        assert response.status_code == 200
        body = response.json()
        assert [p["policyId"] for p in body["policies"]] == [POLICY_ID, "syn-2"]
        assert body["policies"][0]["nextSyncAt"] == NOW


class TestUpdatePolicy:
    def _patch(self, app, body):
        return TestClient(app).patch(f"/assistants/{ASSISTANT_ID}/sync-policies/{POLICY_ID}", json=body)

    def test_interval_change(self, app):
        _override_user(app)
        changed = _make_policy(interval="weekly")
        change = AsyncMock(return_value=changed)
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{ROUTES_MODULE}.get_sync_policy", AsyncMock(return_value=_make_policy())),
            patch(f"{ROUTES_MODULE}.change_policy_interval", change),
        ):
            response = self._patch(app, {"interval": "weekly"})

        assert response.status_code == 200
        assert response.json()["interval"] == "weekly"
        change.assert_awaited_once_with(ASSISTANT_ID, POLICY_ID, "weekly")

    def test_same_interval_is_noop(self, app):
        _override_user(app)
        change = AsyncMock()
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{ROUTES_MODULE}.get_sync_policy", AsyncMock(return_value=_make_policy())),
            patch(f"{ROUTES_MODULE}.change_policy_interval", change),
        ):
            response = self._patch(app, {"interval": "daily"})

        assert response.status_code == 200
        change.assert_not_awaited()

    def test_pause(self, app):
        _override_user(app)
        set_state = AsyncMock(return_value=True)
        paused = _make_policy(state="paused_user", stateReason="Paused by user")
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{ROUTES_MODULE}.get_sync_policy", AsyncMock(side_effect=[_make_policy(), paused])),
            patch(f"{ROUTES_MODULE}.set_policy_state", set_state),
        ):
            response = self._patch(app, {"state": "paused_user"})

        assert response.status_code == 200
        assert response.json()["state"] == "paused_user"
        set_state.assert_awaited_once_with(
            ASSISTANT_ID, POLICY_ID, "paused_user", state_reason="Paused by user"
        )

    def test_resume_comes_due_immediately(self, app):
        _override_user(app)
        set_state = AsyncMock(return_value=True)
        paused = _make_policy(state="paused_user")
        resumed = _make_policy(state="active")
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{ROUTES_MODULE}.get_sync_policy", AsyncMock(side_effect=[paused, resumed])),
            patch(f"{ROUTES_MODULE}.set_policy_state", set_state),
        ):
            response = self._patch(app, {"state": "active"})

        assert response.status_code == 200
        assert response.json()["state"] == "active"
        # Resume re-arms due-now, not one interval out
        args, kwargs = set_state.await_args
        assert args == (ASSISTANT_ID, POLICY_ID, "active")
        assert kwargs["next_sync_at"] is not None

    def test_resume_of_reauth_pause_409(self, app):
        """Only a fresh OAuth consent resumes a reauth pause."""
        _override_user(app)
        set_state = AsyncMock()
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{ROUTES_MODULE}.get_sync_policy", AsyncMock(return_value=_make_policy(state="paused_reauth"))),
            patch(f"{ROUTES_MODULE}.set_policy_state", set_state),
        ):
            response = self._patch(app, {"state": "active"})

        assert response.status_code == 409
        set_state.assert_not_awaited()

    def test_pause_of_reauth_pause_allowed(self, app):
        """A user may still explicitly park a reauth-paused policy."""
        _override_user(app)
        set_state = AsyncMock(return_value=True)
        reauth = _make_policy(state="paused_reauth")
        parked = _make_policy(state="paused_user", stateReason="Paused by user")
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{ROUTES_MODULE}.get_sync_policy", AsyncMock(side_effect=[reauth, parked])),
            patch(f"{ROUTES_MODULE}.set_policy_state", set_state),
        ):
            response = self._patch(app, {"state": "paused_user"})

        assert response.status_code == 200

    def test_invalid_state_422(self, app):
        """paused_reauth / paused_error / paused_inactive are system-owned."""
        _override_user(app)
        with patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")):
            response = self._patch(app, {"state": "paused_reauth"})
        assert response.status_code == 422

    def test_missing_policy_404(self, app):
        _override_user(app)
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{ROUTES_MODULE}.get_sync_policy", AsyncMock(return_value=None)),
        ):
            response = self._patch(app, {"interval": "weekly"})
        assert response.status_code == 404


class TestDeletePolicy:
    def _delete(self, app):
        return TestClient(app).delete(f"/assistants/{ASSISTANT_ID}/sync-policies/{POLICY_ID}")

    def test_delete_drive_file_clears_backpointer(self, app):
        _override_user(app)
        delete_policy = AsyncMock(return_value=True)
        delete_marker = AsyncMock()
        clear_backpointer = MagicMock()
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{ROUTES_MODULE}.get_sync_policy", AsyncMock(return_value=_make_policy())),
            patch(f"{ROUTES_MODULE}.delete_sync_policy", delete_policy),
            patch(f"{ROUTES_MODULE}.delete_reauth_marker", delete_marker),
            patch(f"{ROUTES_MODULE}.records.clear_document_sync_policy_id", clear_backpointer),
        ):
            response = self._delete(app)

        assert response.status_code == 204
        delete_policy.assert_awaited_once_with(ASSISTANT_ID, POLICY_ID)
        delete_marker.assert_awaited_once_with(USER_ID, POLICY_ID)
        clear_backpointer.assert_called_once_with(ASSISTANT_ID, "doc-001")

    def test_delete_web_crawl_restores_job_ttl(self, app):
        _override_user(app)
        policy = _make_policy(sourceType="web_crawl", sourceRef="crawl-001")
        restore_ttl = AsyncMock()
        clear_backpointer = MagicMock()
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{ROUTES_MODULE}.get_sync_policy", AsyncMock(return_value=policy)),
            patch(f"{ROUTES_MODULE}.delete_sync_policy", AsyncMock(return_value=True)),
            patch(f"{ROUTES_MODULE}.delete_reauth_marker", AsyncMock()),
            patch("apis.app_api.web_sources.crawl_repository.restore_crawl_ttl", restore_ttl),
            patch(f"{ROUTES_MODULE}.records.clear_document_sync_policy_id", clear_backpointer),
        ):
            response = self._delete(app)

        assert response.status_code == 204
        restore_ttl.assert_awaited_once_with(assistant_id=ASSISTANT_ID, crawl_id="crawl-001")
        clear_backpointer.assert_not_called()

    def test_missing_policy_404(self, app):
        _override_user(app)
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{ROUTES_MODULE}.get_sync_policy", AsyncMock(return_value=None)),
        ):
            response = self._delete(app)
        assert response.status_code == 404


class TestRunNow:
    def _post(self, app):
        return TestClient(app).post(f"/assistants/{ASSISTANT_ID}/sync-policies/{POLICY_ID}/run-now")

    def test_run_now_202(self, app):
        _override_user(app)
        triggered = _make_policy(lastManualRunAt=NOW)
        trigger = AsyncMock(return_value=triggered)
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{ROUTES_MODULE}.trigger_run_now", trigger),
        ):
            response = self._post(app)

        assert response.status_code == 202
        assert response.json()["policyId"] == POLICY_ID
        trigger.assert_awaited_once_with(ASSISTANT_ID, POLICY_ID)

    def test_missing_policy_404(self, app):
        _override_user(app)
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{ROUTES_MODULE}.trigger_run_now", AsyncMock(side_effect=KeyError(POLICY_ID))),
        ):
            response = self._post(app)
        assert response.status_code == 404

    def test_paused_policy_409(self, app):
        _override_user(app)
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(
                f"{ROUTES_MODULE}.trigger_run_now",
                AsyncMock(side_effect=ValueError("Cannot run-now a policy in state paused_user")),
            ),
        ):
            response = self._post(app)
        assert response.status_code == 409

    def test_cooldown_429(self, app):
        _override_user(app)
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{ROUTES_MODULE}.trigger_run_now", AsyncMock(side_effect=RunNowCooldown(POLICY_ID))),
        ):
            response = self._post(app)
        assert response.status_code == 429


FOLDER_REF = "google-workspace:folder-abc"
FILE_SOURCE_MODULE = "apis.app_api.file_sources.service"


class TestFolderPolicy:
    """``drive_folder`` policies (assistant-kb-sync.md §11): one per folder,
    behind FOLDER_SYNC_ENABLED, checked against the caller's file source."""

    def _post(self, app, source_ref=FOLDER_REF, **extra):
        return TestClient(app).post(
            f"/assistants/{ASSISTANT_ID}/sync-policies",
            json={"sourceType": "drive_folder", "sourceRef": source_ref, "interval": "weekly", **extra},
        )

    @pytest.fixture
    def folder_sync_on(self, monkeypatch):
        monkeypatch.setenv("FOLDER_SYNC_ENABLED", "true")

    @pytest.fixture
    def file_source_ok(self):
        resolve = AsyncMock(return_value=(SimpleNamespace(provider_id="google-workspace"), SimpleNamespace()))
        with (
            patch(f"{FILE_SOURCE_MODULE}.resolve_file_source", resolve),
            patch("apis.shared.oauth.provider_repository.get_provider_repository", MagicMock()),
            patch("apis.shared.rbac.service.get_app_role_service", MagicMock()),
        ):
            yield resolve

    @pytest.mark.parametrize("value", ["false", " FALSE "])
    def test_flag_off_400(self, app, monkeypatch, value):
        _override_user(app)
        monkeypatch.setenv("FOLDER_SYNC_ENABLED", value)
        create = AsyncMock()
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{ROUTES_MODULE}.create_sync_policy", create),
        ):
            response = self._post(app)
        assert response.status_code == 400
        create.assert_not_awaited()

    def test_flag_off_still_authenticates_first(self, app, monkeypatch):
        monkeypatch.setenv("FOLDER_SYNC_ENABLED", "false")
        mock_no_auth(app)
        assert self._post(app).status_code == 401

    @pytest.mark.parametrize("ref", ["no-separator", ":folder-only", "connector-only:"])
    def test_malformed_ref_400(self, app, folder_sync_on, file_source_ok, ref):
        _override_user(app)
        with patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")):
            response = self._post(app, source_ref=ref)
        assert response.status_code == 400
        file_source_ok.assert_not_awaited()

    def test_connector_the_caller_cannot_use_is_refused(self, app, folder_sync_on):
        from fastapi import HTTPException

        _override_user(app)
        denied = AsyncMock(side_effect=HTTPException(status_code=403, detail="no access"))
        create = AsyncMock()
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{FILE_SOURCE_MODULE}.resolve_file_source", denied),
            patch("apis.shared.oauth.provider_repository.get_provider_repository", MagicMock()),
            patch("apis.shared.rbac.service.get_app_role_service", MagicMock()),
            patch(f"{ROUTES_MODULE}.create_sync_policy", create),
        ):
            response = self._post(app)
        assert response.status_code == 403
        create.assert_not_awaited()

    def test_create_201_with_folder_name_and_no_source_record(self, app, folder_sync_on, file_source_ok):
        _override_user(app)
        policy = _make_policy(sourceType="drive_folder", sourceRef=FOLDER_REF, sourceName="Readings/Week 3")
        get_source = MagicMock()
        update_fields = MagicMock()
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("editor")),
            patch(f"{ROUTES_MODULE}.records.get_source_item", get_source),
            patch(f"{ROUTES_MODULE}.records.update_document_sync_fields", update_fields),
            patch(f"{ROUTES_MODULE}.create_sync_policy", AsyncMock(return_value=policy)) as create,
        ):
            response = self._post(app, sourceName="Readings/Week 3")

        assert response.status_code == 201
        body = response.json()
        assert body["sourceType"] == "drive_folder"
        assert body["sourceName"] == "Readings/Week 3"
        create.assert_awaited_once_with(
            assistant_id=ASSISTANT_ID,
            source_type="drive_folder",
            source_ref=FOLDER_REF,
            interval="weekly",
            created_by_user_id=USER_ID,
            source_name="Readings/Week 3",
            due_now=True,
        )
        assert file_source_ok.await_args.args[0] == "google-workspace"
        get_source.assert_not_called()
        update_fields.assert_not_called()

    def test_list_carries_the_last_run_summary(self, app):
        _override_user(app)
        policy = _make_policy(
            sourceType="drive_folder",
            sourceRef=FOLDER_REF,
            lastRunSummary={"listed": 12, "updated": 2, "added": 1, "removedFromSource": 1},
        )
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{ROUTES_MODULE}.list_sync_policies", AsyncMock(return_value=[policy])),
        ):
            response = TestClient(app).get(f"/assistants/{ASSISTANT_ID}/sync-policies")
        summary = response.json()["policies"][0]["lastRunSummary"]
        assert summary["listed"] == 12
        assert summary["removedFromSource"] == 1
        assert summary["deferred"] == 0

    def test_drive_file_policy_on_a_folder_covered_document_409(self, app):
        _override_user(app)
        create = AsyncMock()
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(
                f"{ROUTES_MODULE}.records.get_source_item",
                return_value={**DRIVE_SOURCE, "syncPolicyId": "syn-folder"},
            ),
            patch(f"{ROUTES_MODULE}.create_sync_policy", create),
        ):
            response = TestClient(app).post(
                f"/assistants/{ASSISTANT_ID}/sync-policies",
                json={"sourceType": "drive_file", "sourceRef": "doc-001", "interval": "daily"},
            )
        assert response.status_code == 409
        create.assert_not_awaited()

    def test_delete_clears_back_pointers_and_keeps_documents(self, app):
        _override_user(app)
        policy = _make_policy(sourceType="drive_folder", sourceRef=FOLDER_REF)
        clear_folder = MagicMock(return_value=3)
        clear_one = MagicMock()
        with (
            patch(f"{ROUTES_MODULE}.resolve_assistant_permission", _resolve("owner")),
            patch(f"{ROUTES_MODULE}.get_sync_policy", AsyncMock(return_value=policy)),
            patch(f"{ROUTES_MODULE}.delete_sync_policy", AsyncMock(return_value=True)),
            patch(f"{ROUTES_MODULE}.delete_reauth_marker", AsyncMock()),
            patch(f"{ROUTES_MODULE}.records.clear_sync_policy_back_pointers", clear_folder),
            patch(f"{ROUTES_MODULE}.records.clear_document_sync_policy_id", clear_one),
        ):
            response = TestClient(app).delete(f"/assistants/{ASSISTANT_ID}/sync-policies/{POLICY_ID}")

        assert response.status_code == 204
        clear_folder.assert_called_once_with(ASSISTANT_ID, POLICY_ID)
        clear_one.assert_not_called()
