"""Tests for artifact-share CRUD (owner side).

Covers the two-row transactional write, partition-scoped listing, owner
enforcement on mutation, and revocation. The security-critical mint path
lives in `test_shared_render_token.py`.
"""

from __future__ import annotations

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from apis.app_api.artifacts import service as token_service
from apis.app_api.artifacts.shares import (
    artifact_shares_router,
    shared_artifacts_router,
)
from apis.shared.auth import User, get_current_user_from_session

TABLE = "test-user-artifacts"
REGION = "us-east-1"
OWNER_ID = "owner-1"
OWNER_EMAIL = "owner@x.com"


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    token_service._reset_caches_for_tests()


def _owner() -> User:
    return User(
        email=OWNER_EMAIL, user_id=OWNER_ID, name="Owner", roles=[]
    )


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    """A mocked artifacts table plus an app mounting both share routers.

    Yields (make_client, ddb) so a test can re-bind the authenticated
    user without rebuilding the table.
    """
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
            # The session-delete cascade walks SessionIndex to find the
            # session's artifacts, so the fixture mirrors the real table.
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
        monkeypatch.setenv("ARTIFACTS_ORIGIN", "https://a.test.example.com")

        def make_client(user: User | None = None) -> TestClient:
            app = FastAPI()
            app.include_router(artifact_shares_router)
            app.include_router(shared_artifacts_router)
            app.dependency_overrides[get_current_user_from_session] = (
                lambda: user or _owner()
            )
            return TestClient(app)

        yield make_client, boto3.resource("dynamodb", region_name=REGION)


def _put_version(
    ddb,
    *,
    user_id: str = OWNER_ID,
    artifact: str = "art-1",
    version: int = 1,
    title: str = "My Chart",
    session_id: str = "sess-1",
) -> None:
    ddb.Table(TABLE).put_item(
        Item={
            "PK": f"USER#{user_id}",
            "SK": f"ARTIFACT#{artifact}#V#{version:05d}",
            "artifact_id": artifact,
            "user_id": user_id,
            "version": version,
            "storage": "s3",
            "content_key": f"{user_id}/{artifact}/v{version}/index.html",
            "content_type": "text/html; charset=utf-8",
            "title": title,
            "session_id": session_id,
        }
    )


