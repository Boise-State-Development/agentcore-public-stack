"""Integration tests for FineTuningJobsRepository using moto DynamoDB."""

import pytest
import uuid


class TestCreateJob:

    def test_creates_item_with_all_attributes(self, jobs_repository):
        job_id = uuid.uuid4().hex
        result = jobs_repository.create_job(
            user_id="user-001",
            email="alice@example.com",
            job_id=job_id,
            model_id="meta-llama-3-8b",
            model_name="Meta Llama 3 8B",
            dataset_s3_key="datasets/user-001/abc/train.jsonl",
            instance_type="ml.g5.2xlarge",
            hyperparameters={"epochs": "3"},
            sagemaker_job_name=f"ft-{job_id[:8]}-20260313",
            output_s3_prefix=f"output/user-001/{job_id}",
        )

        assert result["job_id"] == job_id
        assert result["user_id"] == "user-001"
        assert result["email"] == "alice@example.com"
        assert result["model_id"] == "meta-llama-3-8b"
        assert result["status"] == "PENDING"
        assert result["instance_type"] == "ml.g5.2xlarge"
        assert result["hyperparameters"] == {"epochs": "3"}
        assert result["created_at"] != ""
        assert result["updated_at"] != ""

    def test_creates_item_without_hyperparameters(self, jobs_repository):
        job_id = uuid.uuid4().hex
        result = jobs_repository.create_job(
            user_id="user-001",
            email="alice@example.com",
            job_id=job_id,
            model_id="phi-3-mini-4k",
            model_name="Phi-3 Mini 4K",
            dataset_s3_key="datasets/user-001/def/train.jsonl",
            instance_type="ml.g5.xlarge",
            hyperparameters=None,
            sagemaker_job_name=f"ft-{job_id[:8]}-20260313",
            output_s3_prefix=f"output/user-001/{job_id}",
        )

        assert result["hyperparameters"] is None

    def test_default_max_runtime_is_86400(self, jobs_repository):
        job_id = uuid.uuid4().hex
        result = jobs_repository.create_job(
            user_id="user-001",
            email="alice@example.com",
            job_id=job_id,
            model_id="meta-llama-3-8b",
            model_name="Meta Llama 3 8B",
            dataset_s3_key="datasets/user-001/abc/train.jsonl",
            instance_type="ml.g5.2xlarge",
            hyperparameters=None,
            sagemaker_job_name=f"ft-{job_id[:8]}-20260313",
            output_s3_prefix=f"output/user-001/{job_id}",
        )

        assert result["max_runtime_seconds"] == 86400


class TestGetJob:

    def test_returns_none_for_nonexistent(self, jobs_repository):
        result = jobs_repository.get_job("user-001", "nonexistent-id")
        assert result is None

    def test_returns_item_for_existing(self, jobs_repository):
        job_id = uuid.uuid4().hex
        jobs_repository.create_job(
            user_id="user-001",
            email="alice@example.com",
            job_id=job_id,
            model_id="meta-llama-3-8b",
            model_name="Meta Llama 3 8B",
            dataset_s3_key="datasets/user-001/abc/train.jsonl",
            instance_type="ml.g5.2xlarge",
            hyperparameters=None,
            sagemaker_job_name=f"ft-{job_id[:8]}-20260313",
            output_s3_prefix=f"output/user-001/{job_id}",
        )

        result = jobs_repository.get_job("user-001", job_id)
        assert result is not None
        assert result["job_id"] == job_id


class TestListUserJobs:

    def test_returns_empty_for_no_jobs(self, jobs_repository):
        result = jobs_repository.list_user_jobs("user-001")
        assert result == []

    def test_returns_only_users_jobs(self, jobs_repository):
        for i, user_id in enumerate(["user-001", "user-001", "user-002"]):
            job_id = uuid.uuid4().hex
            jobs_repository.create_job(
                user_id=user_id,
                email=f"{user_id}@example.com",
                job_id=job_id,
                model_id="meta-llama-3-8b",
                model_name="Meta Llama 3 8B",
                dataset_s3_key=f"datasets/{user_id}/abc/train.jsonl",
                instance_type="ml.g5.2xlarge",
                hyperparameters=None,
                sagemaker_job_name=f"ft-{job_id[:8]}-20260313",
                output_s3_prefix=f"output/{user_id}/{job_id}",
            )

        user1_jobs = jobs_repository.list_user_jobs("user-001")
        user2_jobs = jobs_repository.list_user_jobs("user-002")

        assert len(user1_jobs) == 2
        assert len(user2_jobs) == 1
        assert all(j["user_id"] == "user-001" for j in user1_jobs)


