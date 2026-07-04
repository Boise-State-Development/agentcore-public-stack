"""Lambda-handler test fixtures (moto).

Mirrors the assistants-table fixture from tests/shared/conftest.py —
pytest dir-scoped conftests don't share fixtures across sibling test
packages, and the kb-sync Lambda tests need the same table shape the
services use (including the sparse DueSyncIndex).
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


@pytest.fixture()
def assistants_table(aws, monkeypatch):
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    name = "test-assistants"
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", name)

    def gsi(index_name, hash_key, range_key):
        return {
            "IndexName": index_name,
            "KeySchema": [
                {"AttributeName": hash_key, "KeyType": "HASH"},
                {"AttributeName": range_key, "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }

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
            gsi("OwnerStatusIndex", "GSI_PK", "GSI_SK"),
            gsi("DueSyncIndex", "GSI4_PK", "GSI4_SK"),
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(name)
