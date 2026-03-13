"""Unit tests for SageMakerService with mocked boto3 clients."""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
from botocore.exceptions import ClientError

from apis.app_api.fine_tuning.sagemaker_service import SageMakerService


@pytest.fixture
def mock_sagemaker():
    return MagicMock()


@pytest.fixture
def mock_logs():
    return MagicMock()


@pytest.fixture
def service(mock_sagemaker, mock_logs, monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("SAGEMAKER_EXECUTION_ROLE_ARN", "arn:aws:iam::123456789012:role/sagemaker-role")
    monkeypatch.setenv("SAGEMAKER_SECURITY_GROUP_ID", "sg-12345678")
    monkeypatch.setenv("SAGEMAKER_SUBNET_IDS", "subnet-aaa,subnet-bbb")
    return SageMakerService(
        sagemaker_client=mock_sagemaker,
        logs_client=mock_logs,
        role_arn="arn:aws:iam::123456789012:role/sagemaker-role",
        security_group_id="sg-12345678",
        subnet_ids="subnet-aaa,subnet-bbb",
    )


class TestCreateTrainingJob:

    def test_calls_sagemaker_with_correct_params(self, service, mock_sagemaker):
        mock_sagemaker.create_training_job.return_value = {"TrainingJobArn": "arn:aws:sagemaker:us-west-2:123:training-job/test"}

        result = service.create_training_job(
            job_name="ft-abc12345-20260313",
            hyperparameters={"epochs": "3", "model_name_or_path": "meta-llama/Meta-Llama-3-8B"},
            input_s3_uri="s3://bucket/datasets/user-001/abc/train.jsonl",
            output_s3_uri="s3://bucket/output/user-001/job-abc",
            instance_type="ml.g5.2xlarge",
        )

        mock_sagemaker.create_training_job.assert_called_once()
        call_kwargs = mock_sagemaker.create_training_job.call_args[1]

        assert call_kwargs["TrainingJobName"] == "ft-abc12345-20260313"
        assert call_kwargs["RoleArn"] == "arn:aws:iam::123456789012:role/sagemaker-role"
        assert call_kwargs["ResourceConfig"]["InstanceType"] == "ml.g5.2xlarge"
        assert call_kwargs["ResourceConfig"]["InstanceCount"] == 1
        assert call_kwargs["ResourceConfig"]["VolumeSizeInGB"] == 100
        assert call_kwargs["StoppingCondition"]["MaxRuntimeInSeconds"] == 86400
        assert call_kwargs["HyperParameters"]["epochs"] == "3"

    def test_includes_vpc_config(self, service, mock_sagemaker):
        mock_sagemaker.create_training_job.return_value = {}

        service.create_training_job(
            job_name="ft-test",
            hyperparameters={},
            input_s3_uri="s3://bucket/input",
            output_s3_uri="s3://bucket/output",
            instance_type="ml.g5.xlarge",
        )

        call_kwargs = mock_sagemaker.create_training_job.call_args[1]
        vpc = call_kwargs["VpcConfig"]
        assert vpc["SecurityGroupIds"] == ["sg-12345678"]
        assert vpc["Subnets"] == ["subnet-aaa", "subnet-bbb"]

    def test_no_vpc_config_when_empty(self, mock_sagemaker, mock_logs):
        service = SageMakerService(
            sagemaker_client=mock_sagemaker,
            logs_client=mock_logs,
            role_arn="arn:role",
            security_group_id="",
            subnet_ids="",
        )
        mock_sagemaker.create_training_job.return_value = {}

        service.create_training_job(
            job_name="ft-test",
            hyperparameters={},
            input_s3_uri="s3://bucket/input",
            output_s3_uri="s3://bucket/output",
            instance_type="ml.g5.xlarge",
        )

        call_kwargs = mock_sagemaker.create_training_job.call_args[1]
        assert "VpcConfig" not in call_kwargs


class TestCreateTrainingJobWithScripts:

    def test_injects_sagemaker_program_hp(self, service, mock_sagemaker):
        """When source_dir_s3_uri is provided, sagemaker_program HP should be set."""
        mock_sagemaker.create_training_job.return_value = {}

        service.create_training_job(
            job_name="ft-test",
            hyperparameters={"epochs": "3"},
            input_s3_uri="s3://bucket/input",
            output_s3_uri="s3://bucket/output",
            instance_type="ml.g5.2xlarge",
            source_dir_s3_uri="s3://bucket/scripts/sourcedir.tar.gz",
        )

        call_kwargs = mock_sagemaker.create_training_job.call_args[1]
        assert call_kwargs["HyperParameters"]["sagemaker_program"] == "train.py"

    def test_injects_sagemaker_submit_directory_hp(self, service, mock_sagemaker):
        """When source_dir_s3_uri is provided, sagemaker_submit_directory HP should be set."""
        mock_sagemaker.create_training_job.return_value = {}

        service.create_training_job(
            job_name="ft-test",
            hyperparameters={"epochs": "3"},
            input_s3_uri="s3://bucket/input",
            output_s3_uri="s3://bucket/output",
            instance_type="ml.g5.2xlarge",
            source_dir_s3_uri="s3://bucket/scripts/sourcedir.tar.gz",
        )

        call_kwargs = mock_sagemaker.create_training_job.call_args[1]
        assert call_kwargs["HyperParameters"]["sagemaker_submit_directory"] == "s3://bucket/scripts/sourcedir.tar.gz"

    def test_no_script_hps_when_source_dir_empty(self, service, mock_sagemaker):
        """When source_dir_s3_uri is empty, no script HPs should be injected (backward compat)."""
        mock_sagemaker.create_training_job.return_value = {}

        service.create_training_job(
            job_name="ft-test",
            hyperparameters={"epochs": "3"},
            input_s3_uri="s3://bucket/input",
            output_s3_uri="s3://bucket/output",
            instance_type="ml.g5.2xlarge",
            source_dir_s3_uri="",
        )

        call_kwargs = mock_sagemaker.create_training_job.call_args[1]
        assert "sagemaker_program" not in call_kwargs["HyperParameters"]
        assert "sagemaker_submit_directory" not in call_kwargs["HyperParameters"]

    def test_does_not_mutate_original_hyperparameters(self, service, mock_sagemaker):
        """Should not mutate the caller's hyperparameters dict when injecting script HPs."""
        mock_sagemaker.create_training_job.return_value = {}
        original_hps = {"epochs": "3"}

        service.create_training_job(
            job_name="ft-test",
            hyperparameters=original_hps,
            input_s3_uri="s3://bucket/input",
            output_s3_uri="s3://bucket/output",
            instance_type="ml.g5.2xlarge",
            source_dir_s3_uri="s3://bucket/scripts/sourcedir.tar.gz",
        )

        # Original dict should not be mutated
        assert "sagemaker_program" not in original_hps
        assert "sagemaker_submit_directory" not in original_hps


class TestDescribeTrainingJob:

    def test_returns_normalized_status(self, service, mock_sagemaker):
        start_time = datetime(2026, 3, 13, 10, 0, 0, tzinfo=timezone.utc)
        end_time = datetime(2026, 3, 13, 12, 0, 0, tzinfo=timezone.utc)
        mock_sagemaker.describe_training_job.return_value = {
            "TrainingJobStatus": "Completed",
            "SecondaryStatus": "Completed",
            "TrainingStartTime": start_time,
            "TrainingEndTime": end_time,
            "BillableTimeInSeconds": 7200,
        }

        result = service.describe_training_job("ft-test")

        assert result["status"] == "Completed"
        assert result["billable_seconds"] == 7200
        assert "training_start_time" in result
        assert "training_end_time" in result

    def test_includes_failure_reason_when_failed(self, service, mock_sagemaker):
        mock_sagemaker.describe_training_job.return_value = {
            "TrainingJobStatus": "Failed",
            "FailureReason": "ResourceLimitExceeded",
        }

        result = service.describe_training_job("ft-test")

        assert result["status"] == "Failed"
        assert result["failure_reason"] == "ResourceLimitExceeded"


class TestStopTrainingJob:

    def test_returns_true_on_success(self, service, mock_sagemaker):
        assert service.stop_training_job("ft-test") is True
        mock_sagemaker.stop_training_job.assert_called_once_with(TrainingJobName="ft-test")

    def test_returns_false_when_already_stopped(self, service, mock_sagemaker):
        mock_sagemaker.stop_training_job.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "already stopped"}},
            "StopTrainingJob",
        )

        assert service.stop_training_job("ft-test") is False


