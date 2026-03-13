"""Route tests for admin fine-tuning access management endpoints."""

import pytest
import os
from unittest.mock import MagicMock, patch
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient

from apis.shared.auth.models import User
from apis.shared.auth.dependencies import get_current_user
from apis.shared.auth.rbac import require_admin
from apis.app_api.fine_tuning.repository import FineTuningAccessRepository


def _create_app():
    """Create a minimal FastAPI app with the admin fine-tuning router."""
    from apis.app_api.admin.fine_tuning.routes import router, get_repository
    app = FastAPI()

    # Mount the router under /admin prefix (like the real app)
    from fastapi import APIRouter
    admin_router = APIRouter(prefix="/admin")
    admin_router.include_router(router)
    app.include_router(admin_router)

    return app


def _override_auth(app: FastAPI, user: User):
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user


def _override_repo(app: FastAPI, repo: MagicMock):
    from apis.app_api.admin.fine_tuning.routes import get_repository
    app.dependency_overrides[get_repository] = lambda: repo


def _override_jobs_repo(app: FastAPI, jobs_repo: MagicMock):
    from apis.app_api.admin.fine_tuning.routes import get_jobs_repository
    app.dependency_overrides[get_jobs_repository] = lambda: jobs_repo


def _override_inf_repo(app: FastAPI, inf_repo: MagicMock):
    from apis.app_api.admin.fine_tuning.routes import get_inf_repository
    app.dependency_overrides[get_inf_repository] = lambda: inf_repo


SAMPLE_GRANT = {
    "email": "user@example.com",
    "granted_by": "admin@example.com",
    "granted_at": "2026-01-01T00:00:00Z",
    "monthly_quota_hours": 10.0,
    "current_month_usage_hours": 2.0,
    "quota_period": "2026-03",
}


class TestListAccess:

    def test_returns_200_with_grants(self, make_user):
        app = _create_app()
        admin = make_user(email="admin@example.com", roles=["Admin"])
        _override_auth(app, admin)

        mock_repo = MagicMock()
        mock_repo.list_access.return_value = [SAMPLE_GRANT]
        _override_repo(app, mock_repo)

        client = TestClient(app)
        resp = client.get("/admin/fine-tuning/access")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 1
        assert body["grants"][0]["email"] == "user@example.com"

    def test_requires_admin_role(self):
        app = _create_app()

        def _raise_403():
            raise HTTPException(status_code=403, detail="Forbidden")
        app.dependency_overrides[require_admin] = _raise_403

        client = TestClient(app)
        resp = client.get("/admin/fine-tuning/access")
        assert resp.status_code == 403


class TestGrantAccess:

    def test_returns_201_with_new_grant(self, make_user):
        app = _create_app()
        admin = make_user(email="admin@example.com", roles=["Admin"])
        _override_auth(app, admin)

        mock_repo = MagicMock()
        mock_repo.grant_access.return_value = SAMPLE_GRANT
        _override_repo(app, mock_repo)

        client = TestClient(app)
        resp = client.post(
            "/admin/fine-tuning/access",
            json={"email": "user@example.com", "monthly_quota_hours": 10.0},
        )

        assert resp.status_code == 201
        assert resp.json()["email"] == "user@example.com"

    def test_returns_400_for_duplicate_email(self, make_user):
        app = _create_app()
        admin = make_user(email="admin@example.com", roles=["Admin"])
        _override_auth(app, admin)

        mock_repo = MagicMock()
        mock_repo.grant_access.side_effect = ValueError("Access already granted")
        _override_repo(app, mock_repo)

        client = TestClient(app)
        resp = client.post(
            "/admin/fine-tuning/access",
            json={"email": "dup@example.com"},
        )

        assert resp.status_code == 400
        assert "already granted" in resp.json()["detail"]

    def test_requires_admin_role(self):
        app = _create_app()

        def _raise_403():
            raise HTTPException(status_code=403, detail="Forbidden")
        app.dependency_overrides[require_admin] = _raise_403

        client = TestClient(app)
        resp = client.post(
            "/admin/fine-tuning/access",
            json={"email": "user@example.com"},
        )
        assert resp.status_code == 403