def _create_share(tc: TestClient, *, artifact="art-1", **body) -> dict:
    payload = {"version": 1, "accessLevel": "public"}
    payload.update(body)
    resp = tc.post(f"/artifacts/{artifact}/shares", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ------------------------------------------------------------------
# Create
# ------------------------------------------------------------------


def test_create_writes_both_rows_transactionally(env) -> None:
    """Both the owner row and the share-lookup row must exist after a
    single create — the owner row is what makes listing possible, the
    lookup row is what makes the recipient path possible."""
    make_client, ddb = env
    _put_version(ddb)
    share = _create_share(make_client())

    share_id = share["shareId"]
    table = ddb.Table(TABLE)

    owner_row = table.get_item(
        Key={
            "PK": f"USER#{OWNER_ID}",
            "SK": f"SHARE#art-1#V#00001#{share_id}",
        }
    ).get("Item")
    lookup_row = table.get_item(
        Key={"PK": f"SHARE#{share_id}", "SK": "META"}
    ).get("Item")

    assert owner_row is not None
    assert lookup_row is not None
    # Identical attribute sets — only the keys differ. If these drift,
    # the owner's view of who can see a share stops matching what the
    # recipient path actually enforces.
    assert {k: v for k, v in owner_row.items() if k not in ("PK", "SK")} == {
        k: v for k, v in lookup_row.items() if k not in ("PK", "SK")
    }
    assert lookup_row["owner_id"] == OWNER_ID
    assert lookup_row["artifact_id"] == "art-1"
    assert int(lookup_row["version"]) == 1


def test_create_denormalizes_title_and_content_type(env) -> None:
    make_client, ddb = env
    _put_version(ddb, title="Quarterly Deck")
    share = _create_share(make_client())
    assert share["title"] == "Quarterly Deck"
    assert share["contentType"] == "text/html; charset=utf-8"
    assert share["shareUrl"] == f"/shared-artifact/{share['shareId']}"


def test_create_for_unknown_version_is_404(env) -> None:
    make_client, _ = env
    resp = make_client().post(
        "/artifacts/art-1/shares", json={"version": 1, "accessLevel": "public"}
    )
    assert resp.status_code == 404


def test_cannot_share_another_users_artifact(env) -> None:
    """Ownership scoping: the version lookup builds its PK from the
    session user, so someone else's artifact is an indistinguishable
    404 rather than a shareable target."""
    make_client, ddb = env
    _put_version(ddb, user_id="someone-else")
    resp = make_client().post(
        "/artifacts/art-1/shares", json={"version": 1, "accessLevel": "public"}
    )
    assert resp.status_code == 404


def test_specific_requires_allowed_emails(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    resp = make_client().post(
        "/artifacts/art-1/shares",
        json={"version": 1, "accessLevel": "specific"},
    )
    assert resp.status_code == 422


def test_owner_email_is_kept_on_the_allowlist(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    share = _create_share(
        make_client(),
        accessLevel="specific",
        allowedEmails=["friend@x.com"],
    )
    assert OWNER_EMAIL in share["allowedEmails"]
    assert "friend@x.com" in share["allowedEmails"]


def test_public_share_carries_no_allowlist(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    share = _create_share(make_client())
    assert share["allowedEmails"] is None


def test_version_must_be_positive(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    resp = make_client().post(
        "/artifacts/art-1/shares", json={"version": 0, "accessLevel": "public"}
    )
    assert resp.status_code == 422


# ------------------------------------------------------------------
# List
# ------------------------------------------------------------------


def test_list_is_scoped_to_the_artifact_and_the_owner(env) -> None:
    make_client, ddb = env
    _put_version(ddb, artifact="art-1")
    _put_version(ddb, artifact="art-2")
    tc = make_client()
    a1 = _create_share(tc, artifact="art-1")
    _create_share(tc, artifact="art-2")

    listed = tc.get("/artifacts/art-1/shares").json()["shares"]
    assert [s["shareId"] for s in listed] == [a1["shareId"]]


def test_list_does_not_leak_another_owners_shares(env) -> None:
    make_client, ddb = env
    _put_version(ddb, artifact="art-1")
    _create_share(make_client(), artifact="art-1")

    other = User(email="other@x.com", user_id="other-1", name="O", roles=[])
    listed = make_client(other).get("/artifacts/art-1/shares").json()
    assert listed["shares"] == []


def test_list_for_unshared_artifact_is_empty_not_404(env) -> None:
    """An empty list reveals nothing about whether the artifact exists."""
    make_client, _ = env
    resp = make_client().get("/artifacts/does-not-exist/shares")
    assert resp.status_code == 200
    assert resp.json()["shares"] == []


def test_multiple_versions_of_one_artifact_list_together(env) -> None:
    make_client, ddb = env
    _put_version(ddb, version=1)
    _put_version(ddb, version=2)
    tc = make_client()
    _create_share(tc, version=1)
    _create_share(tc, version=2)

    listed = tc.get("/artifacts/art-1/shares").json()["shares"]
    assert sorted(s["version"] for s in listed) == [1, 2]


# ------------------------------------------------------------------
# Update
# ------------------------------------------------------------------


def test_update_changes_access_level_on_both_rows(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    tc = make_client()
    share = _create_share(
        tc, accessLevel="specific", allowedEmails=["friend@x.com"]
    )
    share_id = share["shareId"]

    resp = tc.patch(
        f"/artifacts/shares/{share_id}", json={"accessLevel": "public"}
    )
    assert resp.status_code == 200
    assert resp.json()["accessLevel"] == "public"
    # Switching to public clears the stale allowlist rather than leaving
    # a list that no longer gates anything.
    assert resp.json()["allowedEmails"] is None

    table = ddb.Table(TABLE)
    for key in (
        {"PK": f"USER#{OWNER_ID}", "SK": f"SHARE#art-1#V#00001#{share_id}"},
        {"PK": f"SHARE#{share_id}", "SK": "META"},
    ):
        row = table.get_item(Key=key)["Item"]
        assert row["access_level"] == "public"
        assert "allowed_emails" not in row


def test_update_replaces_the_allowlist(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    tc = make_client()
    share = _create_share(
        tc, accessLevel="specific", allowedEmails=["a@x.com"]
    )
    resp = tc.patch(
        f"/artifacts/shares/{share['shareId']}",
        json={"accessLevel": "specific", "allowedEmails": ["b@x.com"]},
    )
    assert resp.status_code == 200
    emails = resp.json()["allowedEmails"]
    assert "b@x.com" in emails
    assert "a@x.com" not in emails
    assert OWNER_EMAIL in emails


def test_update_by_non_owner_is_403(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    share = _create_share(make_client())

    other = User(email="other@x.com", user_id="other-1", name="O", roles=[])
    resp = make_client(other).patch(
        f"/artifacts/shares/{share['shareId']}", json={"accessLevel": "public"}
    )
    assert resp.status_code == 403


def test_update_unknown_share_is_404(env) -> None:
    make_client, _ = env
    resp = make_client().patch(
        "/artifacts/shares/nope", json={"accessLevel": "public"}
    )
    assert resp.status_code == 404


# ------------------------------------------------------------------
# Revoke
# ------------------------------------------------------------------


def test_revoke_deletes_both_rows(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    tc = make_client()
    share = _create_share(tc)
    share_id = share["shareId"]

    assert tc.delete(f"/artifacts/shares/{share_id}").status_code == 204

    table = ddb.Table(TABLE)
    assert "Item" not in table.get_item(
        Key={"PK": f"SHARE#{share_id}", "SK": "META"}
    )
    assert "Item" not in table.get_item(
        Key={
            "PK": f"USER#{OWNER_ID}",
            "SK": f"SHARE#art-1#V#00001#{share_id}",
        }
    )


def test_revoked_share_404s_for_the_recipient(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    tc = make_client()
    share = _create_share(tc)
    tc.delete(f"/artifacts/shares/{share['shareId']}")

    viewer = User(email="v@x.com", user_id="viewer-1", name="V", roles=[])
    resp = make_client(viewer).get(f"/shared-artifacts/{share['shareId']}")
    assert resp.status_code == 404


def test_revoke_by_non_owner_is_403_and_leaves_the_share_live(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    tc = make_client()
    share = _create_share(tc)

    other = User(email="other@x.com", user_id="other-1", name="O", roles=[])
    assert (
        make_client(other)
        .delete(f"/artifacts/shares/{share['shareId']}")
        .status_code
        == 403
    )
    assert (
        tc.get(f"/shared-artifacts/{share['shareId']}").status_code == 200
    )


def test_revoke_unknown_share_is_404(env) -> None:
    make_client, _ = env
    assert make_client().delete("/artifacts/shares/nope").status_code == 404


# ------------------------------------------------------------------
# Recipient metadata
# ------------------------------------------------------------------


def test_recipient_metadata_shape_never_carries_content(env) -> None:
    make_client, ddb = env
    _put_version(ddb, title="Shared Chart")
    share = _create_share(make_client())

    viewer = User(email="v@x.com", user_id="viewer-1", name="V", roles=[])
    body = make_client(viewer).get(
        f"/shared-artifacts/{share['shareId']}"
    ).json()

    assert body["title"] == "Shared Chart"
    assert body["ownerEmail"] == OWNER_EMAIL
    assert body["version"] == 1
    assert body["canDownload"] is True
    # Content never travels on this route, and neither do the owner's
    # internal ids or the rest of the allowlist.
    for leaked in ("content", "ownerId", "artifactId", "allowedEmails"):
        assert leaked not in body


def test_recipient_metadata_denies_a_disallowed_viewer(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    share = _create_share(
        make_client(), accessLevel="specific", allowedEmails=["friend@x.com"]
    )

    stranger = User(
        email="stranger@x.com", user_id="stranger-1", name="S", roles=[]
    )
    resp = make_client(stranger).get(f"/shared-artifacts/{share['shareId']}")
    assert resp.status_code == 403


# ------------------------------------------------------------------
# Auth
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/artifacts/art-1/shares", {"version": 1, "accessLevel": "public"}),
        ("get", "/artifacts/art-1/shares", None),
        ("patch", "/artifacts/shares/s1", {"accessLevel": "public"}),
        ("delete", "/artifacts/shares/s1", None),
        ("get", "/shared-artifacts/s1", None),
        ("post", "/shared-artifacts/s1/render-token", None),
    ],
)
def test_every_route_requires_a_session(method, path, body) -> None:
    """No dependency override and no session cookie → blocked by the
    session dependency before any share logic runs. There is no
    anonymous path to a shared artifact; "public" means any
    *authenticated* tenant user."""
    app = FastAPI()
    app.include_router(artifact_shares_router)
    app.include_router(shared_artifacts_router)
    tc = TestClient(app)
    kwargs = {"json": body} if body is not None else {}
    assert getattr(tc, method)(path, **kwargs).status_code == 401


# ------------------------------------------------------------------
# Session-delete cascade
# ------------------------------------------------------------------
#
# When a conversation is deleted its artifacts outlive it, so their
# share links have to be revoked or they keep working forever. Runs as a
# background task after the 204, so it must never raise.


from apis.app_api.artifacts.service import ArtifactShareService  # noqa: E402


def _put_head(
    ddb,
    *,
    user_id: str = OWNER_ID,
    artifact: str = "art-1",
    session_id: str = "sess-1",
) -> None:
    """The HEAD row carrying GSI1PK — only HEAD rows are on SessionIndex."""
    ddb.Table(TABLE).put_item(
        Item={
            "PK": f"USER#{user_id}",
            "SK": f"ARTIFACT#{artifact}#HEAD",
            "GSI1PK": f"SESSION#{session_id}",
            "GSI1SK": f"ARTIFACT#2026-09-04#{artifact}",
            "artifact_id": artifact,
            "user_id": user_id,
            "session_id": session_id,
            "version": 1,
            "title": "T",
            "content_type": "text/html; charset=utf-8",
        }
    )


def _cascade(session_id: str = "sess-1", owner: str = OWNER_ID) -> int:
    return ArtifactShareService().delete_for_session(session_id, owner)


def test_cascade_revokes_both_rows_of_every_share(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    _put_head(ddb)
    tc = make_client()
    a = _create_share(tc)
    b = _create_share(tc)

    assert _cascade() == 2

    table = ddb.Table(TABLE)
    for share in (a, b):
        sid = share["shareId"]
        assert "Item" not in table.get_item(
            Key={"PK": f"SHARE#{sid}", "SK": "META"}
        )
        assert "Item" not in table.get_item(
            Key={
                "PK": f"USER#{OWNER_ID}",
                "SK": f"SHARE#art-1#V#00001#{sid}",
            }
        )


def test_cascade_kills_the_recipient_path(env) -> None:
    """The lookup row is the capability, so a cascaded share must stop
    resolving — that is the whole point of the cleanup."""
    make_client, ddb = env
    _put_version(ddb)
    _put_head(ddb)
    share = _create_share(make_client())

    viewer = User(email="v@x.com", user_id="viewer-1", name="V", roles=[])
    assert (
        make_client(viewer)
        .get(f"/shared-artifacts/{share['shareId']}")
        .status_code
        == 200
    )

    _cascade()

    assert (
        make_client(viewer)
        .get(f"/shared-artifacts/{share['shareId']}")
        .status_code
        == 404
    )


def test_cascade_covers_every_artifact_and_version_in_the_session(env) -> None:
    make_client, ddb = env
    for artifact in ("art-1", "art-2"):
        _put_head(ddb, artifact=artifact)
        _put_version(ddb, artifact=artifact, version=1)
        _put_version(ddb, artifact=artifact, version=2)
    tc = make_client()
    _create_share(tc, artifact="art-1", version=1)
    _create_share(tc, artifact="art-1", version=2)
    _create_share(tc, artifact="art-2", version=1)

    assert _cascade() == 3
    assert tc.get("/artifacts/art-1/shares").json()["shares"] == []
    assert tc.get("/artifacts/art-2/shares").json()["shares"] == []


def test_cascade_leaves_other_sessions_alone(env) -> None:
    make_client, ddb = env
    _put_head(ddb, artifact="art-1", session_id="sess-1")
    _put_head(ddb, artifact="art-2", session_id="sess-2")
    _put_version(ddb, artifact="art-1")
    _put_version(ddb, artifact="art-2")
    tc = make_client()
    _create_share(tc, artifact="art-1")
    keep = _create_share(tc, artifact="art-2")

    assert _cascade("sess-1") == 1

    remaining = tc.get("/artifacts/art-2/shares").json()["shares"]
    assert [s["shareId"] for s in remaining] == [keep["shareId"]]


def test_cascade_never_touches_another_owners_shares(env) -> None:
    """SessionIndex is not user-partitioned, so a borrowed or colliding
    session id must not reach someone else's shares. The owner filter is
    the only thing preventing that."""
    make_client, ddb = env
    _put_version(ddb)
    _put_head(ddb)
    share = _create_share(make_client())

    # Same session id, a different caller.
    assert _cascade("sess-1", owner="someone-else") == 0
    assert (
        make_client().get("/artifacts/art-1/shares").json()["shares"][0][
            "shareId"
        ]
        == share["shareId"]
    )


def test_cascade_on_a_session_with_no_artifacts_is_zero(env) -> None:
    make_client, _ = env
    assert _cascade("sess-empty") == 0


def test_cascade_on_artifacts_with_no_shares_is_zero(env) -> None:
    make_client, ddb = env
    _put_head(ddb)
    _put_version(ddb)
    assert _cascade() == 0


def test_cascade_returns_zero_when_artifacts_are_not_configured(
    env, monkeypatch
) -> None:
    """The session routes are always mounted; artifacts may not be. That
    is a normal no-op, not an error."""
    make_client, ddb = env
    _put_version(ddb)
    _put_head(ddb)
    _create_share(make_client())
    token_service._reset_caches_for_tests()
    monkeypatch.delenv("DYNAMODB_ARTIFACTS_TABLE_NAME", raising=False)

    assert _cascade() == 0


def test_cascade_swallows_a_backing_store_failure(env, monkeypatch) -> None:
    """It runs after the 204 has been sent, so raising would only produce
    an unhandled background-task error — the failure mode is an orphan
    row, never a blocked delete."""
    make_client, ddb = env
    _put_version(ddb)
    _put_head(ddb)
    _create_share(make_client())

    def boom(*_args, **_kwargs):
        raise RuntimeError("dynamo is down")

    monkeypatch.setattr(
        ArtifactShareService, "_shares_for_session", staticmethod(boom)
    )
    assert _cascade() == 0


def test_cascade_deletes_the_lookup_row_before_the_owner_row(
    env, monkeypatch
) -> None:
    """Ordering is the safety property. The lookup row is what the
    recipient path resolves, so it goes first: a half-finished cascade
    then leaves an inert owner row rather than a live share its owner can
    no longer see to revoke."""
    make_client, ddb = env
    _put_version(ddb)
    _put_head(ddb)
    _create_share(make_client())

    seen: list[str] = []
    real_table = token_service._table()

    class _RecordingBatch:
        def __init__(self, inner):
            self._inner = inner

        def delete_item(self, Key):  # noqa: N803 — boto3 kwarg name
            seen.append("lookup" if Key["PK"].startswith("SHARE#") else "owner")
            return self._inner.delete_item(Key=Key)

    class _RecordingWriter:
        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            return _RecordingBatch(self._inner.__enter__())

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

    class _RecordingTable:
        """Delegates everything to the real table, recording which key
        space each batched delete targets."""

        def __getattr__(self, name):
            return getattr(real_table, name)

        def batch_writer(self, **kwargs):
            return _RecordingWriter(real_table.batch_writer(**kwargs))

    # Patch the module-level accessor: the service holds its own cached
    # Table, and boto3 builds a distinct class per resource instance, so
    # patching the class off a different resource would miss.
    monkeypatch.setattr(token_service, "_table", lambda: _RecordingTable())

    assert _cascade() == 1
    assert seen == ["lookup", "owner"], seen