class TestGetTrainingLogs:

    def test_returns_log_messages(self, service, mock_logs):
        mock_logs.describe_log_streams.return_value = {
            "logStreams": [{"logStreamName": "ft-test/algo-1-12345"}],
        }
        mock_logs.get_log_events.return_value = {
            "events": [
                {"message": "Starting training..."},
                {"message": "Epoch 1/3 complete"},
            ],
        }

        logs = service.get_training_logs("ft-test")

        assert len(logs) == 2
        assert logs[0] == "Starting training..."

    def test_returns_empty_when_no_streams(self, service, mock_logs):
        mock_logs.describe_log_streams.return_value = {"logStreams": []}

        logs = service.get_training_logs("ft-test")
        assert logs == []

    def test_returns_empty_on_resource_not_found(self, service, mock_logs):
        mock_logs.describe_log_streams.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
            "DescribeLogStreams",
        )

        logs = service.get_training_logs("ft-test")
        assert logs == []


class TestCalculateCost:

    def test_known_instance_type(self):
        cost = SageMakerService.calculate_cost("ml.g5.2xlarge", 7200)
        assert cost == pytest.approx(3.03, abs=0.01)

    def test_unknown_instance_type_returns_zero(self):
        cost = SageMakerService.calculate_cost("ml.unknown.xlarge", 3600)
        assert cost == 0.0

    def test_zero_seconds(self):
        cost = SageMakerService.calculate_cost("ml.g5.2xlarge", 0)
        assert cost == 0.0
