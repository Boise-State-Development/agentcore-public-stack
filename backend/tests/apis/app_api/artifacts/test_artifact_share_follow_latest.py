"""Shares that follow the latest version (`followsLatest`).

A share pins one immutable version unless its owner opts in to a link
that always shows the newest one. These tests hold four things:

- a following share resolves the owner's `#HEAD` on every recipient
  read (metadata, render token, content), and a pinned one never does;
- the ACL check still runs first, so following never widens who can
  read, only what version they see;
- a project share can never follow (its Outputs record one version);
- a following share whose artifact is gone 404s rather than minting.
"""

from __future__ import annotations

import boto3
import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from apis.app_api.artifacts import service as token_service
from apis.app_api.artifacts.service import get_artifact_content_service
from apis.app_api.artifacts.shares import (
    artifact_shares_router,
    shared_artifacts_router,
)
from apis.shared.auth import User, get_current_user_from_session

KEY = "test-render-key-44-chars-of-entropy-aaaaaaaa"
SECRET_NAME = "test-artifact-render-token-key"
TABLE = "test-user-artifacts"
ORIGIN = "https://artifacts.test.example.com"
REGION = "us-east-1"

OWNER = User(email="owner@x.com", user_id="owner-1", name="owner", roles=[])
VIEWER = User(email="viewer@x.com", user_id="viewer-1", name="viewer", roles=[])
OUTSIDER = User(email="out@x.com", user_id="out-1", name="out", roles=[])


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    token_service._reset_caches_for_tests()


class _RecordingContent:
    """Stands in for ArtifactContentService and records what it was asked."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def get(self, *, owner_id: str, artifact_id: str, version: int):
        self.calls.append(
            {"owner_id": owner_id, "artifact_id": artifact_id, "version": version}
        )
        return f"<p>v{version}</p>", "text/html; charset=utf-8"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    with mock_aws():
        monkeypatch.setenv("AWS_REGION", REGION)
        sm = boto3.client("secretsmanager", region_name=REGION)
        arn = sm.create_secret(Name=SECRET_NAME, SecretString=KEY)["ARN"]
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
        monkeypatch.setenv("ARTIFACTS_RENDER_TOKEN_SECRET_ARN", arn)
        monkeypatch.setenv("DYNAMODB_ARTIFACTS_TABLE_NAME", TABLE)
        monkeypatch.setenv("ARTIFACTS_ORIGIN", ORIGIN)
        content = _RecordingContent()

        def make_client(user: User) -> TestClient:
            app = FastAPI()
            app.include_router(artifact_shares_router)
            app.include_router(shared_artifacts_router)
            app.dependency_overrides[get_current_user_from_session] = lambda: user
            app.dependency_overrides[get_artifact_content_service] = lambda: content
            return TestClient(app)

        yield make_client, boto3.resource("dynamodb", region_name=REGION).Table(TABLE), content


def _write_version(table, version: int, title: str = "Report") -> None:
    """Append a version and re-point HEAD, in the writer's order."""
    base = {
        "artifact_id": "art-1",
        "user_id": OWNER.user_id,
        "version": version,
        "storage": "s3",
        "content_key": f"{OWNER.user_id}/art-1/v{version}/index.html",
        "content_type": "text/html; charset=utf-8",
        "title": title,
        "session_id": "sess-1",
    }
    pk = f"USER#{OWNER.user_id}"
    table.put_item(Item={"PK": pk, "SK": f"ARTIFACT#art-1#V#{version:05d}", **base})
    table.put_item(Item={"PK": pk, "SK": "ARTIFACT#art-1#HEAD", **base})