class TestGetAccess:

    def test_returns_200_for_existing(self, make_user):
        app = _create_app()
        admin = make_user(email="admin@example.com", roles=["Admin"])
        _override_auth(app, admin)

        mock_repo = MagicMock()
        mock_repo.get_access.return_value = SAMPLE_GRANT
        _override_repo(app, mock_repo)

        client = TestClient(app)
        resp = client.get("/admin/fine-tuning/access/user@example.com")

        assert resp.status_code == 200
        assert resp.json()["email"] == "user@example.com"

    def test_returns_404_for_nonexistent(self, make_user):
        app = _create_app()
        admin = make_user(email="admin@example.com", roles=["Admin"])
        _override_auth(app, admin)

        mock_repo = MagicMock()
        mock_repo.get_access.return_value = None
        _override_repo(app, mock_repo)

        client = TestClient(app)
        resp = client.get("/admin/fine-tuning/access/nobody@example.com")

        assert resp.status_code == 404


class TestUpdateQuota:

    def test_returns_200_with_updated_grant(self, make_user):
        app = _create_app()
        admin = make_user(email="admin@example.com", roles=["Admin"])
        _override_auth(app, admin)

        updated = {**SAMPLE_GRANT, "monthly_quota_hours": 50.0}
        mock_repo = MagicMock()
        mock_repo.update_quota.return_value = updated
        _override_repo(app, mock_repo)

        client = TestClient(app)
        resp = client.put(
            "/admin/fine-tuning/access/user@example.com",
            json={"monthly_quota_hours": 50.0},
        )

        assert resp.status_code == 200
        assert resp.json()["monthly_quota_hours"] == 50.0

    def test_returns_404_for_nonexistent(self, make_user):
        app = _create_app()
        admin = make_user(email="admin@example.com", roles=["Admin"])
        _override_auth(app, admin)

        mock_repo = MagicMock()
        mock_repo.update_quota.return_value = None
        _override_repo(app, mock_repo)

        client = TestClient(app)
        resp = client.put(
            "/admin/fine-tuning/access/nobody@example.com",
            json={"monthly_quota_hours": 50.0},
        )

        assert resp.status_code == 404


class TestRevokeAccess:

    def test_returns_204_on_success(self, make_user):
        app = _create_app()
        admin = make_user(email="admin@example.com", roles=["Admin"])
        _override_auth(app, admin)

        mock_repo = MagicMock()
        mock_repo.revoke_access.return_value = True
        _override_repo(app, mock_repo)

        client = TestClient(app)
        resp = client.delete("/admin/fine-tuning/access/user@example.com")

        assert resp.status_code == 204

    def test_returns_404_for_nonexistent(self, make_user):
        app = _create_app()
        admin = make_user(email="admin@example.com", roles=["Admin"])
        _override_auth(app, admin)

        mock_repo = MagicMock()
        mock_repo.revoke_access.return_value = False
        _override_repo(app, mock_repo)

        client = TestClient(app)
        resp = client.delete("/admin/fine-tuning/access/nobody@example.com")

        assert resp.status_code == 404


SAMPLE_JOB = {
    "job_id": "abc123def456",
    "user_id": "user-001",
    "email": "user@example.com",
    "model_id": "meta-llama-3-8b",
    "model_name": "Meta Llama 3 8B",
    "status": "TRAINING",
    "dataset_s3_key": "datasets/user-001/abc/train.jsonl",
    "output_s3_prefix": "output/user-001/abc123def456",
    "instance_type": "ml.g5.2xlarge",
    "instance_count": 1,
    "hyperparameters": {"epochs": "3"},
    "sagemaker_job_name": "ft-abc12345-20260313",
    "training_start_time": None,
    "training_end_time": None,
    "billable_seconds": None,
    "estimated_cost_usd": None,
    "created_at": "2026-03-13T10:00:00+00:00",
    "updated_at": "2026-03-13T10:00:00+00:00",
    "error_message": None,
    "max_runtime_seconds": 86400,
}


