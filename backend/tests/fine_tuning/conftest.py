"""Shared fixtures for fine-tuning tests."""

import pytest
import boto3
from moto import mock_aws
from typing import Optional, List

from apis.shared.auth.models import User


@pytest.fixture()
def aws(monkeypatch):
    """Activate moto mock_aws and set default env vars."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    with mock_aws():
        yield


@pytest.fixture()
def fine_tuning_access_table(aws, monkeypatch):
    """Create the fine-tuning-access DynamoDB table in moto."""
    table_name = "test-fine-tuning-access"
    monkeypatch.setenv("DYNAMODB_FINE_TUNING_ACCESS_TABLE_NAME", table_name)

    client = boto3.client("dynamodb", region_name="us-east-1")
    client.create_table(
        TableName=table_name,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return boto3.resource("dynamodb", region_name="us-east-1").Table(table_name)


@pytest.fixture()
def repository(fine_tuning_access_table):
    """Instantiate a FineTuningAccessRepository against the moto table."""
    from apis.app_api.fine_tuning.repository import FineTuningAccessRepository
    return FineTuningAccessRepository(table_name="test-fine-tuning-access")


@pytest.fixture()
def fine_tuning_jobs_table(aws, monkeypatch):
    """Create the fine-tuning-jobs DynamoDB table in moto."""
    table_name = "test-fine-tuning-jobs"
    monkeypatch.setenv("DYNAMODB_FINE_TUNING_JOBS_TABLE_NAME", table_name)

    client = boto3.client("dynamodb", region_name="us-east-1")
    client.create_table(
        TableName=table_name,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
            {"AttributeName": "createdAt", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "StatusIndex",
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": "createdAt", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return boto3.resource("dynamodb", region_name="us-east-1").Table(table_name)


@pytest.fixture()
def jobs_repository(fine_tuning_jobs_table):
    """Instantiate a FineTuningJobsRepository against the moto table."""
    from apis.app_api.fine_tuning.job_repository import FineTuningJobsRepository
    return FineTuningJobsRepository(table_name="test-fine-tuning-jobs")


@pytest.fixture()
def inference_repository(fine_tuning_jobs_table):
    """Instantiate an InferenceRepository against the moto table (same table as training)."""
    from apis.app_api.fine_tuning.inference_repository import InferenceRepository
    return InferenceRepository(table_name="test-fine-tuning-jobs")


@pytest.fixture()
def mock_s3_bucket(aws, monkeypatch):
    """Create an S3 bucket in moto for presigned URL tests."""
    bucket_name = "test-fine-tuning-data"
    monkeypatch.setenv("S3_FINE_TUNING_BUCKET_NAME", bucket_name)
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=bucket_name)
    return bucket_name


@pytest.fixture
def make_user():
    """Factory for creating test User objects."""

    def _make_user(
        email: str = "test@example.com",
        user_id: str = "user-001",
        name: str = "Test User",
        roles: Optional[List[str]] = None,
    ) -> User:
        return User(
            email=email,
            user_id=user_id,
            name=name,
            roles=roles if roles is not None else ["User"],
        )

    return _make_user
