"""One LEDGER#DEPLOY row per build a service has run (release-ledger spec §5.2)."""

from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

from apis.shared.costs import deploy_marker

TABLE = "test-system-cost-rollup"


@pytest.fixture()
def rollup_table(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv(deploy_marker.ROLLUP_TABLE_ENV, TABLE)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(deploy_marker, "APP_VERSION", "1.33.0")
        monkeypatch.setattr(deploy_marker, "APP_BUILD", "8f676bb2e1a4c0d2")
        monkeypatch.setattr("apis.shared.aws_clients.get_dynamodb_table", lambda name, region_name=None: ddb.Table(name))
        yield table


def test_item_is_content_free_and_keyed_by_service_and_build(monkeypatch):
    monkeypatch.setattr(deploy_marker, "APP_VERSION", "1.33.0")
    monkeypatch.setattr(deploy_marker, "APP_BUILD", "abc123")
    item = deploy_marker.deploy_marker_item("app-api", now=datetime(2026, 11, 6, 22, 0, tzinfo=timezone.utc))
    assert item == {
        "PK": "LEDGER#DEPLOY",
        "SK": "app-api#abc123",
        "service": "app-api",
        "appVersion": "1.33.0",
        "build": "abc123",
        "firstSeenAt": "2026-11-06T22:00:00+00:00",
    }


def test_no_build_means_no_row(monkeypatch):
    """A local run or an image built outside scripts/build has nothing to mark."""
    monkeypatch.setattr(deploy_marker, "APP_BUILD", deploy_marker.UNKNOWN)
    assert deploy_marker.deploy_marker_item("app-api") is None
    assert deploy_marker.record_deploy_marker("app-api") is False


def test_writes_once_per_build(rollup_table):
    assert deploy_marker.record_deploy_marker("app-api") is True
    # The second task of the same build, and every restart after it.
    assert deploy_marker.record_deploy_marker("app-api") is False
    rows = rollup_table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq("LEDGER#DEPLOY")
    )["Items"]
    assert [r["SK"] for r in rows] == ["app-api#8f676bb2e1a4c0d2"]
    assert rows[0]["appVersion"] == "1.33.0"


def test_without_the_table_variable_nothing_is_attempted(monkeypatch):
    monkeypatch.delenv(deploy_marker.ROLLUP_TABLE_ENV, raising=False)
    monkeypatch.setattr(deploy_marker, "APP_BUILD", "abc")
    assert deploy_marker.record_deploy_marker("app-api") is False


def test_a_failure_never_escapes(monkeypatch):
    monkeypatch.setenv(deploy_marker.ROLLUP_TABLE_ENV, TABLE)
    monkeypatch.setattr(deploy_marker, "APP_BUILD", "abc")

    def _boom(name, region_name=None):
        raise RuntimeError("no credentials")

    monkeypatch.setattr("apis.shared.aws_clients.get_dynamodb_table", _boom)
    assert deploy_marker.record_deploy_marker("app-api") is False
