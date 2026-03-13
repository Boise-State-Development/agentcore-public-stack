"""Integration tests for InferenceRepository using moto DynamoDB."""

import pytest
import uuid


class TestCreateInferenceJob:

    def test_creates_item_with_all_attributes(self, inference_repository):
        job_id = uuid.uuid4().hex
        result = inference_repository.create_inference_job(
            user_id="user-001",
            email="alice@example.com",
            job_id=job_id,
            training_job_id="train-abc123",
            model_name="Meta Llama 3 8B",
            model_s3_path="s3://bucket/output/user-001/train-abc123/ft-abc12345/output/model.tar.gz",
            input_s3_key="inference-input/user-001/xyz/input.txt",
            instance_type="ml.g5.2xlarge",
            transform_job_name=f"inf-{job_id[:8]}-20260313",
            output_s3_prefix=f"inference-output/user-001/{job_id}",
        )

        assert result["job_id"] == job_id
        assert result["user_id"] == "user-001"
        assert result["email"] == "alice@example.com"
        assert result["job_type"] == "inference"
        assert result["training_job_id"] == "train-abc123"
        assert result["model_name"] == "Meta Llama 3 8B"
        assert result["status"] == "PENDING"
        assert result["instance_type"] == "ml.g5.2xlarge"
        assert result["created_at"] != ""
        assert result["updated_at"] != ""

    def test_default_max_runtime_is_3600(self, inference_repository):
        job_id = uuid.uuid4().hex
        result = inference_repository.create_inference_job(
            user_id="user-001",
            email="alice@example.com",
            job_id=job_id,
            training_job_id="train-abc123",
            model_name="Meta Llama 3 8B",
            model_s3_path="s3://bucket/model.tar.gz",
            input_s3_key="inference-input/user-001/xyz/input.txt",
            instance_type="ml.g5.2xlarge",
            transform_job_name=f"inf-{job_id[:8]}-20260313",
            output_s3_prefix=f"inference-output/user-001/{job_id}",
        )

        assert result["max_runtime_seconds"] == 3600

    def test_uses_inf_sk_prefix(self, inference_repository):
        """Verify the INF# SK prefix is used (not JOB#)."""
        job_id = uuid.uuid4().hex
        inference_repository.create_inference_job(
            user_id="user-001",
            email="alice@example.com",
            job_id=job_id,
            training_job_id="train-abc123",
            model_name="Test Model",
            model_s3_path="s3://bucket/model.tar.gz",
            input_s3_key="inference-input/user-001/xyz/input.txt",
            instance_type="ml.g5.xlarge",
            transform_job_name=f"inf-{job_id[:8]}-20260313",
            output_s3_prefix=f"inference-output/user-001/{job_id}",
        )

        # Direct table get to verify SK prefix
        raw = inference_repository._table.get_item(
            Key={"PK": f"USER#user-001", "SK": f"INF#{job_id}"}
        )
        assert raw.get("Item") is not None
        assert raw["Item"]["SK"] == f"INF#{job_id}"


class TestGetInferenceJob:

    def test_returns_none_for_nonexistent(self, inference_repository):
        result = inference_repository.get_inference_job("user-001", "nonexistent-id")
        assert result is None

    def test_returns_item_for_existing(self, inference_repository):
        job_id = uuid.uuid4().hex
        inference_repository.create_inference_job(
            user_id="user-001",
            email="alice@example.com",
            job_id=job_id,
            training_job_id="train-abc123",
            model_name="Meta Llama 3 8B",
            model_s3_path="s3://bucket/model.tar.gz",
            input_s3_key="inference-input/user-001/xyz/input.txt",
            instance_type="ml.g5.2xlarge",
            transform_job_name=f"inf-{job_id[:8]}-20260313",
            output_s3_prefix=f"inference-output/user-001/{job_id}",
        )

        result = inference_repository.get_inference_job("user-001", job_id)
        assert result is not None
        assert result["job_id"] == job_id
        assert result["job_type"] == "inference"


