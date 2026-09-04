"""Tests for the app-api user-wide artifact library endpoint.

`GET /artifacts/library` differs from the session list next door in
cardinality and in scoping: one row per *artifact* (not per version),
across *every* session (not one), served by a single base-table Query on
`PK=USER#{uid}` with no index involved.

Ownership here is enforced by the partition key rather than re-checked
per row, which is the substantive difference from `list_for_session` —
that one reads a GSI partitioned by session and so has to filter. The
scoping test below exists to prove the key is actually doing that job.
"""

from __future__ import annotations

import boto3
import pytest
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
OTHER_USER = "user-456"


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    artifact_service._reset_caches_for_tests()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    with mock_aws():
        monkeypatch.setenv("AWS_REGION", REGION)
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
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
        monkeypatch.setenv("DYNAMODB_ARTIFACTS_TABLE_NAME", TABLE)

        app = FastAPI()
        app.include_router(artifacts_router)
        app.dependency_overrides[get_current_user_from_session] = lambda: User(
            email="u@x.com", user_id=USER_ID, name="U", roles=[]
        )
        yield TestClient(app), boto3.resource("dynamodb", region_name=REGION)


def _put_artifact(
    ddb,
    *,
    artifact: str,
    user_id: str = USER_ID,
    session_id: str = "sess-1",
    title: str = "Doc",
    updated_at: str | None = "2026-05-15T10:00:00+00:00",
    content_type: str = "text/markdown",
    versions: int = 1,
) -> None:
    """A HEAD row plus its version rows, mirroring the writer.

    `updated_at=None` models a row written before the attribute existed.
    The version rows matter: the library must not return them, and the
    Query pays to read them, so a fixture without them would not
    exercise the filter at all.
    """
    table = ddb.Table(TABLE)
    common = {
        "storage": "s3",
        "content_type": content_type,
        "artifact_id": artifact,
        "user_id": user_id,
        "session_id": session_id,
        "title": title,
        "created_at": "2026-05-01T09:00:00+00:00",
    }
    for version in range(1, versions + 1):
        table.put_item(
            Item={
                **common,
                "PK": f"USER#{user_id}",
                "SK": f"ARTIFACT#{artifact}#V#{version:05d}",
                "version": version,
                "content_key": f"{user_id}/{artifact}/v{version}/index.html",
                "updated_at": "2026-05-01T09:00:00+00:00",
            }
        )
    head = {
        **common,
        "PK": f"USER#{user_id}",
        "SK": f"ARTIFACT#{artifact}#HEAD",
        "version": versions,
        "content_key": f"{user_id}/{artifact}/v{versions}/index.html",
        "GSI1PK": f"SESSION#{session_id}",
    }
    if updated_at is not None:
        head["updated_at"] = updated_at
        head["GSI1SK"] = f"ARTIFACT#{updated_at}#{artifact}"
        head["GSI2PK"] = f"USER#{user_id}"
        head["GSI2SK"] = f"ARTIFACT#{updated_at}#{artifact}"
    table.put_item(Item=head)


def test_returns_one_row_per_artifact_newest_first(client) -> None:
    c, ddb = client
    _put_artifact(ddb, artifact="a1", updated_at="2026-05-10T10:00:00+00:00")
    _put_artifact(
        ddb, artifact="a2", updated_at="2026-06-01T10:00:00+00:00", versions=4
    )
    _put_artifact(ddb, artifact="a3", updated_at="2026-05-20T10:00:00+00:00")

    res = c.get("/artifacts/library")
    assert res.status_code == 200
    rows = res.json()["artifacts"]

    # One per artifact — a2 has four versions and still appears once.
    assert [r["artifact_id"] for r in rows] == ["a2", "a3", "a1"]
    assert rows[0]["version"] == 4


def test_spans_every_session(client) -> None:
    """The point of the library: artifacts outlive the chat that made
    them, and the user's whole history is one partition."""
    c, ddb = client
    _put_artifact(
        ddb, artifact="a1", session_id="sess-1",
        updated_at="2026-05-10T10:00:00+00:00",
    )
    _put_artifact(
        ddb, artifact="a2", session_id="sess-2",
        updated_at="2026-05-11T10:00:00+00:00",
    )

    rows = c.get("/artifacts/library").json()["artifacts"]
    assert {r["session_id"] for r in rows} == {"sess-1", "sess-2"}


