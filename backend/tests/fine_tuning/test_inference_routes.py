"""Route tests for inference (Batch Transform) endpoints."""

import pytest
from unittest.mock import MagicMock
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apis.shared.auth.models import User
from apis.shared.auth.dependencies import get_current_user
from apis.app_api.fine_tuning.routes import router
from apis.app_api.fine_tuning.dependencies import require_fine_tuning_access
from apis.app_api.fine_tuning.job_repository import get_fine_tuning_jobs_repository
from apis.app_api.fine_tuning.inference_repository import get_inference_repository
from apis.app_api.fine_tuning.s3_service import get_fine_tuning_s3_service
from apis.app_api.fine_tuning.sagemaker_service import get_sagemaker_service
from apis.app_api.fine_tuning.repository import get_fine_tuning_access_repository


def _create_app():
    app = FastAPI()
    app.include_router(router)
    return app


def _setup_deps(
    app, user, grant,
    jobs_repo=None, inf_repo=None, s3_service=None,
    sagemaker=None, access_repo=None,
):
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_fine_tuning_access] = lambda: grant
    if jobs_repo:
        app.dependency_overrides[get_fine_tuning_jobs_repository] = lambda: jobs_repo
    if inf_repo:
        app.dependency_overrides[get_inference_repository] = lambda: inf_repo
    if s3_service:
        app.dependency_overrides[get_fine_tuning_s3_service] = lambda: s3_service
    if sagemaker:
        app.dependency_overrides[get_sagemaker_service] = lambda: sagemaker
    if access_repo:
        app.dependency_overrides[get_fine_tuning_access_repository] = lambda: access_repo


SAMPLE_GRANT = {
    "email": "user@example.com",
    "granted_by": "admin@example.com",
    "granted_at": "2026-01-01T00:00:00Z",
    "monthly_quota_hours": 10.0,
    "current_month_usage_hours": 2.0,
    "quota_period": "2026-03",
}

SAMPLE_COMPLETED_TRAINING_JOB = {
    "job_id": "train-abc123",
    "user_id": "user-001",
    "email": "user@example.com",
    "model_id": "meta-llama-3-8b",
    "model_name": "Meta Llama 3 8B",
    "status": "COMPLETED",
    "dataset_s3_key": "datasets/user-001/abc/train.jsonl",
    "output_s3_prefix": "output/user-001/train-abc123",
    "instance_type": "ml.g5.2xlarge",
    "instance_count": 1,
    "hyperparameters": {"epochs": "3"},
    "sagemaker_job_name": "ft-trainabc-20260313",
    "training_start_time": "2026-03-13T10:00:00+00:00",
    "training_end_time": "2026-03-13T12:00:00+00:00",
    "billable_seconds": 7200,
    "estimated_cost_usd": 3.03,
    "created_at": "2026-03-13T09:00:00+00:00",
    "updated_at": "2026-03-13T12:00:00+00:00",
    "error_message": None,
    "max_runtime_seconds": 86400,
}

