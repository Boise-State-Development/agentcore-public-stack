"""Tests for artifact rename and delete (`PATCH`/`DELETE /artifacts/{id}`).

Three things here are worth more than coverage:

* **Rename must not touch `version` or `updated_at`.** `version` carries
  the writer's optimistic lock and `updated_at` is embedded in the
  HEAD row's GSI sort keys, which only the writer maintains. Both are
  asserted explicitly rather than left to a round-trip of the response
  body, because a regression on either is silent and expensive.
* **Delete must cascade to shares.** A surviving lookup row is a live
  link to an artifact that no longer exists.
* **Ownership is enforced by the partition key**, so the scoping tests
  assert the other user's rows are untouched, not merely that the call
  4-0-4s.
"""

from __future__ import annotations

from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from apis.app_api.artifacts import service as artifact_service
from apis.app_api.artifacts.routes import router as artifacts_router
from apis.app_api.artifacts.service import (
    ArtifactLifecycleService,
    ArtifactQueryError,
    get_artifact_lifecycle_service,
)
from apis.shared.auth import User, get_current_user_from_session

TABLE = "test-user-artifacts"
BUCKET = "test-artifacts-bucket"
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
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET)

        monkeypatch.setenv("DYNAMODB_ARTIFACTS_TABLE_NAME", TABLE)
        monkeypatch.setenv("S3_ARTIFACTS_BUCKET_NAME", BUCKET)

        app = FastAPI()
        app.include_router(artifacts_router)
        app.dependency_overrides[get_current_user_from_session] = (
            lambda: User(
                email="u@x.com", user_id=USER_ID, name="U", roles=[]
            )
        )
        yield (
            TestClient(app),
            boto3.resource("dynamodb", region_name=REGION),
            s3,
        )


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


def _seed_artifact(
    ddb,
    s3,
    *,
    user_id: str = USER_ID,
    artifact: str = "art-1",
    versions: int = 2,
    title: str = "Original title",
    session_id: str = "sess-1",
) -> None:
    """Write `versions` version rows + a HEAD row, with S3 objects."""
    table = ddb.Table(TABLE)
    for version in range(1, versions + 1):
        key = f"{user_id}/{artifact}/v{version}/index.html"
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"<h1>hi</h1>")
        table.put_item(
            Item={
                "PK": f"USER#{user_id}",
                "SK": f"ARTIFACT#{artifact}#V#{version:05d}",
                "storage": "s3",
                "content_key": key,
                "content_type": "text/html; charset=utf-8",
                "version": version,
                "artifact_id": artifact,
                "user_id": user_id,
                "session_id": session_id,
                "title": title,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": f"2026-01-0{version}T00:00:00+00:00",
            }
        )
    head_updated = f"2026-01-0{versions}T00:00:00+00:00"
    table.put_item(
        Item={
            "PK": f"USER#{user_id}",
            "SK": f"ARTIFACT#{artifact}#HEAD",
            "storage": "s3",
            "content_key": f"{user_id}/{artifact}/v{versions}/index.html",
            "content_type": "text/html; charset=utf-8",
            "version": versions,
            "artifact_id": artifact,
            "user_id": user_id,
            "session_id": session_id,
            "title": title,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": head_updated,
            "GSI1PK": f"SESSION#{session_id}",
            "GSI1SK": f"ARTIFACT#{head_updated}#{artifact}",
            "GSI2PK": f"USER#{user_id}",
            "GSI2SK": f"ARTIFACT#{head_updated}#{artifact}",
        }
    )