def test_scopes_to_the_authenticated_user(client) -> None:
    """Ownership rides the partition key. There is no request parameter
    that could widen this, which is why the endpoint takes none."""
    c, ddb = client
    _put_artifact(ddb, artifact="mine")
    _put_artifact(ddb, artifact="theirs", user_id=OTHER_USER)

    rows = c.get("/artifacts/library").json()["artifacts"]
    assert [r["artifact_id"] for r in rows] == ["mine"]


def test_undated_legacy_rows_are_returned_and_sort_last(client) -> None:
    """A row predating `updated_at` must still be listed — dropping a
    user's oldest artifacts would be worse than showing them undated —
    but an empty sort key must not float it to the top."""
    c, ddb = client
    _put_artifact(ddb, artifact="old", updated_at=None)
    _put_artifact(ddb, artifact="new", updated_at="2026-05-10T10:00:00+00:00")

    rows = c.get("/artifacts/library").json()["artifacts"]
    assert [r["artifact_id"] for r in rows] == ["new", "old"]
    assert rows[1]["updated_at"] == ""


def test_carries_the_fields_the_library_renders(client) -> None:
    c, ddb = client
    _put_artifact(
        ddb, artifact="a1", title="Budget model",
        content_type="text/csv", session_id="sess-7",
    )

    row = c.get("/artifacts/library").json()["artifacts"][0]
    assert row == {
        "artifact_id": "a1",
        "version": 1,
        "title": "Budget model",
        "content_type": "text/csv",
        "created_at": "2026-05-01T09:00:00+00:00",
        "updated_at": "2026-05-15T10:00:00+00:00",
        "session_id": "sess-7",
    }


def test_empty_library_is_an_empty_list(client) -> None:
    c, _ = client
    res = c.get("/artifacts/library")
    assert res.status_code == 200
    assert res.json()["artifacts"] == []


def test_query_failure_is_retryable_503(client) -> None:
    c, _ = client

    class Failing(ArtifactListService):
        def list_for_user(self, *, user_id: str):
            raise ArtifactQueryError("boom")

    c.app.dependency_overrides[get_artifact_list_service] = Failing
    assert c.get("/artifacts/library").status_code == 503


def test_misconfiguration_is_500(client) -> None:
    c, _ = client

    class Misconfigured(ArtifactListService):
        def list_for_user(self, *, user_id: str):
            raise RenderTokenConfigError("no table")

    c.app.dependency_overrides[get_artifact_list_service] = Misconfigured
    assert c.get("/artifacts/library").status_code == 500


def test_library_route_is_not_shadowed_by_the_artifact_id_route(client) -> None:
    """`/artifacts/library` sits in the same space as
    `/artifacts/{artifact_id}/content`. The literal must win rather than
    be read as an artifact id."""
    c, ddb = client
    _put_artifact(ddb, artifact="a1")

    res = c.get("/artifacts/library")
    assert res.status_code == 200
    assert "artifacts" in res.json()


def test_paginates_a_partition_larger_than_one_page(client) -> None:
    """The Query loop must drain `LastEvaluatedKey`. Asserted with a
    stub rather than 1MB of fixture rows, since moto pages on real byte
    size and a realistic partition is far under the limit."""
    calls: list[dict] = []

    class Paged:
        def query(self, **kwargs):
            calls.append(kwargs)
            if "ExclusiveStartKey" not in kwargs:
                return {
                    "Items": [
                        {
                            "PK": f"USER#{USER_ID}",
                            "SK": "ARTIFACT#a1#HEAD",
                            "artifact_id": "a1",
                            "version": 1,
                            "title": "One",
                            "content_type": "text/markdown",
                            "created_at": "2026-05-01T09:00:00+00:00",
                            "updated_at": "2026-05-01T09:00:00+00:00",
                            "session_id": "s1",
                        }
                    ],
                    "LastEvaluatedKey": {"PK": "x", "SK": "y"},
                }
            return {
                "Items": [
                    {
                        "PK": f"USER#{USER_ID}",
                        "SK": "ARTIFACT#a2#HEAD",
                        "artifact_id": "a2",
                        "version": 1,
                        "title": "Two",
                        "content_type": "text/markdown",
                        "created_at": "2026-05-02T09:00:00+00:00",
                        "updated_at": "2026-05-02T09:00:00+00:00",
                        "session_id": "s2",
                    }
                ]
            }

    artifact_service._ddb_table = Paged()
    rows = ArtifactListService().list_for_user(user_id=USER_ID)

    assert len(calls) == 2
    assert [r["artifact_id"] for r in rows] == ["a2", "a1"]
