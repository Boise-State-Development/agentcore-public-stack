"""Route tests for fine-tuning job endpoints."""

import math
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apis.shared.auth.models import User
from apis.shared.auth.dependencies import get_current_user
from apis.app_api.fine_tuning.routes import router, _estimate_training_progress
from apis.app_api.fine_tuning.dependencies import require_fine_tuning_access
from apis.app_api.fine_tuning.job_repository import get_fine_tuning_jobs_repository
from apis.app_api.fine_tuning.s3_service import get_fine_tuning_s3_service
from apis.app_api.fine_tuning.sagemaker_service import get_sagemaker_service
from apis.app_api.fine_tuning.repository import get_fine_tuning_access_repository
from apis.app_api.fine_tuning.script_packaging_service import get_script_packaging_service


def _create_app():
    app = FastAPI()
    app.include_router(router)
    return app


def _setup_deps(app, user, grant, jobs_repo=None, s3_service=None, sagemaker=None, access_repo=None, script_service=None):
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_fine_tuning_access] = lambda: grant
    if jobs_repo:
        app.dependency_overrides[get_fine_tuning_jobs_repository] = lambda: jobs_repo
    if s3_service:
        app.dependency_overrides[get_fine_tuning_s3_service] = lambda: s3_service
    if sagemaker:
        app.dependency_overrides[get_sagemaker_service] = lambda: sagemaker
    if access_repo:
        app.dependency_overrides[get_fine_tuning_access_repository] = lambda: access_repo
    if script_service:
        app.dependency_overrides[get_script_packaging_service] = lambda: script_service


SAMPLE_GRANT = {
    "email": "user@example.com",
    "granted_by": "admin@example.com",
    "granted_at": "2026-01-01T00:00:00Z",
    "monthly_quota_hours": 10.0,
    "current_month_usage_hours": 2.0,
    "quota_period": "2026-03",
}

SAMPLE_JOB = {
    "job_id": "abc123def456",
    "user_id": "user-001",
    "email": "user@example.com",
    "model_id": "distilgpt2",
    "model_name": "DistilGPT-2",
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
    "training_progress": None,
}