def _seed_share(
    ddb,
    *,
    owner_id: str = USER_ID,
    artifact: str = "art-1",
    version: int = 1,
    share_id: str = "share-1",
) -> None:
    """Both rows of one share, exactly as `_write_share_rows` writes them."""
    table = ddb.Table(TABLE)
    attrs = {
        "share_id": share_id,
        "artifact_id": artifact,
        "version": version,
        "owner_id": owner_id,
        "owner_email": "u@x.com",
        "access_level": "public",
        "title": "Original title",
        "content_type": "text/html; charset=utf-8",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    table.put_item(
        Item={
            **attrs,
            "PK": f"USER#{owner_id}",
            "SK": f"SHARE#{artifact}#V#{version:05d}#{share_id}",
        }
    )
    table.put_item(Item={**attrs, "PK": f"SHARE#{share_id}", "SK": "META"})


def _row(ddb, pk: str, sk: str) -> dict | None:
    return ddb.Table(TABLE).get_item(Key={"PK": pk, "SK": sk}).get("Item")


def _tags(s3, key: str) -> dict:
    resp = s3.get_object_tagging(Bucket=BUCKET, Key=key)
    return {t["Key"]: t["Value"] for t in resp["TagSet"]}


# ---------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------


def test_rename_updates_head_and_every_version_row(client):
    """The library reads HEAD, the session list reads version rows. If a
    rename only reached HEAD, the same artifact would show two different
    names in two places in the app."""
    api, ddb, s3 = client
    _seed_artifact(ddb, s3, versions=3)

    resp = api.patch("/artifacts/art-1", json={"title": "Renamed"})

    assert resp.status_code == 200
    assert resp.json()["title"] == "Renamed"
    assert _row(ddb, f"USER#{USER_ID}", "ARTIFACT#art-1#HEAD")["title"] == (
        "Renamed"
    )
    for version in (1, 2, 3):
        row = _row(
            ddb, f"USER#{USER_ID}", f"ARTIFACT#art-1#V#{version:05d}"
        )
        assert row["title"] == "Renamed"


def test_rename_does_not_touch_version_or_updated_at(client):
    """`version` is the writer's optimistic-lock attribute and
    `updated_at` is baked into HEAD's GSI sort keys. A rename that moved
    either would race concurrent agent updates, or split the library's
    ordering from the session index's."""
    api, ddb, s3 = client
    _seed_artifact(ddb, s3, versions=2)
    before = _row(ddb, f"USER#{USER_ID}", "ARTIFACT#art-1#HEAD")

    api.patch("/artifacts/art-1", json={"title": "Renamed"})

    after = _row(ddb, f"USER#{USER_ID}", "ARTIFACT#art-1#HEAD")
    assert after["version"] == before["version"]
    assert after["updated_at"] == before["updated_at"]
    assert after["GSI1SK"] == before["GSI1SK"]
    assert after["GSI2SK"] == before["GSI2SK"]
    # ...and the rename is still recorded, just on an attribute nothing
    # sorts on.
    assert after["renamed_at"]


def test_rename_trims_surrounding_whitespace(client):
    api, ddb, s3 = client
    _seed_artifact(ddb, s3, versions=1)

    resp = api.patch("/artifacts/art-1", json={"title": "  Spaced  "})

    assert resp.status_code == 200
    assert resp.json()["title"] == "Spaced"


def test_rename_rejects_a_whitespace_only_title(client):
    """Passes the model's `min_length=1` but is empty once trimmed, so
    the service is the layer that has to catch it."""
    api, ddb, s3 = client
    _seed_artifact(ddb, s3, versions=1)

    resp = api.patch("/artifacts/art-1", json={"title": "   "})

    assert resp.status_code == 400
    assert _row(ddb, f"USER#{USER_ID}", "ARTIFACT#art-1#HEAD")["title"] == (
        "Original title"
    )


def test_rename_rejects_an_empty_title(client):
    api, ddb, s3 = client
    _seed_artifact(ddb, s3, versions=1)

    assert api.patch("/artifacts/art-1", json={"title": ""}).status_code == 422


def test_rename_rejects_an_overlong_title(client):
    api, ddb, s3 = client
    _seed_artifact(ddb, s3, versions=1)

    resp = api.patch(
        "/artifacts/art-1",
        json={"title": "x" * (artifact_service.MAX_ARTIFACT_TITLE_LENGTH + 1)},
    )

    assert resp.status_code == 422


def test_rename_cannot_reach_another_users_artifact(client):
    """The lookup key is built from the session, so someone else's id is
    an indistinguishable 404 — and their row must be untouched."""
    api, ddb, s3 = client
    _seed_artifact(ddb, s3, user_id=OTHER_USER, artifact="art-9")

    resp = api.patch("/artifacts/art-9", json={"title": "Hijacked"})

    assert resp.status_code == 404
    assert _row(ddb, f"USER#{OTHER_USER}", "ARTIFACT#art-9#HEAD")["title"] == (
        "Original title"
    )


def test_rename_of_an_unknown_artifact_is_404(client):
    api, _ddb, _s3 = client
    assert api.patch("/artifacts/nope", json={"title": "x"}).status_code == 404


# ---------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------


def test_delete_removes_head_and_every_version_row(client):
    """Not just the HEAD pointer: prior versions are independently
    addressable (the panel's version picker mints a token per version),
    so leaving them would leave the artifact live and unlistable."""
    api, ddb, s3 = client
    _seed_artifact(ddb, s3, versions=3)

    resp = api.delete("/artifacts/art-1")

    assert resp.status_code == 204
    assert _row(ddb, f"USER#{USER_ID}", "ARTIFACT#art-1#HEAD") is None
    for version in (1, 2, 3):
        assert (
            _row(ddb, f"USER#{USER_ID}", f"ARTIFACT#art-1#V#{version:05d}")
            is None
        )


def test_delete_tags_every_object_for_lifecycle_expiry(client):
    """The bucket's `expire-soft-deleted` rule filters on this exact tag.
    It is the only handle left on the object once the row holding its
    `content_key` is gone."""
    api, ddb, s3 = client
    _seed_artifact(ddb, s3, versions=2)

    api.delete("/artifacts/art-1")

    for version in (1, 2):
        key = f"{USER_ID}/art-1/v{version}/index.html"
        assert _tags(s3, key) == {"lifecycle-class": "deleted"}


def test_delete_revokes_every_share_of_the_artifact(client):
    """Shares are per-version, so the cascade sweeps the whole prefix —
    otherwise a link handed out for v1 outlives what it points at."""
    api, ddb, s3 = client
    _seed_artifact(ddb, s3, versions=2)
    _seed_share(ddb, version=1, share_id="share-1")
    _seed_share(ddb, version=2, share_id="share-2")

    api.delete("/artifacts/art-1")

    for share_id, version in (("share-1", 1), ("share-2", 2)):
        assert _row(ddb, f"SHARE#{share_id}", "META") is None
        assert (
            _row(
                ddb,
                f"USER#{USER_ID}",
                f"SHARE#art-1#V#{version:05d}#{share_id}",
            )
            is None
        )


def test_delete_leaves_shares_of_other_artifacts_alone(client):
    api, ddb, s3 = client
    _seed_artifact(ddb, s3, artifact="art-1", versions=1)
    _seed_artifact(ddb, s3, artifact="art-2", versions=1)
    _seed_share(ddb, artifact="art-2", version=1, share_id="keep-me")

    api.delete("/artifacts/art-1")

    assert _row(ddb, "SHARE#keep-me", "META") is not None
    assert _row(ddb, f"USER#{USER_ID}", "ARTIFACT#art-2#HEAD") is not None


def test_delete_cannot_reach_another_users_artifact(client):
    api, ddb, s3 = client
    _seed_artifact(ddb, s3, user_id=OTHER_USER, artifact="art-9")

    resp = api.delete("/artifacts/art-9")

    assert resp.status_code == 404
    assert _row(ddb, f"USER#{OTHER_USER}", "ARTIFACT#art-9#HEAD") is not None
    assert (
        _row(ddb, f"USER#{OTHER_USER}", "ARTIFACT#art-9#V#00001") is not None
    )


def test_delete_of_an_unknown_artifact_is_404(client):
    api, _ddb, _s3 = client
    assert api.delete("/artifacts/nope").status_code == 404


def test_delete_completes_when_object_tagging_fails(client):
    """A failed tag costs unreclaimed bytes. Aborting over it would cost
    the user a delete that visibly did nothing, which is worse — the
    object is already unreachable once its row is gone."""
    api, ddb, s3 = client
    _seed_artifact(ddb, s3, versions=1)

    failure = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "nope"}},
        "PutObjectTagging",
    )
    with patch.object(
        artifact_service, "_s3"
    ) as fake_s3:
        fake_s3.return_value.put_object_tagging.side_effect = failure
        resp = api.delete("/artifacts/art-1")

    assert resp.status_code == 204
    assert _row(ddb, f"USER#{USER_ID}", "ARTIFACT#art-1#HEAD") is None


def test_delete_stops_before_touching_rows_when_shares_cannot_be_listed(
    client,
):
    """Enumeration runs first precisely so a failure here is a clean
    no-op — the alternative is deleting an artifact while blind to the
    live links pointing at it."""
    api, ddb, s3 = client
    _seed_artifact(ddb, s3, versions=2)

    service = ArtifactLifecycleService()
    with patch.object(
        service._shares,
        "revoke_for_artifact",
        side_effect=ArtifactQueryError("boom"),
    ):
        api.app.dependency_overrides[get_artifact_lifecycle_service] = (
            lambda: service
        )
        resp = api.delete("/artifacts/art-1")
        api.app.dependency_overrides.pop(get_artifact_lifecycle_service)

    assert resp.status_code == 503
    assert _row(ddb, f"USER#{USER_ID}", "ARTIFACT#art-1#HEAD") is not None
    assert _row(ddb, f"USER#{USER_ID}", "ARTIFACT#art-1#V#00001") is not None