class TestListAllJobs:

    def test_returns_all_jobs(self, jobs_repository):
        for user_id in ["user-001", "user-002"]:
            job_id = uuid.uuid4().hex
            jobs_repository.create_job(
                user_id=user_id,
                email=f"{user_id}@example.com",
                job_id=job_id,
                model_id="meta-llama-3-8b",
                model_name="Meta Llama 3 8B",
                dataset_s3_key=f"datasets/{user_id}/abc/train.jsonl",
                instance_type="ml.g5.2xlarge",
                hyperparameters=None,
                sagemaker_job_name=f"ft-{job_id[:8]}-20260313",
                output_s3_prefix=f"output/{user_id}/{job_id}",
            )

        result = jobs_repository.list_all_jobs()
        assert len(result) == 2

    def test_filters_by_status(self, jobs_repository):
        job_id_1 = uuid.uuid4().hex
        jobs_repository.create_job(
            user_id="user-001",
            email="alice@example.com",
            job_id=job_id_1,
            model_id="meta-llama-3-8b",
            model_name="Meta Llama 3 8B",
            dataset_s3_key="datasets/user-001/abc/train.jsonl",
            instance_type="ml.g5.2xlarge",
            hyperparameters=None,
            sagemaker_job_name=f"ft-{job_id_1[:8]}-20260313",
            output_s3_prefix=f"output/user-001/{job_id_1}",
        )
        # Update first job to TRAINING
        jobs_repository.update_job_status("user-001", job_id_1, "TRAINING")

        job_id_2 = uuid.uuid4().hex
        jobs_repository.create_job(
            user_id="user-002",
            email="bob@example.com",
            job_id=job_id_2,
            model_id="meta-llama-3-8b",
            model_name="Meta Llama 3 8B",
            dataset_s3_key="datasets/user-002/abc/train.jsonl",
            instance_type="ml.g5.2xlarge",
            hyperparameters=None,
            sagemaker_job_name=f"ft-{job_id_2[:8]}-20260313",
            output_s3_prefix=f"output/user-002/{job_id_2}",
        )

        training_jobs = jobs_repository.list_all_jobs(status_filter="TRAINING")
        pending_jobs = jobs_repository.list_all_jobs(status_filter="PENDING")

        assert len(training_jobs) == 1
        assert training_jobs[0]["job_id"] == job_id_1
        assert len(pending_jobs) == 1
        assert pending_jobs[0]["job_id"] == job_id_2


class TestUpdateJobStatus:

    def test_updates_status(self, jobs_repository):
        job_id = uuid.uuid4().hex
        jobs_repository.create_job(
            user_id="user-001",
            email="alice@example.com",
            job_id=job_id,
            model_id="meta-llama-3-8b",
            model_name="Meta Llama 3 8B",
            dataset_s3_key="datasets/user-001/abc/train.jsonl",
            instance_type="ml.g5.2xlarge",
            hyperparameters=None,
            sagemaker_job_name=f"ft-{job_id[:8]}-20260313",
            output_s3_prefix=f"output/user-001/{job_id}",
        )

        result = jobs_repository.update_job_status("user-001", job_id, "TRAINING")
        assert result is not None
        assert result["status"] == "TRAINING"

    def test_updates_with_optional_fields(self, jobs_repository):
        job_id = uuid.uuid4().hex
        jobs_repository.create_job(
            user_id="user-001",
            email="alice@example.com",
            job_id=job_id,
            model_id="meta-llama-3-8b",
            model_name="Meta Llama 3 8B",
            dataset_s3_key="datasets/user-001/abc/train.jsonl",
            instance_type="ml.g5.2xlarge",
            hyperparameters=None,
            sagemaker_job_name=f"ft-{job_id[:8]}-20260313",
            output_s3_prefix=f"output/user-001/{job_id}",
        )

        result = jobs_repository.update_job_status(
            "user-001", job_id, "COMPLETED",
            training_start_time="2026-03-13T10:00:00+00:00",
            training_end_time="2026-03-13T12:00:00+00:00",
            billable_seconds=7200,
            estimated_cost_usd=3.03,
        )

        assert result["status"] == "COMPLETED"
        assert result["training_start_time"] == "2026-03-13T10:00:00+00:00"
        assert result["training_end_time"] == "2026-03-13T12:00:00+00:00"
        assert result["billable_seconds"] == 7200
        assert result["estimated_cost_usd"] == pytest.approx(3.03)

    def test_returns_none_for_nonexistent(self, jobs_repository):
        result = jobs_repository.update_job_status("user-001", "nonexistent", "TRAINING")
        assert result is None

    def test_updates_error_message(self, jobs_repository):
        job_id = uuid.uuid4().hex
        jobs_repository.create_job(
            user_id="user-001",
            email="alice@example.com",
            job_id=job_id,
            model_id="meta-llama-3-8b",
            model_name="Meta Llama 3 8B",
            dataset_s3_key="datasets/user-001/abc/train.jsonl",
            instance_type="ml.g5.2xlarge",
            hyperparameters=None,
            sagemaker_job_name=f"ft-{job_id[:8]}-20260313",
            output_s3_prefix=f"output/user-001/{job_id}",
        )

        result = jobs_repository.update_job_status(
            "user-001", job_id, "FAILED",
            error_message="Out of memory",
        )

        assert result["status"] == "FAILED"
        assert result["error_message"] == "Out of memory"