class TestListUserInferenceJobs:

    def test_returns_empty_for_no_jobs(self, inference_repository):
        result = inference_repository.list_user_inference_jobs("user-001")
        assert result == []

    def test_returns_only_inference_jobs(self, inference_repository, jobs_repository):
        """Inference listing should not include training jobs in the same table."""
        # Create a training job (JOB# SK)
        train_job_id = uuid.uuid4().hex
        jobs_repository.create_job(
            user_id="user-001",
            email="alice@example.com",
            job_id=train_job_id,
            model_id="meta-llama-3-8b",
            model_name="Meta Llama 3 8B",
            dataset_s3_key="datasets/user-001/abc/train.jsonl",
            instance_type="ml.g5.2xlarge",
            hyperparameters=None,
            sagemaker_job_name=f"ft-{train_job_id[:8]}-20260313",
            output_s3_prefix=f"output/user-001/{train_job_id}",
        )

        # Create an inference job (INF# SK)
        inf_job_id = uuid.uuid4().hex
        inference_repository.create_inference_job(
            user_id="user-001",
            email="alice@example.com",
            job_id=inf_job_id,
            training_job_id=train_job_id,
            model_name="Meta Llama 3 8B",
            model_s3_path="s3://bucket/model.tar.gz",
            input_s3_key="inference-input/user-001/xyz/input.txt",
            instance_type="ml.g5.2xlarge",
            transform_job_name=f"inf-{inf_job_id[:8]}-20260313",
            output_s3_prefix=f"inference-output/user-001/{inf_job_id}",
        )

        inf_jobs = inference_repository.list_user_inference_jobs("user-001")
        train_jobs = jobs_repository.list_user_jobs("user-001")

        assert len(inf_jobs) == 1
        assert inf_jobs[0]["job_id"] == inf_job_id
        assert len(train_jobs) == 1
        assert train_jobs[0]["job_id"] == train_job_id

    def test_returns_only_users_jobs(self, inference_repository):
        for user_id in ["user-001", "user-001", "user-002"]:
            job_id = uuid.uuid4().hex
            inference_repository.create_inference_job(
                user_id=user_id,
                email=f"{user_id}@example.com",
                job_id=job_id,
                training_job_id="train-abc",
                model_name="Test Model",
                model_s3_path="s3://bucket/model.tar.gz",
                input_s3_key=f"inference-input/{user_id}/xyz/input.txt",
                instance_type="ml.g5.xlarge",
                transform_job_name=f"inf-{job_id[:8]}-20260313",
                output_s3_prefix=f"inference-output/{user_id}/{job_id}",
            )

        user1_jobs = inference_repository.list_user_inference_jobs("user-001")
        user2_jobs = inference_repository.list_user_inference_jobs("user-002")

        assert len(user1_jobs) == 2
        assert len(user2_jobs) == 1
        assert all(j["user_id"] == "user-001" for j in user1_jobs)


class TestListAllInferenceJobs:

    def test_returns_all_inference_jobs(self, inference_repository):
        for user_id in ["user-001", "user-002"]:
            job_id = uuid.uuid4().hex
            inference_repository.create_inference_job(
                user_id=user_id,
                email=f"{user_id}@example.com",
                job_id=job_id,
                training_job_id="train-abc",
                model_name="Test Model",
                model_s3_path="s3://bucket/model.tar.gz",
                input_s3_key=f"inference-input/{user_id}/xyz/input.txt",
                instance_type="ml.g5.xlarge",
                transform_job_name=f"inf-{job_id[:8]}-20260313",
                output_s3_prefix=f"inference-output/{user_id}/{job_id}",
            )

        result = inference_repository.list_all_inference_jobs()
        assert len(result) == 2

    def test_filters_by_status(self, inference_repository):
        job_id_1 = uuid.uuid4().hex
        inference_repository.create_inference_job(
            user_id="user-001",
            email="alice@example.com",
            job_id=job_id_1,
            training_job_id="train-abc",
            model_name="Test Model",
            model_s3_path="s3://bucket/model.tar.gz",
            input_s3_key="inference-input/user-001/xyz/input.txt",
            instance_type="ml.g5.xlarge",
            transform_job_name=f"inf-{job_id_1[:8]}-20260313",
            output_s3_prefix=f"inference-output/user-001/{job_id_1}",
        )
        inference_repository.update_inference_status("user-001", job_id_1, "TRANSFORMING")

        job_id_2 = uuid.uuid4().hex
        inference_repository.create_inference_job(
            user_id="user-002",
            email="bob@example.com",
            job_id=job_id_2,
            training_job_id="train-def",
            model_name="Test Model",
            model_s3_path="s3://bucket/model.tar.gz",
            input_s3_key="inference-input/user-002/xyz/input.txt",
            instance_type="ml.g5.xlarge",
            transform_job_name=f"inf-{job_id_2[:8]}-20260313",
            output_s3_prefix=f"inference-output/user-002/{job_id_2}",
        )

        transforming = inference_repository.list_all_inference_jobs(status_filter="TRANSFORMING")
        pending = inference_repository.list_all_inference_jobs(status_filter="PENDING")

        assert len(transforming) == 1
        assert transforming[0]["job_id"] == job_id_1
        assert len(pending) == 1
        assert pending[0]["job_id"] == job_id_2


