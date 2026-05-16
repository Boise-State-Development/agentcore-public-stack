"""Tests for the artifact authoring tools.

The headline guarantee is `test_record_satisfies_minter`: a row written
by this tool must be accepted byte-for-byte by #310's app-api minter
(the real downstream reader) and resolve to the S3 object #309's render
Lambda would serve.
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from agents.builtin_tools.artifacts import service
from apis.inference_api.chat.routes import _build_artifact_tools

REGION = "us-east-1"
TABLE = "test-user-artifacts"
BUCKET = "test-artifacts-content"
USER = "user-123"
SESSION = "sess-9"
DOC = "<!doctype html><html><body><h1>hi</h1></body></html>"


@pytest.fixture(autouse=True)
def _reset() -> None:
    service._reset_caches_for_tests()


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch):
    with mock_aws():
        monkeypatch.setenv("AWS_REGION", REGION)
        monkeypatch.setenv("S3_ARTIFACTS_BUCKET_NAME", BUCKET)
        monkeypatch.setenv("DYNAMODB_ARTIFACTS_TABLE_NAME", TABLE)

        boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "SessionIndex",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION), boto3.client(
            "s3", region_name=REGION
        )


def _item(ddb, artifact_id: str, sk_suffix: str) -> dict:
    return ddb.Table(TABLE).get_item(
        Key={"PK": f"USER#{USER}", "SK": f"ARTIFACT#{artifact_id}#{sk_suffix}"}
    ).get("Item")


def test_create_writes_s3_and_rows(aws) -> None:
    ddb, s3 = aws
    aid, ver = service.create_artifact_record(USER, SESSION, "My Art", DOC, "")
    assert ver == 1

    key = f"{USER}/{aid}/v1/index.html"
    assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode() == DOC

    vrow = _item(ddb, aid, "V#00001")
    assert vrow["storage"] == "s3"
    assert vrow["content_key"] == key
    assert vrow["content_type"] == "text/html; charset=utf-8"

    head = _item(ddb, aid, "HEAD")
    assert head["version"] == 1
    assert head["GSI1PK"] == f"SESSION#{SESSION}"
    assert head["GSI1SK"].startswith("ARTIFACT#") and head["GSI1SK"].endswith(aid)


def test_update_increments_and_preserves_old(aws) -> None:
    ddb, s3 = aws
    aid, _ = service.create_artifact_record(USER, SESSION, "T", DOC, "")
    new_doc = "<!doctype html><html><body>v2</body></html>"
    ver = service.update_artifact_record(USER, aid, new_doc, None, None)
    assert ver == 2

    # Old version object is immutable / still present.
    assert s3.get_object(
        Bucket=BUCKET, Key=f"{USER}/{aid}/v1/index.html"
    )["Body"].read().decode() == DOC
    assert s3.get_object(
        Bucket=BUCKET, Key=f"{USER}/{aid}/v2/index.html"
    )["Body"].read().decode() == new_doc

    assert _item(ddb, aid, "V#00002")["content_key"] == f"{USER}/{aid}/v2/index.html"
    head = _item(ddb, aid, "HEAD")
    assert head["version"] == 2
    assert head["title"] == "T"  # carried forward


def test_update_unknown_artifact_raises(aws) -> None:
    with pytest.raises(service.ArtifactNotFoundError):
        service.update_artifact_record(USER, "nope", DOC, None, None)


def test_update_foreign_artifact_raises(aws) -> None:
    aid, _ = service.create_artifact_record(USER, SESSION, "T", DOC, "")
    with pytest.raises(service.ArtifactNotFoundError):
        service.update_artifact_record("someone-else", aid, DOC, None, None)


def test_content_type_default(aws) -> None:
    ddb, _ = aws
    aid, _ = service.create_artifact_record(USER, SESSION, "T", DOC, "")
    assert _item(ddb, aid, "V#00001")["content_type"] == "text/html; charset=utf-8"


def test_ssm_fallback(aws, monkeypatch: pytest.MonkeyPatch) -> None:
    """Env unset → resolve bucket/table from /{PROJECT_PREFIX}/artifacts/*."""
    monkeypatch.delenv("S3_ARTIFACTS_BUCKET_NAME", raising=False)
    monkeypatch.delenv("DYNAMODB_ARTIFACTS_TABLE_NAME", raising=False)
    monkeypatch.setenv("PROJECT_PREFIX", "myproj")
    ssm = boto3.client("ssm", region_name=REGION)
    ssm.put_parameter(Name="/myproj/artifacts/bucket-name", Value=BUCKET, Type="String")
    ssm.put_parameter(Name="/myproj/artifacts/table-name", Value=TABLE, Type="String")
    service._reset_caches_for_tests()

    aid, ver = service.create_artifact_record(USER, SESSION, "T", DOC, "")
    assert ver == 1 and aid


def test_record_satisfies_minter(aws) -> None:
    """Cross-PR contract: the written version row must be accepted by
    #310's app-api minter and resolve to the S3 object #309 serves."""
    _, s3 = aws
    aid, ver = service.create_artifact_record(USER, SESSION, "T", DOC, "")

    from apis.app_api.artifacts import service as minter

    minter._reset_caches_for_tests()
    # Minter reads its own table handle from the same env we set.
    minter._assert_version_exists(USER, aid, ver)  # must not raise

    # And the content_key the readers trust actually points at content.
    vrow = _item(boto3.resource("dynamodb", region_name=REGION), aid, "V#00001")
    assert s3.get_object(
        Bucket=BUCKET, Key=vrow["content_key"]
    )["Body"].read().decode() == DOC


@pytest.mark.parametrize(
    "enabled,expected",
    [(None, 0), ([], 0), (["other"], 0), (["create_artifact"], 1),
     (["create_artifact", "update_artifact"], 2)],
)
def test_routes_gating(enabled, expected) -> None:
    tools = _build_artifact_tools(enabled, SESSION, USER)
    assert len(tools) == expected


def test_list_session_artifacts_returns_heads_newest_first(aws) -> None:
    a1, _ = service.create_artifact_record(USER, SESSION, "First", DOC, "")
    a2, _ = service.create_artifact_record(USER, SESSION, "Second", DOC, "")
    # Bump a1 to v2 so it becomes the most-recently-updated HEAD.
    service.update_artifact_record(USER, a1, DOC, None, None)

    rows = service.list_session_artifacts(USER, SESSION)
    by_id = {r["artifact_id"]: r for r in rows}
    assert set(by_id) == {a1, a2}
    assert by_id[a1]["version"] == 2  # reflects current HEAD, not v1
    assert by_id[a2]["title"] == "Second"
    # Newest-first: a1 (just updated) precedes a2.
    assert [r["artifact_id"] for r in rows] == [a1, a2]


def test_list_session_artifacts_scopes_to_user(aws) -> None:
    mine, _ = service.create_artifact_record(USER, SESSION, "Mine", DOC, "")
    service.create_artifact_record("someone-else", SESSION, "Theirs", DOC, "")

    rows = service.list_session_artifacts(USER, SESSION)
    assert [r["artifact_id"] for r in rows] == [mine]


def test_list_session_artifacts_empty_session(aws) -> None:
    assert service.list_session_artifacts(USER, "no-such-session") == []


def test_set_produced_by_message_index_stamps_head_and_lists(aws) -> None:
    aid, _ = service.create_artifact_record(USER, SESSION, "Doc", DOC, "")
    assert service.list_session_artifacts(USER, SESSION)[0][
        "produced_by_message_index"
    ] is None

    service.set_produced_by_message_index(USER, aid, 7)

    rows = service.list_session_artifacts(USER, SESSION)
    assert rows[0]["produced_by_message_index"] == 7
    # The stamp must leave the optimistic-lock `version` untouched so a
    # later update_artifact still re-points HEAD cleanly.
    assert service.update_artifact_record(USER, aid, DOC, None, None) == 2


def test_set_produced_by_message_index_requires_existing_head(aws) -> None:
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError):
        service.set_produced_by_message_index(USER, "nope", 1)