class TestDeleteJob:

    def test_deletes_item(self, jobs_repository):
        job_id = uuid.uuid4().hex
        jobs_repository.create_job(
            user_id="user-001",
            email="alice@example.com",
            job_id=job_id,
            model_id="meta-llama-3-8b",
            model_name="Meta Llama 3 8B",
            dataset_s3_key="datasets/user-001/abc/train.jsonl",
            instance_type="ml.g5.2xlarge",
            hyperparameters=None,
            sagemaker_job_name=f"ft-{job_id[:8]}-20260313",
            output_s3_prefix=f"output/user-001/{job_id}",
        )

        assert jobs_repository.delete_job("user-001", job_id) is True
        assert jobs_repository.get_job("user-001", job_id) is None

    def test_returns_false_for_nonexistent(self, jobs_repository):
        assert jobs_repository.delete_job("user-001", "nonexistent") is False


class TestQueryJobsByStatusAndDate:
    """The cost dashboard's GSI query.

    Training and inference records share one table and one StatusIndex, so this
    query has to return training rows only — otherwise a caller that also
    queries the inference repository counts an inference job's cost twice.
    """

    def _completed_training_job(self, jobs_repository, job_id):
        jobs_repository.create_job(
            user_id="user-001",
            email="alice@example.com",
            job_id=job_id,
            model_id="electra-tiny",
            model_name="ELECTRA Tiny",
            dataset_s3_key="datasets/user-001/abc/train.csv",
            instance_type="ml.g5.xlarge",
            hyperparameters={"epochs": "3"},
            sagemaker_job_name=f"ft-{job_id[:8]}",
            output_s3_prefix=f"output/user-001/{job_id}",
        )
        jobs_repository.update_job_status(
            user_id="user-001",
            job_id=job_id,
            status="COMPLETED",
            billable_seconds=300,
            estimated_cost_usd=0.1175,
        )

    def _completed_inference_job(self, inference_repository, job_id):
        inference_repository.create_inference_job(
            user_id="user-001",
            email="alice@example.com",
            job_id=job_id,
            training_job_id="train-001",
            model_name="ELECTRA Tiny",
            model_s3_path="s3://bucket/model.tar.gz",
            input_s3_key="inference-input/user-001/in.txt",
            instance_type="ml.g5.xlarge",
            transform_job_name=f"inf-{job_id[:8]}",
            output_s3_prefix=f"inference-output/user-001/{job_id}",
        )
        inference_repository.update_inference_status(
            user_id="user-001",
            job_id=job_id,
            status="COMPLETED",
            billable_seconds=230,
            estimated_cost_usd=0.0901,
        )

    @staticmethod
    def _range():
        return "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00"

    def test_returns_matching_training_job(self, jobs_repository):
        job_id = uuid.uuid4().hex
        self._completed_training_job(jobs_repository, job_id)
        start, end = self._range()

        results = jobs_repository.query_jobs_by_status_and_date("COMPLETED", start, end)

        assert [j["job_id"] for j in results] == [job_id]

    def test_stored_status_casing_is_what_matches(self, jobs_repository):
        """SageMaker spells it "Completed"; the record stores "COMPLETED"."""
        self._completed_training_job(jobs_repository, uuid.uuid4().hex)
        start, end = self._range()

        assert jobs_repository.query_jobs_by_status_and_date("Completed", start, end) == []
        assert len(jobs_repository.query_jobs_by_status_and_date("COMPLETED", start, end)) == 1

    def test_excludes_inference_jobs(self, jobs_repository, inference_repository):
        training_id = uuid.uuid4().hex
        self._completed_training_job(jobs_repository, training_id)
        self._completed_inference_job(inference_repository, uuid.uuid4().hex)
        start, end = self._range()

        results = jobs_repository.query_jobs_by_status_and_date("COMPLETED", start, end)

        assert [j["job_id"] for j in results] == [training_id]

    def test_inference_query_excludes_training_jobs(
        self, jobs_repository, inference_repository
    ):
        self._completed_training_job(jobs_repository, uuid.uuid4().hex)
        inference_id = uuid.uuid4().hex
        self._completed_inference_job(inference_repository, inference_id)
        start, end = self._range()

        results = inference_repository.query_jobs_by_status_and_date(
            "COMPLETED", start, end
        )

        assert [j["job_id"] for j in results] == [inference_id]

    def test_excludes_jobs_outside_the_period(self, jobs_repository):
        self._completed_training_job(jobs_repository, uuid.uuid4().hex)

        results = jobs_repository.query_jobs_by_status_and_date(
            "COMPLETED", "2000-01-01T00:00:00+00:00", "2000-02-01T00:00:00+00:00"
        )

        assert results == []
