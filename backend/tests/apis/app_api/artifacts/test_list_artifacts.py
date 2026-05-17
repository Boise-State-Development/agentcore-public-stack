"""Tests for the app-api session artifacts list endpoint.

The endpoint returns *every version* of every artifact in a session via
a two-step query: SessionIndex (HEAD rows only) to discover the
artifacts, then a per-artifact main-table `SK begins_with #V#` query for
all immutable version rows. The SPA renders one card per version,
anchored to the turn that produced it via the per-version
`produced_by_message_index` the writer stamps.
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


def _put_version(
    ddb,
    *,
    artifact: str,
    version: int,
    user_id: str = USER_ID,
    session_id: str = SESSION,
    title: str = "Doc",
    updated_at: str | None = "2026-05-15T10:00:00+00:00",
    created_at: str = "2026-05-15T10:00:00+00:00",
    produced_by: int | None = None,
) -> None:
    """One immutable version row, mirroring the writer. `updated_at` /
    `produced_by` left None models a pre-per-version-linkage row."""
    item = {
        "PK": f"USER#{user_id}",
        "SK": f"ARTIFACT#{artifact}#V#{version:05d}",
        "storage": "s3",
        "content_key": f"{user_id}/{artifact}/v{version}/index.html",
        "content_type": "text/html; charset=utf-8",
        "version": version,
        "artifact_id": artifact,
        "user_id": user_id,
        "session_id": session_id,
        "title": title,
        "created_at": created_at,
    }
    if updated_at is not None:
        item["updated_at"] = updated_at
    if produced_by is not None:
        item["produced_by_message_index"] = produced_by
    ddb.Table(TABLE).put_item(Item=item)


def _put_head(
    ddb,
    *,
    artifact: str,
    head_version: int,
    user_id: str = USER_ID,
    session_id: str = SESSION,
    updated_at: str = "2026-05-15T10:00:00+00:00",
    title: str = "Doc",
) -> None:
    """The HEAD pointer row — carries the SessionIndex GSI keys used for
    step-1 artifact discovery."""
    ddb.Table(TABLE).put_item(
        Item={
            "PK": f"USER#{user_id}",
            "SK": f"ARTIFACT#{artifact}#HEAD",
            "GSI1PK": f"SESSION#{session_id}",
            "GSI1SK": f"ARTIFACT#{updated_at}#{artifact}",
            "storage": "s3",
            "content_key": f"{user_id}/{artifact}/v{head_version}/index.html",
            "content_type": "text/html; charset=utf-8",
            "version": head_version,
            "artifact_id": artifact,
            "user_id": user_id,
            "session_id": session_id,
            "title": title,
            "created_at": "2026-05-15T10:00:00+00:00",
            "updated_at": updated_at,
        }
    )


def _put_artifact(
    ddb,
    *,
    artifact: str,
    versions: list[dict],
    user_id: str = USER_ID,
    session_id: str = SESSION,
) -> None:
    """N immutable version rows plus a HEAD at the latest — exactly what
    the writer leaves after a create + updates sequence."""
    for v in versions:
        _put_version(
            ddb,
            artifact=artifact,
            user_id=user_id,
            session_id=session_id,
            **v,
        )
    last = max(versions, key=lambda v: v["version"])
    _put_head(
        ddb,
        artifact=artifact,
        head_version=last["version"],
        user_id=user_id,
        session_id=session_id,
        updated_at=last.get("updated_at") or "2026-05-15T10:00:00+00:00",
        title=last.get("title", "Doc"),
    )


def test_empty_session_is_empty_list(client) -> None:
    tc, _ = client
    resp = tc.get("/artifacts", params={"session_id": SESSION})
    assert resp.status_code == 200
    assert resp.json() == {"artifacts": []}


def test_returns_every_version_newest_artifact_first(client) -> None:
    tc, ddb = client
    _put_artifact(
        ddb,
        artifact="old",
        versions=[
            {"version": 1, "updated_at": "2026-05-15T10:00:00+00:00", "title": "Old"}
        ],
    )
    _put_artifact(
        ddb,
        artifact="new",
        versions=[
            {"version": 1, "updated_at": "2026-05-15T11:00:00+00:00", "title": "New"},
            {"version": 2, "updated_at": "2026-05-15T11:30:00+00:00", "title": "New"},
            {"version": 3, "updated_at": "2026-05-15T12:00:00+00:00", "title": "New"},
        ],
    )

    arts = tc.get("/artifacts", params={"session_id": SESSION}).json()[
        "artifacts"
    ]
    # Every version of every artifact is present.
    assert {(a["artifact_id"], a["version"]) for a in arts} == {
        ("new", 1),
        ("new", 2),
        ("new", 3),
        ("old", 1),
    }
    # Step-1 discovery is HEAD-newest-first, so all of "new"'s versions
    # come before "old"'s.
    ids = [a["artifact_id"] for a in arts]
    assert set(ids[:3]) == {"new"}
    assert ids[-1] == "old"


def test_per_version_produced_by_index(client) -> None:
    """Each version row carries its own linkage index so the SPA can
    anchor every version's card under the turn that produced it. A row
    without one (pre-linkage) is null → SPA end-of-conversation strip."""
    tc, ddb = client
    _put_artifact(
        ddb,
        artifact="art-1",
        versions=[
            {"version": 1, "updated_at": "2026-05-15T11:00:00+00:00", "produced_by": 3},
            {"version": 2, "updated_at": "2026-05-15T12:00:00+00:00", "produced_by": 7},
            {"version": 3, "updated_at": "2026-05-15T12:30:00+00:00"},
        ],
    )
    arts = tc.get("/artifacts", params={"session_id": SESSION}).json()[
        "artifacts"
    ]
    by_v = {a["version"]: a for a in arts}
    assert by_v[1]["produced_by_message_index"] == 3
    assert by_v[2]["produced_by_message_index"] == 7
    assert by_v[3]["produced_by_message_index"] is None


def test_legacy_version_rows_degrade_gracefully(client) -> None:
    """Version rows written before per-version linkage lack updated_at /
    produced_by_message_index. They must still be returned (empty/null)
    so the SPA shows them in the strip rather than dropping them."""
    tc, ddb = client
    _put_artifact(
        ddb,
        artifact="legacy",
        versions=[
            {"version": 1, "updated_at": None},
            {"version": 2, "updated_at": None},
        ],
    )
    arts = tc.get("/artifacts", params={"session_id": SESSION}).json()[
        "artifacts"
    ]
    assert {a["version"] for a in arts} == {1, 2}
    for a in arts:
        assert a["updated_at"] == ""
        assert a["produced_by_message_index"] is None


def test_created_at_present_on_each_version(client) -> None:
    tc, ddb = client
    _put_artifact(
        ddb,
        artifact="art-1",
        versions=[
            {"version": 1},
            {"version": 2, "updated_at": "2026-05-15T12:00:00+00:00"},
        ],
    )
    arts = tc.get("/artifacts", params={"session_id": SESSION}).json()[
        "artifacts"
    ]
    assert all(
        a["created_at"] == "2026-05-15T10:00:00+00:00" for a in arts
    )


def test_other_users_artifact_is_filtered(client) -> None:
    """Step 1 drops a HEAD owned by another user that happens to share
    the queried session id; step 2 is PK=USER#{caller}, so their version
    rows are never read even if a HEAD leaked."""
    tc, ddb = client
    _put_artifact(ddb, artifact="mine", versions=[{"version": 1}])
    _put_artifact(
        ddb,
        artifact="theirs",
        versions=[{"version": 1}],
        user_id="someone-else",
    )

    arts = tc.get("/artifacts", params={"session_id": SESSION}).json()[
        "artifacts"
    ]
    assert {a["artifact_id"] for a in arts} == {"mine"}


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