SAMPLE_INFERENCE_JOB = {
    "job_id": "inf-xyz789",
    "user_id": "user-001",
    "email": "user@example.com",
    "job_type": "inference",
    "training_job_id": "train-abc123",
    "model_name": "Meta Llama 3 8B",
    "model_s3_path": "s3://bucket/output/user-001/train-abc123/ft-trainabc-20260313/output/model.tar.gz",
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


class TestListTrainedModels:

    def test_returns_only_completed_training_jobs(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_jobs = MagicMock()
        mock_jobs.list_user_jobs.return_value = [
            SAMPLE_COMPLETED_TRAINING_JOB,
            {**SAMPLE_COMPLETED_TRAINING_JOB, "job_id": "train-2", "status": "TRAINING"},
            {**SAMPLE_COMPLETED_TRAINING_JOB, "job_id": "train-3", "status": "FAILED"},
        ]

        mock_s3 = MagicMock()
        mock_s3.bucket_name = "test-bucket"

        _setup_deps(app, user, SAMPLE_GRANT, jobs_repo=mock_jobs, s3_service=mock_s3)

        client = TestClient(app)
        resp = client.get("/fine-tuning/trained-models")

        assert resp.status_code == 200
        models = resp.json()
        assert len(models) == 1
        assert models[0]["training_job_id"] == "train-abc123"
        assert "model_s3_path" in models[0]

    def test_returns_empty_when_no_completed_jobs(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_jobs = MagicMock()
        mock_jobs.list_user_jobs.return_value = []

        mock_s3 = MagicMock()
        mock_s3.bucket_name = "test-bucket"

        _setup_deps(app, user, SAMPLE_GRANT, jobs_repo=mock_jobs, s3_service=mock_s3)

        client = TestClient(app)
        resp = client.get("/fine-tuning/trained-models")

        assert resp.status_code == 200
        assert resp.json() == []


class TestInferencePresign:

    def test_returns_200_with_presigned_url(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_s3 = MagicMock()
        mock_s3.generate_inference_upload_url.return_value = (
            "https://s3.us-west-2.amazonaws.com/bucket/key?X-Amz-Signature=...",
            "inference-input/user-001/abc/input.txt",
        )
        mock_s3.presign_expiration = 900

        _setup_deps(app, user, SAMPLE_GRANT, s3_service=mock_s3)

        client = TestClient(app)
        resp = client.post(
            "/fine-tuning/inference/presign",
            json={"filename": "input.txt", "content_type": "text/plain"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert "presigned_url" in body
        assert "s3_key" in body
        assert "inference-input" in body["s3_key"]


class TestCreateInferenceJob:

    def test_returns_201_on_success(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_jobs = MagicMock()
        mock_jobs.get_job.return_value = SAMPLE_COMPLETED_TRAINING_JOB

        mock_inf = MagicMock()
        mock_inf.create_inference_job.return_value = SAMPLE_INFERENCE_JOB
        mock_inf.update_inference_status.return_value = {**SAMPLE_INFERENCE_JOB, "status": "TRANSFORMING"}

        mock_s3 = MagicMock()
        mock_s3.check_object_exists.return_value = True
        mock_s3.bucket_name = "test-bucket"
        mock_s3.get_inference_output_s3_prefix.return_value = "inference-output/user-001/job-xyz"
        mock_s3.get_inference_output_s3_uri.return_value = "s3://test-bucket/inference-output/user-001/job-xyz"

        mock_sm = MagicMock()
        mock_sm.create_transform_job.return_value = {}

        mock_access = MagicMock()

        _setup_deps(app, user, SAMPLE_GRANT, mock_jobs, mock_inf, mock_s3, mock_sm, mock_access)

        client = TestClient(app)
        resp = client.post(
            "/fine-tuning/inference",
            json={
                "training_job_id": "train-abc123",
                "input_s3_key": "inference-input/user-001/xyz/input.txt",
            },
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["job_type"] == "inference"
        assert body["training_job_id"] == "train-abc123"

    def test_returns_400_for_nonexistent_training_job(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_jobs = MagicMock()
        mock_jobs.get_job.return_value = None

        _setup_deps(app, user, SAMPLE_GRANT, jobs_repo=mock_jobs, inf_repo=MagicMock(),
                     s3_service=MagicMock(), sagemaker=MagicMock(), access_repo=MagicMock())

        client = TestClient(app)
        resp = client.post(
            "/fine-tuning/inference",
            json={
                "training_job_id": "nonexistent",
                "input_s3_key": "inference-input/user-001/xyz/input.txt",
            },
        )

        assert resp.status_code == 400
        assert "Training job not found" in resp.json()["detail"]

    def test_returns_400_for_non_completed_training_job(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_jobs = MagicMock()
        mock_jobs.get_job.return_value = {**SAMPLE_COMPLETED_TRAINING_JOB, "status": "TRAINING"}

        _setup_deps(app, user, SAMPLE_GRANT, jobs_repo=mock_jobs, inf_repo=MagicMock(),
                     s3_service=MagicMock(), sagemaker=MagicMock(), access_repo=MagicMock())

        client = TestClient(app)
        resp = client.post(
            "/fine-tuning/inference",
            json={
                "training_job_id": "train-abc123",
                "input_s3_key": "inference-input/user-001/xyz/input.txt",
            },
        )

        assert resp.status_code == 400
        assert "not completed" in resp.json()["detail"]

    def test_returns_400_when_input_not_found(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_jobs = MagicMock()
        mock_jobs.get_job.return_value = SAMPLE_COMPLETED_TRAINING_JOB

        mock_s3 = MagicMock()
        mock_s3.check_object_exists.return_value = False

        _setup_deps(app, user, SAMPLE_GRANT, jobs_repo=mock_jobs, inf_repo=MagicMock(),
                     s3_service=mock_s3, sagemaker=MagicMock(), access_repo=MagicMock())

        client = TestClient(app)
        resp = client.post(
            "/fine-tuning/inference",
            json={
                "training_job_id": "train-abc123",
                "input_s3_key": "inference-input/user-001/xyz/input.txt",
            },
        )

        assert resp.status_code == 400
        assert "Input file not found" in resp.json()["detail"]

    def test_returns_400_when_quota_insufficient(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        low_quota = {**SAMPLE_GRANT, "monthly_quota_hours": 10.0, "current_month_usage_hours": 9.8}

        mock_jobs = MagicMock()
        mock_jobs.get_job.return_value = SAMPLE_COMPLETED_TRAINING_JOB

        mock_s3 = MagicMock()
        mock_s3.check_object_exists.return_value = True

        _setup_deps(app, user, low_quota, jobs_repo=mock_jobs, inf_repo=MagicMock(),
                     s3_service=mock_s3, sagemaker=MagicMock(), access_repo=MagicMock())

        client = TestClient(app)
        resp = client.post(
            "/fine-tuning/inference",
            json={
                "training_job_id": "train-abc123",
                "input_s3_key": "inference-input/user-001/xyz/input.txt",
            },
        )

        assert resp.status_code == 400
        assert "Insufficient quota" in resp.json()["detail"]


class TestListInferenceJobs:

    def test_returns_200_with_jobs(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_inf = MagicMock()
        mock_inf.list_user_inference_jobs.return_value = [SAMPLE_INFERENCE_JOB]
        _setup_deps(app, user, SAMPLE_GRANT, inf_repo=mock_inf)

        client = TestClient(app)
        resp = client.get("/fine-tuning/inference")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 1
        assert body["jobs"][0]["job_id"] == SAMPLE_INFERENCE_JOB["job_id"]


class TestGetInferenceJob:

    def test_returns_200_for_existing_job(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        completed_job = {**SAMPLE_INFERENCE_JOB, "status": "COMPLETED"}
        mock_inf = MagicMock()
        mock_inf.get_inference_job.return_value = completed_job

        mock_sm = MagicMock()
        mock_access = MagicMock()

        _setup_deps(app, user, SAMPLE_GRANT, inf_repo=mock_inf, sagemaker=mock_sm, access_repo=mock_access)

        client = TestClient(app)
        resp = client.get(f"/fine-tuning/inference/{SAMPLE_INFERENCE_JOB['job_id']}")

        assert resp.status_code == 200
        assert resp.json()["job_id"] == SAMPLE_INFERENCE_JOB["job_id"]

    def test_returns_404_for_nonexistent(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_inf = MagicMock()
        mock_inf.get_inference_job.return_value = None

        mock_sm = MagicMock()
        mock_access = MagicMock()

        _setup_deps(app, user, SAMPLE_GRANT, inf_repo=mock_inf, sagemaker=mock_sm, access_repo=mock_access)

        client = TestClient(app)
        resp = client.get("/fine-tuning/inference/nonexistent")

        assert resp.status_code == 404

    def test_syncs_status_for_transforming_job(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_inf = MagicMock()
        mock_inf.get_inference_job.return_value = SAMPLE_INFERENCE_JOB
        mock_inf.update_inference_status.return_value = {
            **SAMPLE_INFERENCE_JOB,
            "status": "COMPLETED",
            "billable_seconds": 1800,
            "estimated_cost_usd": 0.76,
        }

        mock_sm = MagicMock()
        mock_sm.describe_transform_job.return_value = {
            "status": "Completed",
            "transform_start_time": "2026-03-13T14:00:00+00:00",
            "transform_end_time": "2026-03-13T14:30:00+00:00",
            "billable_seconds": 1800,
        }
        mock_sm.calculate_cost.return_value = 0.76

        mock_access = MagicMock()
        mock_access.increment_usage.return_value = {}

        _setup_deps(app, user, SAMPLE_GRANT, inf_repo=mock_inf, sagemaker=mock_sm, access_repo=mock_access)

        client = TestClient(app)
        resp = client.get(f"/fine-tuning/inference/{SAMPLE_INFERENCE_JOB['job_id']}")

        assert resp.status_code == 200
        mock_sm.describe_transform_job.assert_called_once()
        mock_inf.update_inference_status.assert_called_once()


class TestGetInferenceJobLogs:

    def test_returns_200_with_logs(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_inf = MagicMock()
        mock_inf.get_inference_job.return_value = SAMPLE_INFERENCE_JOB

        mock_sm = MagicMock()
        mock_sm.get_transform_logs.return_value = ["Loading model...", "Running inference..."]

        _setup_deps(app, user, SAMPLE_GRANT, inf_repo=mock_inf, sagemaker=mock_sm)

        client = TestClient(app)
        resp = client.get(f"/fine-tuning/inference/{SAMPLE_INFERENCE_JOB['job_id']}/logs")

        assert resp.status_code == 200
        assert resp.json()["logs"] == ["Loading model...", "Running inference..."]

    def test_returns_404_for_nonexistent_job(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_inf = MagicMock()
        mock_inf.get_inference_job.return_value = None

        mock_sm = MagicMock()
        _setup_deps(app, user, SAMPLE_GRANT, inf_repo=mock_inf, sagemaker=mock_sm)

        client = TestClient(app)
        resp = client.get("/fine-tuning/inference/nonexistent/logs")

        assert resp.status_code == 404


class TestDownloadInferenceResult:

    def test_returns_200_with_download_url(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        completed_job = {**SAMPLE_INFERENCE_JOB, "status": "COMPLETED"}
        mock_inf = MagicMock()
        mock_inf.get_inference_job.return_value = completed_job

        mock_s3 = MagicMock()
        mock_s3.check_object_exists.return_value = True
        mock_s3.generate_download_url.return_value = "https://s3.amazonaws.com/bucket/results.out?sig=..."
        mock_s3.presign_expiration = 900

        _setup_deps(app, user, SAMPLE_GRANT, inf_repo=mock_inf, s3_service=mock_s3)

        client = TestClient(app)
        resp = client.get(f"/fine-tuning/inference/{SAMPLE_INFERENCE_JOB['job_id']}/download")

        assert resp.status_code == 200
        assert "download_url" in resp.json()

    def test_returns_400_for_non_completed_job(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_inf = MagicMock()
        mock_inf.get_inference_job.return_value = SAMPLE_INFERENCE_JOB  # status=TRANSFORMING

        _setup_deps(app, user, SAMPLE_GRANT, inf_repo=mock_inf, s3_service=MagicMock())

        client = TestClient(app)
        resp = client.get(f"/fine-tuning/inference/{SAMPLE_INFERENCE_JOB['job_id']}/download")

        assert resp.status_code == 400


class TestStopInferenceJob:

    def test_returns_200_on_success(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        mock_inf = MagicMock()
        mock_inf.get_inference_job.return_value = SAMPLE_INFERENCE_JOB
        mock_inf.update_inference_status.return_value = {**SAMPLE_INFERENCE_JOB, "status": "STOPPED"}

        mock_sm = MagicMock()

        _setup_deps(app, user, SAMPLE_GRANT, inf_repo=mock_inf, sagemaker=mock_sm)

        client = TestClient(app)
        resp = client.delete(f"/fine-tuning/inference/{SAMPLE_INFERENCE_JOB['job_id']}")

        assert resp.status_code == 200
        mock_sm.stop_transform_job.assert_called_once()

    def test_returns_400_for_completed_job(self, make_user):
        app = _create_app()
        user = make_user(email="user@example.com")

        completed_job = {**SAMPLE_INFERENCE_JOB, "status": "COMPLETED"}
        mock_inf = MagicMock()
        mock_inf.get_inference_job.return_value = completed_job

        _setup_deps(app, user, SAMPLE_GRANT, inf_repo=mock_inf, sagemaker=MagicMock())

        client = TestClient(app)
        resp = client.delete(f"/fine-tuning/inference/{SAMPLE_INFERENCE_JOB['job_id']}")

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

        # All inference endpoints should return 403
        assert client.get("/fine-tuning/trained-models").status_code == 403
        assert client.post("/fine-tuning/inference/presign", json={"filename": "a", "content_type": "b"}).status_code == 403
        assert client.post("/fine-tuning/inference", json={"training_job_id": "x", "input_s3_key": "y"}).status_code == 403
        assert client.get("/fine-tuning/inference").status_code == 403
        assert client.get("/fine-tuning/inference/abc").status_code == 403
        assert client.get("/fine-tuning/inference/abc/logs").status_code == 403
        assert client.get("/fine-tuning/inference/abc/download").status_code == 403
        assert client.delete("/fine-tuning/inference/abc").status_code == 403
