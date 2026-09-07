"""Tests for the recipient share inbox ("Shared with you").

Three things are under test here, and they are not the same thing:

* the **fan-out rows** — written unconditionally by every share write,
  torn down by every teardown path;
* the **inbox read** — which never trusts those rows, resolving each one
  through the share lookup row before showing or counting it;
* the **flag** — which gates the read and must never gate the write.

The last of those is the one worth breaking a test over: if the writes
were ever put behind the flag, enabling it would surface an inbox
missing every share created while it was off.
"""

from __future__ import annotations

import base64

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
FRIEND_ID = "friend-1"
FRIEND_EMAIL = "friend@x.com"


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    token_service._reset_caches_for_tests()


@pytest.fixture(autouse=True)
def _inbox_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests here exercise the surface, so default it on. The tests
    that care about the flag set it themselves."""
    monkeypatch.setenv("ARTIFACT_SHARE_INBOX_ENABLED", "true")


def _owner() -> User:
    return User(email=OWNER_EMAIL, user_id=OWNER_ID, name="Owner", roles=[])


def _friend(email: str = FRIEND_EMAIL) -> User:
    return User(email=email, user_id=FRIEND_ID, name="Friend", roles=[])


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
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
    artifact: str = "art-1",
    version: int = 1,
    title: str = "My Chart",
    session_id: str = "sess-1",
) -> None:
    ddb.Table(TABLE).put_item(
        Item={
            "PK": f"USER#{OWNER_ID}",
            "SK": f"ARTIFACT#{artifact}#V#{version:05d}",
            "artifact_id": artifact,
            "user_id": OWNER_ID,
            "version": version,
            "storage": "s3",
            "content_key": f"{OWNER_ID}/{artifact}/v{version}/index.html",
            "content_type": "text/html; charset=utf-8",
            "title": title,
            "session_id": session_id,
        }
    )


def _share_with(
    tc: TestClient, emails: list[str], *, artifact: str = "art-1"
) -> dict:
    resp = tc.post(
        f"/artifacts/{artifact}/shares",
        json={
            "version": 1,
            "accessLevel": "specific",
            "allowedEmails": emails,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _recipient_rows(ddb, email: str) -> list[dict]:
    resp = ddb.Table(TABLE).query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq(
            f"SHARED_WITH#{email}"
        )
    )
    return resp.get("Items", [])


def _inbox(tc: TestClient, **params) -> dict:
    resp = tc.get("/shared-artifacts", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ------------------------------------------------------------------
# Fan-out rows
# ------------------------------------------------------------------


def test_share_fans_out_one_row_per_recipient(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    _share_with(make_client(), [FRIEND_EMAIL, "other@x.com"])

    assert len(_recipient_rows(ddb, FRIEND_EMAIL)) == 1
    assert len(_recipient_rows(ddb, "other@x.com")) == 1


def test_fan_out_row_is_a_pointer_carrying_no_title(env) -> None:
    """The row must not denormalize display fields.

    Share rows carry a title; copying it per recipient would multiply
    every future staleness bug by the size of the allowlist, and would
    make a rename cost one write per recipient instead of one per
    share."""
    make_client, ddb = env
    _put_version(ddb, title="Quarterly Deck")
    _share_with(make_client(), [FRIEND_EMAIL])

    row = _recipient_rows(ddb, FRIEND_EMAIL)[0]
    assert "title" not in row
    assert "content_type" not in row
    assert row["share_id"]
    assert row["owner_id"] == OWNER_ID


def test_fan_out_normalizes_the_email_to_lower_case(env) -> None:
    """Addresses are stored as typed and lowercased only at compare time,
    so the partition key has to fold explicitly. Without this, sharing to
    a capitalised address returns an empty inbox to the person it was
    shared with — a wrong answer that looks exactly like "nobody has
    shared anything with you"."""
    make_client, ddb = env
    _put_version(ddb)
    _share_with(make_client(), ["Friend@X.com"])

    assert len(_recipient_rows(ddb, FRIEND_EMAIL)) == 1

    body = _inbox(make_client(_friend()))
    assert len(body["artifacts"]) == 1