class TestListAllJobs:

    def test_returns_200_with_all_jobs(self, make_user):
        app = _create_app()
        admin = make_user(email="admin@example.com", roles=["Admin"])
        _override_auth(app, admin)

        mock_jobs_repo = MagicMock()
        mock_jobs_repo.list_all_jobs.return_value = [SAMPLE_JOB]
        _override_jobs_repo(app, mock_jobs_repo)

        client = TestClient(app)
        resp = client.get("/admin/fine-tuning/jobs")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 1
        assert body["jobs"][0]["job_id"] == "abc123def456"

    def test_filters_by_status(self, make_user):
        app = _create_app()
        admin = make_user(email="admin@example.com", roles=["Admin"])
        _override_auth(app, admin)

        mock_jobs_repo = MagicMock()
        mock_jobs_repo.list_all_jobs.return_value = [SAMPLE_JOB]
        _override_jobs_repo(app, mock_jobs_repo)

        client = TestClient(app)
        resp = client.get("/admin/fine-tuning/jobs?status=TRAINING")

        assert resp.status_code == 200
        mock_jobs_repo.list_all_jobs.assert_called_once_with(status_filter="TRAINING")

    def test_requires_admin_role(self):
        app = _create_app()

        def _raise_403():
            raise HTTPException(status_code=403, detail="Forbidden")
        app.dependency_overrides[require_admin] = _raise_403

        client = TestClient(app)
        resp = client.get("/admin/fine-tuning/jobs")
        assert resp.status_code == 403


SAMPLE_INFERENCE_JOB = {
    "job_id": "inf-xyz789",
    "user_id": "user-001",
    "email": "user@example.com",
    "job_type": "inference",
    "training_job_id": "train-abc123",
    "model_name": "Meta Llama 3 8B",
    "model_s3_path": "s3://bucket/output/user-001/train-abc123/ft-trainabc/output/model.tar.gz",
    "status": "TRANSFORMING",
    "input_s3_key": "inference-input/user-001/xyz/input.txt",
    "output_s3_prefix": "inference-output/user-001/inf-xyz789",
    "result_s3_key": None,
    "instance_type": "ml.g5.2xlarge",
    "transform_job_name": "inf-xyz78900-20260313",
    "transform_start_time": None,
    "transform_end_time": None,
    "billable_seconds": None,
    "estimated_cost_usd": None,
    "created_at": "2026-03-13T14:00:00+00:00",
    "updated_at": "2026-03-13T14:00:00+00:00",
    "error_message": None,
    "max_runtime_seconds": 3600,
}


class TestListAllInferenceJobs:

    def test_returns_200_with_all_inference_jobs(self, make_user):
        app = _create_app()
        admin = make_user(email="admin@example.com", roles=["Admin"])
        _override_auth(app, admin)

        mock_inf_repo = MagicMock()
        mock_inf_repo.list_all_inference_jobs.return_value = [SAMPLE_INFERENCE_JOB]
        _override_inf_repo(app, mock_inf_repo)

        client = TestClient(app)
        resp = client.get("/admin/fine-tuning/inference-jobs")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 1
        assert body["jobs"][0]["job_id"] == "inf-xyz789"
        assert body["jobs"][0]["job_type"] == "inference"

    def test_filters_by_status(self, make_user):
        app = _create_app()
        admin = make_user(email="admin@example.com", roles=["Admin"])
        _override_auth(app, admin)

        mock_inf_repo = MagicMock()
        mock_inf_repo.list_all_inference_jobs.return_value = [SAMPLE_INFERENCE_JOB]
        _override_inf_repo(app, mock_inf_repo)

        client = TestClient(app)
        resp = client.get("/admin/fine-tuning/inference-jobs?status=TRANSFORMING")

        assert resp.status_code == 200
        mock_inf_repo.list_all_inference_jobs.assert_called_once_with(status_filter="TRANSFORMING")

    def test_requires_admin_role(self):
        app = _create_app()

        def _raise_403():
            raise HTTPException(status_code=403, detail="Forbidden")
        app.dependency_overrides[require_admin] = _raise_403

        client = TestClient(app)
        resp = client.get("/admin/fine-tuning/inference-jobs")
        assert resp.status_code == 403
