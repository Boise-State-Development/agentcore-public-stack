"""Route tests for admin fine-tuning access management endpoints."""

import pytest
import os
from unittest.mock import MagicMock, patch
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient

from apis.shared.auth.models import User
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.rbac import require_admin
from tests.conftest import override_admin_auth
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
    app.dependency_overrides[get_current_user_from_session] = lambda: user
    override_admin_auth(app, lambda: user)


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
        override_admin_auth(app, _raise_403)

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
        override_admin_auth(app, _raise_403)

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
        override_admin_auth(app, _raise_403)

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
        override_admin_auth(app, _raise_403)

        client = TestClient(app)
        resp = client.get("/admin/fine-tuning/inference-jobs")
        assert resp.status_code == 403


class TestCostDashboard:
    """The dashboard reported $0.00 while real jobs were being billed.

    Two causes, both regression-guarded here: the StatusIndex GSI partition key
    is compared case-sensitively and was queried with SageMaker's "Completed"
    spelling instead of the stored "COMPLETED"; and FAILED jobs were excluded
    even though AWS bills a job that dies partway through.
    """

    @staticmethod
    def _job(email, status_value, billable, cost):
        return {
            "email": email,
            "status": status_value,
            "billable_seconds": billable,
            "estimated_cost_usd": cost,
        }

    def _client(self, make_user, training_by_status, inference_by_status=None):
        app = _create_app()
        _override_auth(app, make_user(email="admin@example.com", roles=["Admin"]))

        jobs_repo = MagicMock()
        jobs_repo.query_jobs_by_status_and_date.side_effect = (
            lambda status_value, *_: list(training_by_status.get(status_value, []))
        )
        _override_jobs_repo(app, jobs_repo)

        inf_repo = MagicMock()
        inf_repo.query_jobs_by_status_and_date.side_effect = (
            lambda status_value, *_: list((inference_by_status or {}).get(status_value, []))
        )
        _override_inf_repo(app, inf_repo)

        return TestClient(app), jobs_repo, inf_repo

    def test_queries_stored_uppercase_statuses(self, make_user):
        """SageMaker's "Completed" casing matches nothing on the GSI."""
        client, jobs_repo, inf_repo = self._client(make_user, {})

        resp = client.get("/admin/fine-tuning/costs?month=2026-08")

        assert resp.status_code == 200
        for repo in (jobs_repo, inf_repo):
            queried = {
                call.args[0] for call in repo.query_jobs_by_status_and_date.call_args_list
            }
            assert queried == {"COMPLETED", "FAILED", "STOPPED"}

    def test_aggregates_completed_jobs(self, make_user):
        client, _, _ = self._client(
            make_user,
            {"COMPLETED": [self._job("user@example.com", "COMPLETED", 300, 0.1175)]},
        )

        body = client.get("/admin/fine-tuning/costs?month=2026-08").json()

        assert body["total_cost_usd"] == pytest.approx(0.1175)
        # Rounded to 2dp by the route: 300s = 0.0833h -> 0.08
        assert body["total_gpu_hours"] == pytest.approx(0.08)
        assert body["training_job_count"] == 1
        assert body["active_user_count"] == 1
        assert body["users"][0]["email"] == "user@example.com"

    def test_counts_failed_jobs_because_aws_bills_them(self, make_user):
        client, _, _ = self._client(
            make_user,
            {"FAILED": [self._job("user@example.com", "FAILED", 296, 0.1159)]},
        )

        body = client.get("/admin/fine-tuning/costs?month=2026-08").json()

        assert body["total_cost_usd"] == pytest.approx(0.1159)
        assert body["training_job_count"] == 1

    def test_sums_training_and_inference_per_user(self, make_user):
        client, _, _ = self._client(
            make_user,
            {
                "COMPLETED": [self._job("user@example.com", "COMPLETED", 300, 0.1175)],
                "FAILED": [self._job("user@example.com", "FAILED", 296, 0.1159)],
            },
            {"COMPLETED": [self._job("user@example.com", "COMPLETED", 230, 0.09)]},
        )

        body = client.get("/admin/fine-tuning/costs?month=2026-08").json()

        assert body["total_cost_usd"] == pytest.approx(0.1175 + 0.1159 + 0.09)
        assert body["training_job_count"] == 2
        assert body["inference_job_count"] == 1
        assert body["active_user_count"] == 1

    def test_requires_admin_role(self):
        app = _create_app()

        def _raise_403():
            raise HTTPException(status_code=403, detail="Forbidden")
        override_admin_auth(app, _raise_403)

        resp = TestClient(app).get("/admin/fine-tuning/costs")
        assert resp.status_code == 403