class TestListModels:

    def test_returns_200_with_model_catalog(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")
        _setup_deps(app, user, SAMPLE_GRANT)

        client = TestClient(app)
        resp = client.get("/fine-tuning/models")

        assert resp.status_code == 200
        models = resp.json()
        assert len(models) >= 3
        model_ids = {m["model_id"] for m in models}
        assert "distilgpt2" in model_ids
        assert "bert-base-uncased" in model_ids
        assert "smollm2-135m-instruct" in model_ids


class TestPresign:

    def test_returns_200_with_presigned_url(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_s3 = MagicMock()
        mock_s3.generate_upload_url.return_value = (
            "https://s3.us-west-2.amazonaws.com/bucket/key?X-Amz-Signature=...",
            "datasets/user-001/abc/train.jsonl",
        )
        mock_s3.presign_expiration = 900

        _setup_deps(app, user, SAMPLE_GRANT, s3_service=mock_s3)

        client = TestClient(app)
        resp = client.post(
            "/fine-tuning/presign",
            json={"filename": "train.jsonl", "content_type": "application/jsonl"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert "presigned_url" in body
        assert "s3_key" in body
        assert "expires_at" in body


class TestCreateJob:

    def test_returns_201_on_success(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_s3 = MagicMock()
        mock_s3.check_object_exists.return_value = True
        mock_s3.get_output_s3_prefix.return_value = "output/user-001/job-abc"
        mock_s3.get_output_s3_uri.return_value = "s3://bucket/output/user-001/job-abc"
        mock_s3.bucket_name = "test-bucket"

        mock_jobs = MagicMock()
        mock_jobs.create_job.return_value = SAMPLE_JOB
        mock_jobs.update_job_status.return_value = {**SAMPLE_JOB, "status": "TRAINING"}

        mock_sm = MagicMock()
        mock_sm.create_training_job.return_value = {}

        mock_access = MagicMock()

        mock_script = MagicMock()
        mock_script.ensure_scripts_uploaded.return_value = "s3://test-bucket/scripts/sourcedir.tar.gz"

        _setup_deps(app, user, SAMPLE_GRANT, mock_jobs, mock_s3, mock_sm, mock_access, mock_script)

        client = TestClient(app)
        resp = client.post(
            "/fine-tuning/jobs",
            json={
                "model_id": "distilgpt2",
                "dataset_s3_key": "datasets/user-001/abc/train.jsonl",
            },
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["model_id"] == "distilgpt2"

    @patch.dict("os.environ", {"PROJECT_PREFIX": "test-prefix"})
    def test_sagemaker_job_name_includes_project_prefix(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_s3 = MagicMock()
        mock_s3.check_object_exists.return_value = True
        mock_s3.get_output_s3_prefix.return_value = "output/user-001/job-abc"
        mock_s3.get_output_s3_uri.return_value = "s3://bucket/output/user-001/job-abc"
        mock_s3.bucket_name = "test-bucket"

        mock_jobs = MagicMock()
        mock_jobs.create_job.return_value = SAMPLE_JOB
        mock_jobs.update_job_status.return_value = {**SAMPLE_JOB, "status": "TRAINING"}

        mock_sm = MagicMock()
        mock_sm.create_training_job.return_value = {}

        mock_access = MagicMock()

        mock_script = MagicMock()
        mock_script.ensure_scripts_uploaded.return_value = "s3://test-bucket/scripts/sourcedir.tar.gz"

        _setup_deps(app, user, SAMPLE_GRANT, mock_jobs, mock_s3, mock_sm, mock_access, mock_script)

        client = TestClient(app)
        resp = client.post(
            "/fine-tuning/jobs",
            json={
                "model_id": "distilgpt2",
                "dataset_s3_key": "datasets/user-001/abc/train.jsonl",
            },
        )

        assert resp.status_code == 201
        job_name = mock_sm.create_training_job.call_args[1]["job_name"]
        assert job_name.startswith("test-prefix-ft-")

    def test_returns_400_for_unknown_model(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")
        _setup_deps(app, user, SAMPLE_GRANT, MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())

        client = TestClient(app)
        resp = client.post(
            "/fine-tuning/jobs",
            json={
                "model_id": "nonexistent-model",
                "dataset_s3_key": "datasets/user-001/abc/train.jsonl",
            },
        )

        assert resp.status_code == 400
        assert "Unknown model_id" in resp.json()["detail"]

    def test_returns_400_when_dataset_not_found(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_s3 = MagicMock()
        mock_s3.check_object_exists.return_value = False

        _setup_deps(app, user, SAMPLE_GRANT, MagicMock(), mock_s3, MagicMock(), MagicMock(), MagicMock())

        client = TestClient(app)
        resp = client.post(
            "/fine-tuning/jobs",
            json={
                "model_id": "distilgpt2",
                "dataset_s3_key": "datasets/user-001/abc/train.jsonl",
            },
        )

        assert resp.status_code == 400
        assert "Dataset not found" in resp.json()["detail"]

    def test_returns_400_when_quota_insufficient(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        low_quota_grant = {**SAMPLE_GRANT, "monthly_quota_hours": 10.0, "current_month_usage_hours": 9.5}

        mock_s3 = MagicMock()
        mock_s3.check_object_exists.return_value = True

        _setup_deps(app, user, low_quota_grant, MagicMock(), mock_s3, MagicMock(), MagicMock(), MagicMock())

        client = TestClient(app)
        resp = client.post(
            "/fine-tuning/jobs",
            json={
                "model_id": "distilgpt2",
                "dataset_s3_key": "datasets/user-001/abc/train.jsonl",
            },
        )

        assert resp.status_code == 400
        assert "Insufficient quota" in resp.json()["detail"]


class TestListJobs:

    def test_returns_200_with_jobs(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_jobs = MagicMock()
        mock_jobs.list_user_jobs.return_value = [SAMPLE_JOB]
        _setup_deps(app, user, SAMPLE_GRANT, mock_jobs)

        client = TestClient(app)
        resp = client.get("/fine-tuning/jobs")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 1
        assert body["jobs"][0]["job_id"] == SAMPLE_JOB["job_id"]


class TestGetJob:

    def test_returns_200_for_existing_job(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        completed_job = {**SAMPLE_JOB, "status": "COMPLETED"}
        mock_jobs = MagicMock()
        mock_jobs.get_job.return_value = completed_job

        mock_sm = MagicMock()
        mock_access = MagicMock()

        _setup_deps(app, user, SAMPLE_GRANT, mock_jobs, sagemaker=mock_sm, access_repo=mock_access)

        client = TestClient(app)
        resp = client.get(f"/fine-tuning/jobs/{SAMPLE_JOB['job_id']}")

        assert resp.status_code == 200
        assert resp.json()["job_id"] == SAMPLE_JOB["job_id"]

    def test_returns_404_for_nonexistent(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_jobs = MagicMock()
        mock_jobs.get_job.return_value = None

        mock_sm = MagicMock()
        mock_access = MagicMock()

        _setup_deps(app, user, SAMPLE_GRANT, mock_jobs, sagemaker=mock_sm, access_repo=mock_access)

        client = TestClient(app)
        resp = client.get("/fine-tuning/jobs/nonexistent")

        assert resp.status_code == 404

    def test_syncs_status_for_training_job(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_jobs = MagicMock()
        mock_jobs.get_job.return_value = SAMPLE_JOB
        mock_jobs.update_job_status.return_value = {
            **SAMPLE_JOB,
            "status": "COMPLETED",
            "billable_seconds": 7200,
            "estimated_cost_usd": 3.03,
        }

        mock_sm = MagicMock()
        mock_sm.describe_training_job.return_value = {
            "status": "Completed",
            "training_start_time": "2026-03-13T10:00:00+00:00",
            "training_end_time": "2026-03-13T12:00:00+00:00",
            "billable_seconds": 7200,
        }
        mock_sm.calculate_cost.return_value = 3.03

        mock_access = MagicMock()
        mock_access.increment_usage.return_value = {}

        _setup_deps(app, user, SAMPLE_GRANT, mock_jobs, sagemaker=mock_sm, access_repo=mock_access)

        client = TestClient(app)
        resp = client.get(f"/fine-tuning/jobs/{SAMPLE_JOB['job_id']}")

        assert resp.status_code == 200
        mock_sm.describe_training_job.assert_called_once()
        mock_jobs.update_job_status.assert_called_once()


class TestGetJobLogs:

    def test_returns_200_with_logs(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_jobs = MagicMock()
        mock_jobs.get_job.return_value = SAMPLE_JOB

        mock_sm = MagicMock()
        mock_sm.get_training_logs.return_value = ["Starting training...", "Epoch 1/3"]

        _setup_deps(app, user, SAMPLE_GRANT, mock_jobs, sagemaker=mock_sm)

        client = TestClient(app)
        resp = client.get(f"/fine-tuning/jobs/{SAMPLE_JOB['job_id']}/logs")

        assert resp.status_code == 200
        assert resp.json()["logs"] == ["Starting training...", "Epoch 1/3"]

    def test_returns_404_for_nonexistent_job(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_jobs = MagicMock()
        mock_jobs.get_job.return_value = None

        mock_sm = MagicMock()
        _setup_deps(app, user, SAMPLE_GRANT, mock_jobs, sagemaker=mock_sm)

        client = TestClient(app)
        resp = client.get("/fine-tuning/jobs/nonexistent/logs")

        assert resp.status_code == 404


class TestDownloadArtifact:

    def test_returns_200_with_download_url(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        completed_job = {**SAMPLE_JOB, "status": "COMPLETED"}
        mock_jobs = MagicMock()
        mock_jobs.get_job.return_value = completed_job

        mock_s3 = MagicMock()
        mock_s3.check_object_exists.return_value = True
        mock_s3.generate_download_url.return_value = "https://s3.amazonaws.com/bucket/model.tar.gz?sig=..."
        mock_s3.presign_expiration = 900

        _setup_deps(app, user, SAMPLE_GRANT, mock_jobs, mock_s3)

        client = TestClient(app)
        resp = client.get(f"/fine-tuning/jobs/{SAMPLE_JOB['job_id']}/download")

        assert resp.status_code == 200
        assert "download_url" in resp.json()

    def test_returns_400_for_non_completed_job(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_jobs = MagicMock()
        mock_jobs.get_job.return_value = SAMPLE_JOB  # status=TRAINING

        _setup_deps(app, user, SAMPLE_GRANT, mock_jobs, MagicMock())

        client = TestClient(app)
        resp = client.get(f"/fine-tuning/jobs/{SAMPLE_JOB['job_id']}/download")

        assert resp.status_code == 400


class TestStopJob:

    def test_returns_200_on_success(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_jobs = MagicMock()
        mock_jobs.get_job.return_value = SAMPLE_JOB
        mock_jobs.update_job_status.return_value = {**SAMPLE_JOB, "status": "STOPPED"}

        mock_sm = MagicMock()

        _setup_deps(app, user, SAMPLE_GRANT, mock_jobs, sagemaker=mock_sm)

        client = TestClient(app)
        resp = client.delete(f"/fine-tuning/jobs/{SAMPLE_JOB['job_id']}")

        assert resp.status_code == 200
        mock_sm.stop_training_job.assert_called_once()

    def test_returns_400_for_completed_job(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        completed_job = {**SAMPLE_JOB, "status": "COMPLETED"}
        mock_jobs = MagicMock()
        mock_jobs.get_job.return_value = completed_job

        _setup_deps(app, user, SAMPLE_GRANT, mock_jobs, sagemaker=MagicMock())

        client = TestClient(app)
        resp = client.delete(f"/fine-tuning/jobs/{SAMPLE_JOB['job_id']}")

        assert resp.status_code == 400


class TestRequiresAccess:

    def test_returns_403_without_access(self, make_user):
        app = _create_app()

        def _raise_403():
            raise HTTPException(status_code=403, detail="Forbidden")
        app.dependency_overrides[require_fine_tuning_access] = _raise_403

        user = make_user(email="denied@example.com")
        app.dependency_overrides[get_current_user] = lambda: user

        client = TestClient(app)

        assert client.get("/fine-tuning/models").status_code == 403
        assert client.post("/fine-tuning/presign", json={"filename": "a", "content_type": "b"}).status_code == 403
        assert client.post("/fine-tuning/jobs", json={"model_id": "x", "dataset_s3_key": "y"}).status_code == 403
        assert client.get("/fine-tuning/jobs").status_code == 403
        assert client.get("/fine-tuning/jobs/abc").status_code == 403
        assert client.get("/fine-tuning/jobs/abc/logs").status_code == 403
        assert client.get("/fine-tuning/jobs/abc/download").status_code == 403
        assert client.delete("/fine-tuning/jobs/abc").status_code == 403


# =========================================================================
# _estimate_training_progress unit tests
# =========================================================================


class TestEstimateTrainingProgress:

    def test_starting_phase_returns_2(self):
        result = _estimate_training_progress({"secondary_status": "Starting"}, {})
        assert result == 2.0

    def test_launching_instances_returns_3(self):
        result = _estimate_training_progress({"secondary_status": "LaunchingMLInstances"}, {})
        assert result == 3.0

    def test_preparing_stack_returns_5(self):
        result = _estimate_training_progress({"secondary_status": "PreparingTrainingStack"}, {})
        assert result == 5.0

    def test_downloading_image_returns_6(self):
        result = _estimate_training_progress({"secondary_status": "DownloadingTrainingImage"}, {})
        assert result == 6.0

    def test_downloading_data_returns_8(self):
        result = _estimate_training_progress({"secondary_status": "Downloading"}, {})
        assert result == 8.0

    def test_uploading_returns_92(self):
        result = _estimate_training_progress({"secondary_status": "Uploading"}, {})
        assert result == 92.0

    def test_training_phase_with_elapsed_time(self):
        """Training phase uses a logarithmic curve based on elapsed time."""
        start = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        sm_status = {
            "secondary_status": "Training",
            "training_start_time": start,
        }
        result = _estimate_training_progress(sm_status, {})
        # After 30 min: ~57%  (10 + 75 * (1 - e^(-1)))
        expected = round(10.0 + 75.0 * (1.0 - math.exp(-1.0)), 1)
        assert abs(result - expected) < 2.0  # Allow small timing drift

    def test_training_phase_short_elapsed(self):
        """Short elapsed time yields low progress."""
        start = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        sm_status = {
            "secondary_status": "Training",
            "training_start_time": start,
        }
        result = _estimate_training_progress(sm_status, {})
        assert result >= 10.0
        assert result < 20.0

    def test_training_phase_long_elapsed(self):
        """Long elapsed time caps at 85%."""
        start = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        sm_status = {
            "secondary_status": "Training",
            "training_start_time": start,
        }
        result = _estimate_training_progress(sm_status, {})
        assert result == 85.0

    def test_training_phase_uses_job_start_time_fallback(self):
        """Falls back to job's training_start_time when SageMaker status lacks it."""
        start = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        sm_status = {"secondary_status": "Training"}
        job = {"training_start_time": start}
        result = _estimate_training_progress(sm_status, job)
        # After 15 min: ~40%
        assert result > 30.0
        assert result < 50.0

    def test_training_phase_default_without_timing(self):
        """Returns 15% for Training phase without any timing info."""
        sm_status = {"secondary_status": "Training"}
        result = _estimate_training_progress(sm_status, {})
        assert result == 15.0

    def test_unknown_secondary_status_returns_5(self):
        """Returns 5% for unknown secondary status without timing info."""
        sm_status = {"secondary_status": "SomeUnknownPhase"}
        result = _estimate_training_progress(sm_status, {})
        assert result == 5.0

    def test_no_secondary_status_with_timing(self):
        """Uses elapsed time curve when secondary_status is missing but timing exists."""
        start = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        sm_status = {"training_start_time": start}
        result = _estimate_training_progress(sm_status, {})
        assert result > 10.0
        assert result < 40.0

    def test_no_secondary_status_no_timing(self):
        """Returns 5% as absolute fallback."""
        result = _estimate_training_progress({}, {})
        assert result == 5.0

    def test_progress_never_exceeds_92(self):
        """No phase estimate should exceed the Uploading phase (92%)."""
        start = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        sm_status = {
            "secondary_status": "Training",
            "training_start_time": start,
        }
        result = _estimate_training_progress(sm_status, {})
        assert result <= 92.0


# =========================================================================
# _sync_job_status behavior tests (via route integration)
# =========================================================================


class TestSyncJobStatusFallbackProgress:

    def test_returns_fallback_progress_when_status_unchanged(self, make_user):
        """When status is still TRAINING, response includes estimated progress."""
        app = _create_app()
        user = make_user(email="user@example.com")

        training_job = {
            **SAMPLE_JOB,
            "status": "TRAINING",
            "training_progress": None,
            "training_start_time": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
        }
        mock_jobs = MagicMock()
        mock_jobs.get_job.return_value = training_job

        mock_sm = MagicMock()
        mock_sm.describe_training_job.return_value = {
            "status": "InProgress",
            "secondary_status": "Training",
            "training_start_time": training_job["training_start_time"],
        }

        mock_access = MagicMock()
        _setup_deps(app, user, SAMPLE_GRANT, mock_jobs, sagemaker=mock_sm, access_repo=mock_access)

        client = TestClient(app)
        resp = client.get(f"/fine-tuning/jobs/{SAMPLE_JOB['job_id']}")

        assert resp.status_code == 200
        body = resp.json()
        # Should have a non-null estimated progress
        assert body["training_progress"] is not None
        assert body["training_progress"] > 0.0
        # update_job_status should NOT be called (fallback is in-memory only)
        mock_jobs.update_job_status.assert_not_called()

    def test_preserves_real_progress_from_callback(self, make_user):
        """When training_progress already exists in DB, don't overwrite with estimate."""
        app = _create_app()
        user = make_user(email="user@example.com")

        training_job = {
            **SAMPLE_JOB,
            "status": "TRAINING",
            "training_progress": 45.0,  # Real progress from callback
            "training_start_time": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
        }
        mock_jobs = MagicMock()
        mock_jobs.get_job.return_value = training_job

        mock_sm = MagicMock()
        mock_sm.describe_training_job.return_value = {
            "status": "InProgress",
            "secondary_status": "Training",
        }

        mock_access = MagicMock()
        _setup_deps(app, user, SAMPLE_GRANT, mock_jobs, sagemaker=mock_sm, access_repo=mock_access)

        client = TestClient(app)
        resp = client.get(f"/fine-tuning/jobs/{SAMPLE_JOB['job_id']}")

        assert resp.status_code == 200
        # Real progress should be preserved unchanged
        assert resp.json()["training_progress"] == 45.0

    def test_persists_training_start_time_on_unchanged_status(self, make_user):
        """When status unchanged but SageMaker has start time we lack, persist it."""
        app = _create_app()
        user = make_user(email="user@example.com")

        training_job = {
            **SAMPLE_JOB,
            "status": "TRAINING",
            "training_progress": None,
            "training_start_time": None,  # Not yet set
        }
        mock_jobs = MagicMock()
        mock_jobs.get_job.return_value = training_job
        mock_jobs.update_job_status.return_value = {
            **training_job,
            "training_start_time": "2026-03-13T10:05:00+00:00",
        }

        mock_sm = MagicMock()
        mock_sm.describe_training_job.return_value = {
            "status": "InProgress",
            "secondary_status": "Training",
            "training_start_time": "2026-03-13T10:05:00+00:00",
        }

        mock_access = MagicMock()
        _setup_deps(app, user, SAMPLE_GRANT, mock_jobs, sagemaker=mock_sm, access_repo=mock_access)

        client = TestClient(app)
        resp = client.get(f"/fine-tuning/jobs/{SAMPLE_JOB['job_id']}")

        assert resp.status_code == 200
        # Should have persisted training_start_time
        mock_jobs.update_job_status.assert_called_once_with(
            "user-001", SAMPLE_JOB["job_id"], "TRAINING",
            training_start_time="2026-03-13T10:05:00+00:00",
        )

    def test_does_not_persist_start_time_when_already_set(self, make_user):
        """When training_start_time already set, don't call update."""
        app = _create_app()
        user = make_user(email="user@example.com")

        training_job = {
            **SAMPLE_JOB,
            "status": "TRAINING",
            "training_progress": None,
            "training_start_time": "2026-03-13T10:05:00+00:00",
        }
        mock_jobs = MagicMock()
        mock_jobs.get_job.return_value = training_job

        mock_sm = MagicMock()
        mock_sm.describe_training_job.return_value = {
            "status": "InProgress",
            "secondary_status": "Training",
            "training_start_time": "2026-03-13T10:05:00+00:00",
        }

        mock_access = MagicMock()
        _setup_deps(app, user, SAMPLE_GRANT, mock_jobs, sagemaker=mock_sm, access_repo=mock_access)

        client = TestClient(app)
        resp = client.get(f"/fine-tuning/jobs/{SAMPLE_JOB['job_id']}")

        assert resp.status_code == 200
        mock_jobs.update_job_status.assert_not_called()

    def test_syncs_pending_job(self, make_user):
        """PENDING jobs should also trigger SageMaker status sync."""
        app = _create_app()
        user = make_user(email="user@example.com")

        pending_job = {**SAMPLE_JOB, "status": "PENDING", "training_progress": None}
        mock_jobs = MagicMock()
        mock_jobs.get_job.return_value = pending_job
        mock_jobs.update_job_status.return_value = {
            **pending_job,
            "status": "TRAINING",
            "training_start_time": "2026-03-13T10:05:00+00:00",
        }

        mock_sm = MagicMock()
        mock_sm.describe_training_job.return_value = {
            "status": "InProgress",
            "secondary_status": "Training",
            "training_start_time": "2026-03-13T10:05:00+00:00",
        }
        mock_sm.calculate_cost.return_value = 0.0

        mock_access = MagicMock()
        _setup_deps(app, user, SAMPLE_GRANT, mock_jobs, sagemaker=mock_sm, access_repo=mock_access)

        client = TestClient(app)
        resp = client.get(f"/fine-tuning/jobs/{pending_job['job_id']}")

        assert resp.status_code == 200
        # Should have called describe_training_job for PENDING status
        mock_sm.describe_training_job.assert_called_once()
        # Status changed PENDING → TRAINING, so update_job_status should be called
        mock_jobs.update_job_status.assert_called_once()

    def test_no_sync_for_completed_job(self, make_user):
        """COMPLETED jobs should NOT trigger SageMaker sync."""
        app = _create_app()
        user = make_user(email="user@example.com")

        completed_job = {**SAMPLE_JOB, "status": "COMPLETED"}
        mock_jobs = MagicMock()
        mock_jobs.get_job.return_value = completed_job

        mock_sm = MagicMock()
        mock_access = MagicMock()
        _setup_deps(app, user, SAMPLE_GRANT, mock_jobs, sagemaker=mock_sm, access_repo=mock_access)

        client = TestClient(app)
        resp = client.get(f"/fine-tuning/jobs/{completed_job['job_id']}")

        assert resp.status_code == 200
        mock_sm.describe_training_job.assert_not_called()
