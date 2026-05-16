"""Tests for the app-api session artifacts list endpoint.

Mirrors the writer's frozen HEAD-row shape from #311
(backend/src/agents/builtin_tools/artifacts/service.py): only HEAD rows
carry GSI1PK/GSI1SK, so a SessionIndex query returns exactly one row per
artifact (its current version) newest-first.
"""

from __future__ import annotations

import boto3
import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from apis.app_api.artifacts import service as artifact_service
from apis.app_api.artifacts.routes import router as artifacts_router
from apis.app_api.artifacts.service import (
    ArtifactListService,
    ArtifactQueryError,
    RenderTokenConfigError,
    get_artifact_list_service,
)
from apis.shared.auth import User, get_current_user_from_session

TABLE = "test-user-artifacts"
REGION = "us-east-1"
USER_ID = "user-123"
SESSION = "sess-9"


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    artifact_service._reset_caches_for_tests()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    with mock_aws():
        monkeypatch.setenv("AWS_REGION", REGION)
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
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
            BillingMode="PAY_PER_REQUEST",
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
        )

        monkeypatch.setenv("DYNAMODB_ARTIFACTS_TABLE_NAME", TABLE)

        app = FastAPI()
        app.include_router(artifacts_router)
        app.dependency_overrides[get_current_user_from_session] = lambda: User(
            email="u@x.com", user_id=USER_ID, name="U", roles=[]
        )
        yield TestClient(app), boto3.resource("dynamodb", region_name=REGION)


def _put_head(
    ddb,
    *,
    user_id: str = USER_ID,
    session_id: str = SESSION,
    artifact: str = "art-1",
    version: int = 1,
    title: str = "Doc",
    updated_at: str = "2026-05-15T10:00:00+00:00",
    created_at: str = "2026-05-15T10:00:00+00:00",
) -> None:
    ddb.Table(TABLE).put_item(
        Item={
            "PK": f"USER#{user_id}",
            "SK": f"ARTIFACT#{artifact}#HEAD",
            "GSI1PK": f"SESSION#{session_id}",
            "GSI1SK": f"ARTIFACT#{updated_at}#{artifact}",
            "storage": "s3",
            "content_key": f"{user_id}/{artifact}/v{version}/index.html",
            "content_type": "text/html; charset=utf-8",
            "version": version,
            "artifact_id": artifact,
            "user_id": user_id,
            "session_id": session_id,
            "title": title,
            "created_at": created_at,
            "updated_at": updated_at,
        }
    )


def _put_version_row(ddb, *, artifact: str = "art-1", version: int = 1) -> None:
    """A non-HEAD version row — must NOT appear in the SessionIndex query
    (the writer omits GSI1PK/GSI1SK on version rows)."""
    ddb.Table(TABLE).put_item(
        Item={
            "PK": f"USER#{USER_ID}",
            "SK": f"ARTIFACT#{artifact}#V#{version:05d}",
            "storage": "s3",
            "content_key": f"{USER_ID}/{artifact}/v{version}/index.html",
            "content_type": "text/html; charset=utf-8",
            "version": version,
            "artifact_id": artifact,
            "user_id": USER_ID,
            "session_id": SESSION,
            "title": "Doc",
            "created_at": "2026-05-15T10:00:00+00:00",
        }
    )


def test_empty_session_is_empty_list(client) -> None:
    tc, _ = client
    resp = tc.get("/artifacts", params={"session_id": SESSION})
    assert resp.status_code == 200
    assert resp.json() == {"artifacts": []}


def test_lists_head_rows_newest_first(client) -> None:
    tc, ddb = client
    _put_head(
        ddb, artifact="old", updated_at="2026-05-15T10:00:00+00:00", title="Old"
    )
    _put_head(
        ddb, artifact="new", updated_at="2026-05-15T12:00:00+00:00", title="New"
    )
    # A version row for the same artifact must be ignored by the query.
    _put_version_row(ddb, artifact="new", version=1)

    resp = tc.get("/artifacts", params={"session_id": SESSION})
    assert resp.status_code == 200
    arts = resp.json()["artifacts"]
    assert [a["artifact_id"] for a in arts] == ["new", "old"]
    assert arts[0]["title"] == "New"
    assert arts[0]["content_type"] == "text/html; charset=utf-8"


