"""Unit tests for SageMaker Batch Transform methods with mocked boto3 clients."""

import pytest
from unittest.mock import MagicMock
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


class TestCreateTransformJob:

    def test_creates_model_and_transform_job(self, service, mock_sagemaker):
        mock_sagemaker.create_model.return_value = {}
        mock_sagemaker.create_transform_job.return_value = {"TransformJobArn": "arn:..."}

        result = service.create_transform_job(
            job_name="inf-abc12345-20260313",
            model_artifact_s3_uri="s3://bucket/output/user-001/job-abc/ft-abc12345/output/model.tar.gz",
            input_s3_uri="s3://bucket/inference-input/user-001/xyz/input.txt",
            output_s3_uri="s3://bucket/inference-output/user-001/job-def",
            instance_type="ml.g5.2xlarge",
        )

        # Verify model creation
        mock_sagemaker.create_model.assert_called_once()
        model_kwargs = mock_sagemaker.create_model.call_args[1]
        assert model_kwargs["ModelName"] == "model-inf-abc12345-20260313"
        assert model_kwargs["ExecutionRoleArn"] == "arn:aws:iam::123456789012:role/sagemaker-role"
        assert "ModelDataUrl" in model_kwargs["PrimaryContainer"]

        # Verify transform job creation
        mock_sagemaker.create_transform_job.assert_called_once()
        transform_kwargs = mock_sagemaker.create_transform_job.call_args[1]
        assert transform_kwargs["TransformJobName"] == "inf-abc12345-20260313"
        assert transform_kwargs["ModelName"] == "model-inf-abc12345-20260313"
        assert transform_kwargs["TransformResources"]["InstanceType"] == "ml.g5.2xlarge"
        assert transform_kwargs["TransformResources"]["InstanceCount"] == 1

    def test_includes_vpc_config_on_model(self, service, mock_sagemaker):
        mock_sagemaker.create_model.return_value = {}
        mock_sagemaker.create_transform_job.return_value = {}

        service.create_transform_job(
            job_name="inf-test",
            model_artifact_s3_uri="s3://bucket/model.tar.gz",
            input_s3_uri="s3://bucket/input",
            output_s3_uri="s3://bucket/output",
            instance_type="ml.g5.xlarge",
        )

        model_kwargs = mock_sagemaker.create_model.call_args[1]
        vpc = model_kwargs["VpcConfig"]
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
        mock_sagemaker.create_model.return_value = {}
        mock_sagemaker.create_transform_job.return_value = {}

        service.create_transform_job(
            job_name="inf-test",
            model_artifact_s3_uri="s3://bucket/model.tar.gz",
            input_s3_uri="s3://bucket/input",
            output_s3_uri="s3://bucket/output",
            instance_type="ml.g5.xlarge",
        )

        model_kwargs = mock_sagemaker.create_model.call_args[1]
        assert "VpcConfig" not in model_kwargs


class TestDescribeTransformJob:

    def test_returns_normalized_status(self, service, mock_sagemaker):
        start_time = datetime(2026, 3, 13, 10, 0, 0, tzinfo=timezone.utc)
        end_time = datetime(2026, 3, 13, 10, 30, 0, tzinfo=timezone.utc)
        mock_sagemaker.describe_transform_job.return_value = {
            "TransformJobStatus": "Completed",
            "TransformStartTime": start_time,
            "TransformEndTime": end_time,
        }

        result = service.describe_transform_job("inf-test")

        assert result["status"] == "Completed"
        assert result["billable_seconds"] == 1800
        assert "transform_start_time" in result
        assert "transform_end_time" in result

    def test_includes_failure_reason_when_failed(self, service, mock_sagemaker):
        mock_sagemaker.describe_transform_job.return_value = {
            "TransformJobStatus": "Failed",
            "FailureReason": "ModelLoadError",
        }

        result = service.describe_transform_job("inf-test")

        assert result["status"] == "Failed"
        assert result["failure_reason"] == "ModelLoadError"


class TestStopTransformJob:

    def test_returns_true_on_success(self, service, mock_sagemaker):
        assert service.stop_transform_job("inf-test") is True
        mock_sagemaker.stop_transform_job.assert_called_once_with(TransformJobName="inf-test")

    def test_returns_false_when_already_stopped(self, service, mock_sagemaker):
        mock_sagemaker.stop_transform_job.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "already stopped"}},
            "StopTransformJob",
        )

        assert service.stop_transform_job("inf-test") is False


class TestGetTransformLogs:

    def test_returns_log_messages(self, service, mock_logs):
        mock_logs.describe_log_streams.return_value = {
            "logStreams": [{"logStreamName": "inf-test/i-12345"}],
        }
        mock_logs.get_log_events.return_value = {
            "events": [
                {"message": "Loading model..."},
                {"message": "Running inference..."},
            ],
        }

        logs = service.get_transform_logs("inf-test")

        assert len(logs) == 2
        assert logs[0] == "Loading model..."
        mock_logs.describe_log_streams.assert_called_once()
        call_kwargs = mock_logs.describe_log_streams.call_args[1]
        assert call_kwargs["logGroupName"] == "/aws/sagemaker/TransformJobs"
        assert call_kwargs["logStreamNamePrefix"] == "inf-test/"

    def test_returns_empty_when_no_streams(self, service, mock_logs):
        mock_logs.describe_log_streams.return_value = {"logStreams": []}

        logs = service.get_transform_logs("inf-test")
        assert logs == []

    def test_returns_empty_on_resource_not_found(self, service, mock_logs):
        mock_logs.describe_log_streams.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
            "DescribeLogStreams",
        )

        logs = service.get_transform_logs("inf-test")
        assert logs == []
