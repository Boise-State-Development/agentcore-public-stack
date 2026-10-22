"""The one-off cleanup of stray ``DOC#`` rows on live agents (scripts/cleanup_stray_doc_rows.py)."""

from __future__ import annotations

import os
import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import cleanup_stray_doc_rows as cleanup  # noqa: E402

REGION = "us-east-1"
TABLE = "test-rag-assistants"
LIVE = "ast-live00000001"
GONE = "ast-gone00000001"


def _row(agent, sk, **fields):
    return {"PK": f"AST#{agent}", "SK": sk, **fields}


class TestFindStrays:
    def test_only_non_document_ids_on_live_agents(self):
        items = [
            _row(LIVE, "METADATA"),
            _row(LIVE, "DOC#DOC-0123456789ab", status="complete", s3Key="assistants/x/documents/DOC-0123456789ab/a.pdf"),
            _row(LIVE, "DOC#icons", status="failed"),
            _row(LIVE, "SHARE#someone"),
            # An orphan is cleanup_orphaned_agent_rows.py's job.
            _row(GONE, "DOC#icons", status="failed"),
            {"PK": "KBWORK#teardown", "SK": "DOC#icons"},
        ]
        strays = cleanup.find_strays(items)

        assert [(s.agent_id, s.row_id) for s in strays] == [(LIVE, "icons")]
        assert strays[0].deletable

    def test_a_stray_with_an_s3_key_is_not_deletable(self):
        items = [_row(LIVE, "METADATA"), _row(LIVE, "DOC#odd", status="complete", s3Key="assistants/x/odd")]
        (stray,) = cleanup.find_strays(items)
        assert not stray.deletable
        assert stray.summary()["action"] == "leave (has s3Key)"


@pytest.fixture()
def table(monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        t = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
        for item in (
            _row(LIVE, "METADATA", name="Live agent"),
            _row(LIVE, "DOC#DOC-0123456789ab", status="complete", s3Key="assistants/x/documents/DOC-0123456789ab/a.pdf"),
            _row(LIVE, "DOC#icons", status="failed", errorMessage="boom"),
            _row(LIVE, "DOC#odd", status="complete", s3Key="assistants/x/odd"),
        ):
            t.put_item(Item=item)
        yield t


def _keys(t):
    return sorted(i["SK"] for i in t.scan()["Items"])


def _run(*extra):
    return cleanup.main(["--project-prefix", "p", "--region", REGION, "--table", TABLE, *extra])


def test_report_only_changes_nothing(table, capsys):
    before = _keys(table)
    assert _run() == 0
    assert _keys(table) == before
    out = capsys.readouterr().out
    assert "REPORT ONLY" in out and "DOC#icons" in out


def test_apply_requires_the_prefix_confirmation(table, capsys):
    assert _run("--apply") == 2
    assert _run("--apply", "--confirm-prefix", "other") == 2
    assert "DOC#icons" in _keys(table)


def test_apply_deletes_only_the_stray_without_an_s3_key(table):
    assert _run("--apply", "--confirm-prefix", "p") == 0
    assert _keys(table) == ["DOC#DOC-0123456789ab", "DOC#odd", "METADATA"]


def test_a_row_that_gained_an_s3_key_since_the_scan_is_kept(table):
    stray = cleanup.Stray(agent_id=LIVE, row_id="icons", status="failed", s3_key=None)
    table.update_item(
        Key={"PK": f"AST#{LIVE}", "SK": "DOC#icons"},
        UpdateExpression="SET s3Key = :k",
        ExpressionAttributeValues={":k": "assistants/x/documents/y/z"},
    )
    assert cleanup.delete_stray(table, stray) is False
    assert "DOC#icons" in _keys(table)