def test_produced_by_message_index_round_trips(client) -> None:
    """The HEAD row's linkage index (stamped post-turn by the stream
    coordinator) must surface on the list DTO so the SPA can anchor the
    card inline; absent on legacy rows → null (SPA falls back to strip)."""
    tc, ddb = client
    _put_head(ddb, artifact="linked", updated_at="2026-05-15T12:00:00+00:00")
    ddb.Table(TABLE).update_item(
        Key={"PK": f"USER#{USER_ID}", "SK": "ARTIFACT#linked#HEAD"},
        UpdateExpression="SET produced_by_message_index = :i",
        ExpressionAttributeValues={":i": 5},
    )
    _put_head(ddb, artifact="legacy", updated_at="2026-05-15T11:00:00+00:00")

    arts = tc.get("/artifacts", params={"session_id": SESSION}).json()[
        "artifacts"
    ]
    by_id = {a["artifact_id"]: a for a in arts}
    assert by_id["linked"]["produced_by_message_index"] == 5
    assert by_id["legacy"]["produced_by_message_index"] is None


def test_reflects_current_version(client) -> None:
    tc, ddb = client
    _put_head(ddb, artifact="art-1", version=3, title="V3")
    resp = tc.get("/artifacts", params={"session_id": SESSION})
    body = resp.json()["artifacts"]
    assert body[0]["version"] == 3
    assert body[0]["created_at"] == "2026-05-15T10:00:00+00:00"


def test_other_users_session_row_is_filtered(client) -> None:
    """GSI1PK is SESSION#-scoped, not user-scoped: a row owned by another
    user that happens to share the queried session id must be dropped."""
    tc, ddb = client
    _put_head(ddb, artifact="mine", user_id=USER_ID)
    _put_head(ddb, artifact="theirs", user_id="someone-else")

    resp = tc.get("/artifacts", params={"session_id": SESSION})
    arts = resp.json()["artifacts"]
    assert [a["artifact_id"] for a in arts] == ["mine"]


def test_session_id_required(client) -> None:
    tc, _ = client
    resp = tc.get("/artifacts")
    assert resp.status_code == 422


def test_requires_authentication() -> None:
    app = FastAPI()
    app.include_router(artifacts_router)
    resp = TestClient(app).get("/artifacts", params={"session_id": SESSION})
    assert resp.status_code == 401


def test_transient_query_error_is_not_a_config_error(
    client, monkeypatch
) -> None:
    """A transient DynamoDB ClientError is a runtime query failure, not a
    misconfiguration — it must surface as ArtifactQueryError so the route
    can distinguish a configured-but-throttled feature from a broken one."""

    class _ThrottlingTable:
        def query(self, **_):
            raise ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                "Query",
            )

    monkeypatch.setattr(artifact_service, "_table", lambda: _ThrottlingTable())
    with pytest.raises(ArtifactQueryError):
        ArtifactListService().list_for_session(
            user_id=USER_ID, session_id=SESSION
        )


def test_route_maps_transient_query_failure_to_503(client) -> None:
    """ArtifactQueryError → 503 (retryable), distinct from the 500 a real
    RenderTokenConfigError misconfiguration produces."""
    tc, _ = client

    class _FailingService:
        def list_for_session(self, **_):
            raise ArtifactQueryError("artifact list query failed")

    tc.app.dependency_overrides[get_artifact_list_service] = _FailingService
    try:
        resp = tc.get("/artifacts", params={"session_id": SESSION})
    finally:
        tc.app.dependency_overrides.pop(get_artifact_list_service, None)
    assert resp.status_code == 503


def test_route_maps_misconfig_to_500(client) -> None:
    tc, _ = client

    class _MisconfiguredService:
        def list_for_session(self, **_):
            raise RenderTokenConfigError("DYNAMODB_ARTIFACTS_TABLE_NAME is not set")

    tc.app.dependency_overrides[get_artifact_list_service] = _MisconfiguredService
    try:
        resp = tc.get("/artifacts", params={"session_id": SESSION})
    finally:
        tc.app.dependency_overrides.pop(get_artifact_list_service, None)
    assert resp.status_code == 500