class TestUpdateInferenceStatus:

    def test_updates_status(self, inference_repository):
        job_id = uuid.uuid4().hex
        inference_repository.create_inference_job(
            user_id="user-001",
            email="alice@example.com",
            job_id=job_id,
            training_job_id="train-abc",
            model_name="Test Model",
            model_s3_path="s3://bucket/model.tar.gz",
            input_s3_key="inference-input/user-001/xyz/input.txt",
            instance_type="ml.g5.xlarge",
            transform_job_name=f"inf-{job_id[:8]}-20260313",
            output_s3_prefix=f"inference-output/user-001/{job_id}",
        )

        result = inference_repository.update_inference_status("user-001", job_id, "TRANSFORMING")
        assert result is not None
        assert result["status"] == "TRANSFORMING"

    def test_updates_with_optional_fields(self, inference_repository):
        job_id = uuid.uuid4().hex
        inference_repository.create_inference_job(
            user_id="user-001",
            email="alice@example.com",
            job_id=job_id,
            training_job_id="train-abc",
            model_name="Test Model",
            model_s3_path="s3://bucket/model.tar.gz",
            input_s3_key="inference-input/user-001/xyz/input.txt",
            instance_type="ml.g5.2xlarge",
            transform_job_name=f"inf-{job_id[:8]}-20260313",
            output_s3_prefix=f"inference-output/user-001/{job_id}",
        )

        result = inference_repository.update_inference_status(
            "user-001", job_id, "COMPLETED",
            transform_start_time="2026-03-13T10:00:00+00:00",
            transform_end_time="2026-03-13T10:30:00+00:00",
            billable_seconds=1800,
            estimated_cost_usd=0.76,
        )

        assert result["status"] == "COMPLETED"
        assert result["transform_start_time"] == "2026-03-13T10:00:00+00:00"
        assert result["transform_end_time"] == "2026-03-13T10:30:00+00:00"
        assert result["billable_seconds"] == 1800
        assert result["estimated_cost_usd"] == pytest.approx(0.76)

    def test_returns_none_for_nonexistent(self, inference_repository):
        result = inference_repository.update_inference_status("user-001", "nonexistent", "TRANSFORMING")
        assert result is None

    def test_updates_error_message(self, inference_repository):
        job_id = uuid.uuid4().hex
        inference_repository.create_inference_job(
            user_id="user-001",
            email="alice@example.com",
            job_id=job_id,
            training_job_id="train-abc",
            model_name="Test Model",
            model_s3_path="s3://bucket/model.tar.gz",
            input_s3_key="inference-input/user-001/xyz/input.txt",
            instance_type="ml.g5.xlarge",
            transform_job_name=f"inf-{job_id[:8]}-20260313",
            output_s3_prefix=f"inference-output/user-001/{job_id}",
        )

        result = inference_repository.update_inference_status(
            "user-001", job_id, "FAILED",
            error_message="Model loading failed",
        )

        assert result["status"] == "FAILED"
        assert result["error_message"] == "Model loading failed"

    def test_updates_result_s3_key(self, inference_repository):
        job_id = uuid.uuid4().hex
        inference_repository.create_inference_job(
            user_id="user-001",
            email="alice@example.com",
            job_id=job_id,
            training_job_id="train-abc",
            model_name="Test Model",
            model_s3_path="s3://bucket/model.tar.gz",
            input_s3_key="inference-input/user-001/xyz/input.txt",
            instance_type="ml.g5.xlarge",
            transform_job_name=f"inf-{job_id[:8]}-20260313",
            output_s3_prefix=f"inference-output/user-001/{job_id}",
        )

        result = inference_repository.update_inference_status(
            "user-001", job_id, "COMPLETED",
            result_s3_key=f"inference-output/user-001/{job_id}/input.txt.out",
        )

        assert result["status"] == "COMPLETED"
        assert result["result_s3_key"] == f"inference-output/user-001/{job_id}/input.txt.out"
