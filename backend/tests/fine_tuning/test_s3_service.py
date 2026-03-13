"""Unit tests for FineTuningS3Service."""

import pytest
import boto3
from unittest.mock import MagicMock, patch
from botocore.exceptions import ClientError

from apis.app_api.fine_tuning.s3_service import FineTuningS3Service


class TestGenerateUploadUrl:

    def test_returns_presigned_url_and_s3_key(self, mock_s3_bucket):
        service = FineTuningS3Service(bucket_name=mock_s3_bucket)

        url, s3_key = service.generate_upload_url(
            user_id="user-001",
            filename="train.jsonl",
            content_type="application/jsonl",
        )

        assert url.startswith("https://")
        assert "train.jsonl" in s3_key
        assert s3_key.startswith("datasets/user-001/")

    def test_s3_key_includes_user_id(self, mock_s3_bucket):
        service = FineTuningS3Service(bucket_name=mock_s3_bucket)

        _, s3_key = service.generate_upload_url(
            user_id="user-xyz",
            filename="data.csv",
            content_type="text/csv",
        )

        assert "user-xyz" in s3_key

    def test_unique_keys_for_same_filename(self, mock_s3_bucket):
        service = FineTuningS3Service(bucket_name=mock_s3_bucket)

        _, key1 = service.generate_upload_url("user-001", "train.jsonl", "application/jsonl")
        _, key2 = service.generate_upload_url("user-001", "train.jsonl", "application/jsonl")

        assert key1 != key2


class TestGenerateDownloadUrl:

    def test_returns_presigned_get_url(self, mock_s3_bucket):
        service = FineTuningS3Service(bucket_name=mock_s3_bucket)

        url = service.generate_download_url("output/user-001/job-123/model.tar.gz")

        assert url.startswith("https://")
        assert "model.tar.gz" in url


class TestCheckObjectExists:

    def test_returns_true_when_object_exists(self, mock_s3_bucket):
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.put_object(Bucket=mock_s3_bucket, Key="datasets/test.jsonl", Body=b"data")

        service = FineTuningS3Service(bucket_name=mock_s3_bucket)
        assert service.check_object_exists("datasets/test.jsonl") is True

    def test_returns_false_when_object_missing(self, mock_s3_bucket):
        service = FineTuningS3Service(bucket_name=mock_s3_bucket)
        assert service.check_object_exists("datasets/nonexistent.jsonl") is False


class TestOutputPaths:

    def test_get_output_s3_prefix(self, mock_s3_bucket):
        service = FineTuningS3Service(bucket_name=mock_s3_bucket)
        prefix = service.get_output_s3_prefix("user-001", "job-abc")
        assert prefix == "output/user-001/job-abc"

    def test_get_output_s3_uri(self, mock_s3_bucket):
        service = FineTuningS3Service(bucket_name=mock_s3_bucket)
        uri = service.get_output_s3_uri("user-001", "job-abc")
        assert uri == f"s3://{mock_s3_bucket}/output/user-001/job-abc"