def test_owner_is_not_fanned_out_to_themselves(env) -> None:
    """`_resolve_allowed_emails` deliberately keeps the owner on the
    allowlist, so the fan-out has to filter them back out — otherwise
    sharing your own artifact files it under "shared with you"."""
    make_client, ddb = env
    _put_version(ddb)
    _share_with(make_client(), [FRIEND_EMAIL])

    assert _recipient_rows(ddb, OWNER_EMAIL) == []
    assert _inbox(make_client())["artifacts"] == []


def test_public_shares_are_not_fanned_out(env) -> None:
    """"Public" means any authenticated tenant user — there is no
    recipient list to fan out to, and an inbox listing every public share
    in the tenant is a different feature."""
    make_client, ddb = env
    _put_version(ddb)
    resp = make_client().post(
        "/artifacts/art-1/shares",
        json={"version": 1, "accessLevel": "public"},
    )
    assert resp.status_code == 201

    assert _recipient_rows(ddb, FRIEND_EMAIL) == []
    assert _inbox(make_client(_friend()))["artifacts"] == []


def test_update_diffs_the_allowlist_rather_than_rewriting_it(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    share = _share_with(make_client(), [FRIEND_EMAIL, "dropped@x.com"])

    resp = make_client().patch(
        f"/artifacts/shares/{share['shareId']}",
        json={
            "accessLevel": "specific",
            "allowedEmails": [FRIEND_EMAIL, "added@x.com"],
        },
    )
    assert resp.status_code == 200, resp.text

    assert len(_recipient_rows(ddb, FRIEND_EMAIL)) == 1  # kept, not duped
    assert len(_recipient_rows(ddb, "added@x.com")) == 1
    assert _recipient_rows(ddb, "dropped@x.com") == []


def test_switching_a_share_to_public_clears_every_inbox(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    share = _share_with(make_client(), [FRIEND_EMAIL])

    resp = make_client().patch(
        f"/artifacts/shares/{share['shareId']}",
        json={"accessLevel": "public"},
    )
    assert resp.status_code == 200, resp.text

    # Still reachable by link, no longer discoverable.
    assert _recipient_rows(ddb, FRIEND_EMAIL) == []
    assert (
        make_client(_friend()).get(
            f"/shared-artifacts/{share['shareId']}"
        ).status_code
        == 200
    )


def test_revoke_removes_the_fan_out_rows(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    share = _share_with(make_client(), [FRIEND_EMAIL])

    assert make_client().delete(
        f"/artifacts/shares/{share['shareId']}"
    ).status_code == 204

    assert _recipient_rows(ddb, FRIEND_EMAIL) == []
    assert _inbox(make_client(_friend()))["artifacts"] == []


# ------------------------------------------------------------------
# The read — never trusts the pointer
# ------------------------------------------------------------------


def test_inbox_lists_shares_received(env) -> None:
    make_client, ddb = env
    _put_version(ddb, title="Quarterly Deck")
    share = _share_with(make_client(), [FRIEND_EMAIL])

    body = _inbox(make_client(_friend()))
    assert len(body["artifacts"]) == 1
    row = body["artifacts"][0]
    assert row["shareId"] == share["shareId"]
    assert row["title"] == "Quarterly Deck"
    assert row["ownerEmail"] == OWNER_EMAIL
    assert row["shareUrl"] == f"/shared-artifact/{share['shareId']}"
    assert body["nextCursor"] is None


def test_inbox_never_leaks_the_rest_of_the_allowlist(env) -> None:
    """The recipient shape must not carry `allowedEmails` or the owner's
    internal ids — a recipient learns who shared with them, not who else
    it was shared with."""
    make_client, ddb = env
    _put_version(ddb)
    _share_with(make_client(), [FRIEND_EMAIL, "someone.else@x.com"])

    row = _inbox(make_client(_friend()))["artifacts"][0]
    assert "allowedEmails" not in row
    assert "ownerId" not in row
    assert "artifactId" not in row
    assert "someone.else@x.com" not in str(row)


def test_a_stranded_pointer_lists_nothing(env) -> None:
    """A fan-out row whose share is gone must resolve to nothing.

    This is what makes best-effort fan-out safe: teardown that fails
    halfway, or a crash between the two passes, can leave a pointer
    behind, and it must never become a permanent tombstone in somebody's
    inbox."""
    make_client, ddb = env
    _put_version(ddb)
    share = _share_with(make_client(), [FRIEND_EMAIL])

    # Kill the share the way a half-finished revoke would, leaving the
    # pointer in place.
    ddb.Table(TABLE).delete_item(
        Key={"PK": f"SHARE#{share['shareId']}", "SK": "META"}
    )
    assert len(_recipient_rows(ddb, FRIEND_EMAIL)) == 1

    assert _inbox(make_client(_friend()))["artifacts"] == []


def test_a_pointer_whose_allowlist_dropped_you_lists_nothing(env) -> None:
    """Access is re-checked per row against the live share, so the inbox
    and the recipient page can never disagree about who may see what."""
    make_client, ddb = env
    _put_version(ddb)
    share = _share_with(make_client(), [FRIEND_EMAIL])

    # Rewrite the allowlist behind the fan-out row's back, as a partially
    # failed update would.
    table = ddb.Table(TABLE)
    for key in (
        {"PK": f"SHARE#{share['shareId']}", "SK": "META"},
        {
            "PK": f"USER#{OWNER_ID}",
            "SK": f"SHARE#art-1#V#00001#{share['shareId']}",
        },
    ):
        table.update_item(
            Key=key,
            UpdateExpression="SET allowed_emails = :e",
            ExpressionAttributeValues={":e": [OWNER_EMAIL]},
        )

    assert _inbox(make_client(_friend()))["artifacts"] == []


def test_your_own_share_never_appears_in_your_inbox(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    # Fan-out row planted directly, so this tests the read's guard rather
    # than the write's.
    share = _share_with(make_client(), [FRIEND_EMAIL])
    ddb.Table(TABLE).put_item(
        Item={
            "PK": f"SHARED_WITH#{OWNER_EMAIL}",
            "SK": f"SHARE#2026-01-01T00:00:00+00:00#{share['shareId']}",
            "share_id": share["shareId"],
            "owner_id": OWNER_ID,
            "owner_email": OWNER_EMAIL,
            "shared_at": "2026-01-01T00:00:00+00:00",
        }
    )

    assert _inbox(make_client())["artifacts"] == []


def test_inbox_is_scoped_to_the_caller(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    _share_with(make_client(), [FRIEND_EMAIL])

    stranger = User(
        email="stranger@x.com", user_id="s-1", name="S", roles=[]
    )
    assert _inbox(make_client(stranger))["artifacts"] == []


def test_a_viewer_with_no_email_gets_an_empty_inbox(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    _share_with(make_client(), [FRIEND_EMAIL])

    nameless = User(email="", user_id="n-1", name="N", roles=[])
    body = _inbox(make_client(nameless))
    assert body["artifacts"] == []
    assert body["nextCursor"] is None


# ------------------------------------------------------------------
# Pagination
# ------------------------------------------------------------------


def test_inbox_pages_newest_first(env) -> None:
    make_client, ddb = env
    for n in range(1, 4):
        _put_version(ddb, artifact=f"art-{n}", title=f"Doc {n}")
        _share_with(make_client(), [FRIEND_EMAIL], artifact=f"art-{n}")

    friend = make_client(_friend())
    first = _inbox(friend, limit=2)
    assert len(first["artifacts"]) == 2
    assert first["nextCursor"]

    second = _inbox(friend, limit=2, cursor=first["nextCursor"])
    seen = [a["shareId"] for a in first["artifacts"] + second["artifacts"]]
    assert len(set(seen)) == 3  # every share exactly once, no overlap


def test_a_cursor_cannot_reach_another_partition(env) -> None:
    """The cursor carries the sort key only; the partition is rebuilt from
    the session. A cursor forged to name someone else's partition must
    page the caller's own inbox, not theirs."""
    make_client, ddb = env
    _put_version(ddb)
    _share_with(make_client(), [FRIEND_EMAIL])

    forged = base64.urlsafe_b64encode(
        f"SHARED_WITH#{FRIEND_EMAIL}".encode()
    ).decode()
    stranger = User(
        email="stranger@x.com", user_id="s-1", name="S", roles=[]
    )
    assert _inbox(make_client(stranger), cursor=forged)["artifacts"] == []


def test_a_malformed_cursor_restarts_rather_than_erroring(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    _share_with(make_client(), [FRIEND_EMAIL])

    body = _inbox(make_client(_friend()), cursor="not-base64!!")
    assert len(body["artifacts"]) == 1


# ------------------------------------------------------------------
# The flag
# ------------------------------------------------------------------


def test_inbox_404s_while_the_flag_is_off(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_client, ddb = env
    monkeypatch.setenv("ARTIFACT_SHARE_INBOX_ENABLED", "false")
    _put_version(ddb)
    _share_with(make_client(), [FRIEND_EMAIL])

    assert make_client(_friend()).get("/shared-artifacts").status_code == 404


def test_inbox_is_on_when_the_flag_is_unset(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the default-on flip.

    A fork that never sets ``CDK_ARTIFACT_SHARE_INBOX_ENABLED`` should get
    the finished feature, not lose it silently and have to discover a
    variable to get it back."""
    make_client, ddb = env
    monkeypatch.delenv("ARTIFACT_SHARE_INBOX_ENABLED", raising=False)
    _put_version(ddb)
    _share_with(make_client(), [FRIEND_EMAIL])

    assert len(_inbox(make_client(_friend()))["artifacts"]) == 1


def test_fan_out_rows_are_written_while_the_flag_is_off(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing test for the whole flag design.

    If the writes were gated too, turning the flag on would reveal an
    inbox missing every share created while it was off — a wrong answer
    rather than an empty one, and one with no backfill to fix it. So a
    share created dark must still be discoverable the moment the flag
    flips."""
    make_client, ddb = env
    monkeypatch.setenv("ARTIFACT_SHARE_INBOX_ENABLED", "false")
    _put_version(ddb)
    _share_with(make_client(), [FRIEND_EMAIL])

    assert len(_recipient_rows(ddb, FRIEND_EMAIL)) == 1

    monkeypatch.setenv("ARTIFACT_SHARE_INBOX_ENABLED", "true")
    assert len(_inbox(make_client(_friend()))["artifacts"]) == 1


def test_only_the_literal_false_disables_the_inbox(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kill switch, not opt-in — and the empty string is the case that
    matters. An unset GitHub Actions variable forwards ``""``, which must
    resolve to ON. Getting this backwards is how a default-on flag ships
    silently disabled to every fork that never sets the variable."""
    make_client, ddb = env
    _put_version(ddb)
    _share_with(make_client(), [FRIEND_EMAIL])
    friend = make_client(_friend())

    for value in ("false", "FALSE", " False "):
        monkeypatch.setenv("ARTIFACT_SHARE_INBOX_ENABLED", value)
        assert friend.get("/shared-artifacts").status_code == 404, value

    # Everything else — including the empty string, and including junk —
    # leaves the feature on. A typo must not silently disable a surface.
    for value in ("", "  ", "true", "TRUE", " True ", "1", "yes", "FALSE!"):
        monkeypatch.setenv("ARTIFACT_SHARE_INBOX_ENABLED", value)
        assert friend.get("/shared-artifacts").status_code == 200, value


# ------------------------------------------------------------------
# Teardown ordering and the IAM surface
# ------------------------------------------------------------------


class _RecordingTable:
    """Delegates to the real table, recording the DynamoDB API used and,
    for deletes, which key space was hit."""

    def __init__(self, inner, calls: list):
        self._inner = inner
        self._calls = calls

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if callable(attr):

            def _recorded(*args, **kwargs):
                self._calls.append((name, None))
                return attr(*args, **kwargs)

            return _recorded
        return attr

    def delete_item(self, Key):  # noqa: N803 — boto3 kwarg name
        pk = Key["PK"]
        if pk.startswith("SHARED_WITH#"):
            space = "recipient"
        elif pk.startswith("SHARE#"):
            space = "lookup"
        else:
            space = "owner"
        self._calls.append(("delete_item", space))
        return self._inner.delete_item(Key=Key)


def _record(monkeypatch) -> list:
    calls: list = []
    real = token_service._table()
    monkeypatch.setattr(
        token_service, "_table", lambda: _RecordingTable(real, calls)
    )
    return calls


def test_revoke_deletes_recipient_rows_before_the_lookup_row(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovery dies before reachability.

    A crash between the two then leaves a live share nobody can find,
    which is inert. The reverse order leaves a dead share sitting in
    somebody's inbox. Both orders look identical when nothing fails, so
    assert the order rather than the end state."""
    make_client, ddb = env
    _put_version(ddb)
    share = _share_with(make_client(), [FRIEND_EMAIL])

    calls = _record(monkeypatch)
    assert (
        make_client().delete(
            f"/artifacts/shares/{share['shareId']}"
        ).status_code
        == 204
    )

    # The owner and lookup rows go together in the transaction that
    # follows, reached via `table.meta.client` — which this recorder
    # cannot see — so `delete_item` here is exactly the fan-out pass, and
    # its presence proves the fan-out ran before that transaction.
    spaces = [space for name, space in calls if name == "delete_item"]
    assert spaces == ["recipient"], spaces
    # End state, so the ordering assertion above cannot pass on a revoke
    # that did nothing else.
    assert _recipient_rows(ddb, FRIEND_EMAIL) == []
    assert (
        ddb.Table(TABLE)
        .get_item(Key={"PK": f"SHARE#{share['shareId']}", "SK": "META"})
        .get("Item")
        is None
    )


def test_inbox_read_uses_only_iam_granted_dynamodb_actions(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same guard as the delete cascade's, for the read path.

    The app-api task role holds GetItem/PutItem/UpdateItem/DeleteItem/
    Query on this table and nothing else. `BatchGetItem` is its own IAM
    action and is NOT covered by those, so resolving inbox rows with a
    batch read would fail closed in a deployed environment while passing
    every moto test — exactly how `BatchWriteItem` shipped broken in the
    session-delete cascade. Pinning the surface is the only way a unit
    test can catch it."""
    make_client, ddb = env
    _put_version(ddb)
    _share_with(make_client(), [FRIEND_EMAIL])

    calls = _record(monkeypatch)
    _inbox(make_client(_friend()))

    used = {name for name, _ in calls}
    assert "batch_get_item" not in used, used
    assert used <= {"query", "get_item"}, used


def test_fan_out_write_uses_only_iam_granted_dynamodb_actions(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And the write path. `BatchWriteItem` is the trap here."""
    make_client, ddb = env
    _put_version(ddb)

    calls = _record(monkeypatch)
    _share_with(make_client(), [FRIEND_EMAIL, "other@x.com"])

    used = {name for name, _ in calls}
    assert "batch_writer" not in used, used
    assert "batch_write_item" not in used, used
