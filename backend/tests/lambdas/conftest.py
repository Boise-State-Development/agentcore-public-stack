"""Lambda-handler test fixtures (moto).

Mirrors the assistants-table / sessions-metadata-table fixtures from
tests/shared/conftest.py — pytest dir-scoped conftests don't share
fixtures across sibling test packages, and the kb-sync and scheduled-runs
Lambda tests need the same table shapes the services use (including the
sparse DueSyncIndex / DueScheduleIndex).
"""

import boto3
import pytest
from moto import mock_aws

AWS_REGION = "us-east-1"


@pytest.fixture()
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    with mock_aws():
        yield


def _gsi(index_name, hash_key, range_key):
    return {
        "IndexName": index_name,
        "KeySchema": [
            {"AttributeName": hash_key, "KeyType": "HASH"},
            {"AttributeName": range_key, "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "ALL"},
    }


@pytest.fixture()
def assistants_table(aws, monkeypatch):
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    name = "test-assistants"
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", name)

    ddb.create_table(
        TableName=name,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI_PK", "AttributeType": "S"},
            {"AttributeName": "GSI_SK", "AttributeType": "S"},
            {"AttributeName": "GSI4_PK", "AttributeType": "S"},
            {"AttributeName": "GSI4_SK", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            _gsi("OwnerStatusIndex", "GSI_PK", "GSI_SK"),
            _gsi("DueSyncIndex", "GSI4_PK", "GSI4_SK"),
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(name)


@pytest.fixture()
def sessions_metadata_table(aws, monkeypatch):
    """Sessions-metadata table shape used by scheduled_prompts + harness
    (schedules, the sparse DueScheduleIndex, and RUN# audit records)."""
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    name = "test-sessions-metadata"
    monkeypatch.setenv("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", name)

    ddb.create_table(
        TableName=name,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
            {"AttributeName": "GSI_PK", "AttributeType": "S"},
            {"AttributeName": "GSI_SK", "AttributeType": "S"},
            {"AttributeName": "GSI3_PK", "AttributeType": "S"},
            {"AttributeName": "GSI3_SK", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            _gsi("UserTimestampIndex", "GSI1PK", "GSI1SK"),
            _gsi("SessionLookupIndex", "GSI_PK", "GSI_SK"),
            _gsi("DueScheduleIndex", "GSI3_PK", "GSI3_SK"),
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(name)


@pytest.fixture()
def bff_sessions_table(aws, monkeypatch):
    """BFF sessions table shape used by HeadlessGrantService (sparse
    HeadlessGrantUserIndex GSI)."""
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    name = "test-bff-sessions"
    monkeypatch.setenv("BFF_SESSIONS_TABLE_NAME", name)

    ddb.create_table(
        TableName=name,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "grant_user_id", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[
            _gsi("HeadlessGrantUserIndex", "grant_user_id", "created_at"),
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(name)