def _share(make_client, **body) -> dict:
    payload = {"version": 1, "accessLevel": "public", **body}
    resp = make_client(OWNER).post("/artifacts/art-1/shares", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _minted_version(make_client, user: User, share_id: str) -> int:
    resp = make_client(user).post(f"/shared-artifacts/{share_id}/render-token")
    assert resp.status_code == 200, resp.text
    token = resp.json()["url"].split("?t=", 1)[1]
    return jwt.decode(token, KEY, algorithms=["HS256"], audience="artifact-render")["ver"]


def test_a_share_is_pinned_unless_the_owner_opts_in(env) -> None:
    make_client, table, _ = env
    _write_version(table, 1)
    share = _share(make_client)
    assert share["followsLatest"] is False

    _write_version(table, 2, title="Report v2")
    meta = make_client(VIEWER).get(f"/shared-artifacts/{share['shareId']}").json()
    assert meta["version"] == 1
    assert meta["followsLatest"] is False
    assert meta["title"] == "Report"
    assert _minted_version(make_client, VIEWER, share["shareId"]) == 1


def test_a_following_share_shows_the_newest_version(env) -> None:
    make_client, table, content = env
    _write_version(table, 1)
    share = _share(make_client, followsLatest=True)
    assert share["followsLatest"] is True
    assert share["version"] == 1

    _write_version(table, 2, title="Report v2")
    _write_version(table, 3, title="Report v3")

    meta = make_client(VIEWER).get(f"/shared-artifacts/{share['shareId']}").json()
    assert meta["version"] == 3
    assert meta["followsLatest"] is True
    assert meta["title"] == "Report v3"
    assert _minted_version(make_client, VIEWER, share["shareId"]) == 3

    body = make_client(VIEWER).get(f"/shared-artifacts/{share['shareId']}/content").json()
    assert body["version"] == 3
    assert content.calls[-1] == {
        "owner_id": OWNER.user_id,
        "artifact_id": "art-1",
        "version": 3,
    }


def test_the_owner_row_keeps_the_version_the_link_was_made_at(env) -> None:
    make_client, table, _ = env
    _write_version(table, 1)
    share = _share(make_client, followsLatest=True)
    _write_version(table, 2)

    listed = make_client(OWNER).get("/artifacts/art-1/shares").json()["shares"]
    assert [(s["shareId"], s["version"], s["followsLatest"]) for s in listed] == [
        (share["shareId"], 1, True)
    ]


def test_following_never_widens_who_can_read(env) -> None:
    make_client, table, _ = env
    _write_version(table, 1)
    share = _share(
        make_client,
        accessLevel="specific",
        allowedEmails=[VIEWER.email],
        followsLatest=True,
    )
    _write_version(table, 2)

    sid = share["shareId"]
    assert make_client(OUTSIDER).get(f"/shared-artifacts/{sid}").status_code == 403
    assert make_client(OUTSIDER).post(f"/shared-artifacts/{sid}/render-token").status_code == 403
    assert make_client(OUTSIDER).get(f"/shared-artifacts/{sid}/content").status_code == 403
    assert _minted_version(make_client, VIEWER, sid) == 2


def test_the_head_is_read_only_after_the_acl_admits_the_viewer(env, monkeypatch) -> None:
    make_client, table, _ = env
    _write_version(table, 1)
    share = _share(
        make_client, accessLevel="specific", allowedEmails=[VIEWER.email], followsLatest=True
    )
    reads: list = []
    real = token_service._resolve_share_target
    monkeypatch.setattr(
        token_service,
        "_resolve_share_target",
        lambda s: reads.append(s["share_id"]) or real(s),
    )
    make_client(OUTSIDER).post(f"/shared-artifacts/{share['shareId']}/render-token")
    make_client(OUTSIDER).get(f"/shared-artifacts/{share['shareId']}")
    assert reads == []


def test_a_following_share_of_a_deleted_artifact_is_404(env) -> None:
    make_client, table, _ = env
    _write_version(table, 1)
    share = _share(make_client, followsLatest=True)
    table.delete_item(Key={"PK": f"USER#{OWNER.user_id}", "SK": "ARTIFACT#art-1#HEAD"})

    sid = share["shareId"]
    assert make_client(VIEWER).get(f"/shared-artifacts/{sid}").status_code == 404
    assert make_client(VIEWER).post(f"/shared-artifacts/{sid}/render-token").status_code == 404
    assert make_client(VIEWER).get(f"/shared-artifacts/{sid}/content").status_code == 404


def test_update_turns_following_on_and_off(env) -> None:
    make_client, table, _ = env
    _write_version(table, 1)
    share = _share(make_client)
    _write_version(table, 2)
    sid = share["shareId"]
    owner = make_client(OWNER)

    on = owner.patch(f"/artifacts/shares/{sid}", json={"followsLatest": True})
    assert on.status_code == 200, on.text
    assert on.json()["followsLatest"] is True
    assert _minted_version(make_client, VIEWER, sid) == 2

    # Changing who can view leaves following as it was.
    kept = owner.patch(
        f"/artifacts/shares/{sid}",
        json={"accessLevel": "specific", "allowedEmails": [VIEWER.email]},
    )
    assert kept.json()["followsLatest"] is True

    off = owner.patch(f"/artifacts/shares/{sid}", json={"followsLatest": False})
    assert off.json()["followsLatest"] is False
    assert _minted_version(make_client, VIEWER, sid) == 1


def test_only_the_owner_can_change_following(env) -> None:
    make_client, table, _ = env
    _write_version(table, 1)
    sid = _share(make_client)["shareId"]
    resp = make_client(VIEWER).patch(f"/artifacts/shares/{sid}", json={"followsLatest": True})
    assert resp.status_code == 403


def test_a_project_share_cannot_follow(env) -> None:
    make_client, table, _ = env
    _write_version(table, 1)
    resp = make_client(OWNER).post(
        "/artifacts/art-1/shares",
        json={"version": 1, "accessLevel": "project", "followsLatest": True},
    )
    assert resp.status_code == 422


def test_a_following_share_cannot_become_a_project_share(env) -> None:
    """The service refuses it even when the request names no flag."""
    _, table, _ = env
    _write_version(table, 1)
    svc = token_service.ArtifactShareService()
    share = svc.create(
        owner=OWNER, artifact_id="art-1", version=1, access_level="public",
        allowed_emails=None, follows_latest=True,
    )
    with pytest.raises(token_service.ShareCannotFollowError):
        svc.update(
            share_id=share["share_id"], owner=OWNER, access_level="project",
            allowed_emails=None, project_id="proj-1",
        )
    with pytest.raises(token_service.ShareCannotFollowError):
        svc.create(
            owner=OWNER, artifact_id="art-1", version=1, access_level="project",
            allowed_emails=None, project_id="proj-1", follows_latest=True,
        )


def test_the_inbox_says_which_shares_follow(env) -> None:
    make_client, table, _ = env
    _write_version(table, 1)
    _share(make_client, accessLevel="specific", allowedEmails=[VIEWER.email], followsLatest=True)
    resp = make_client(VIEWER).get("/shared-artifacts")
    assert resp.status_code == 200, resp.text
    rows = resp.json()["artifacts"]
    assert [r["followsLatest"] for r in rows] == [True]
