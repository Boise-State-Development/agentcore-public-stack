"""The security core of artifact sharing.

The share-scoped mint hands a viewer a short-lived credential addressed
to the *owner's* DynamoDB partition. The render Lambda performs no
ownership comparison of its own — it never sees the viewer — so the ACL
check in `mint_for_share` is the only thing between "sharing" and "read
any artifact by id". These tests exist to hold that boundary.

`test_render_lambda_accepts_the_new_claims` is what makes "no Lambda
change required" a verified fact rather than a reading of the handler:
it feeds a token carrying the new `vwr`/`shr` claims straight through
the currently-deployed verifier.
"""

from __future__ import annotations

import boto3
import jwt
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
from lambdas.artifact_render import handler as render_lambda

KEY = "test-render-key-44-chars-of-entropy-aaaaaaaa"
SECRET_NAME = "test-artifact-render-token-key"
TABLE = "test-user-artifacts"
ORIGIN = "https://artifacts.test.example.com"
REGION = "us-east-1"

OWNER_ID = "owner-1"
OWNER_EMAIL = "owner@x.com"
VIEWER_ID = "viewer-1"
VIEWER_EMAIL = "viewer@x.com"


@pytest.fixture(autouse=True)
def _reset_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    token_service._reset_caches_for_tests()
    # The verifier caches its own signing key separately.
    monkeypatch.setattr(render_lambda, "_cached_signing_key", None)


def _user(user_id: str, email: str) -> User:
    return User(email=email, user_id=user_id, name=user_id, roles=[])


OWNER = _user(OWNER_ID, OWNER_EMAIL)
VIEWER = _user(VIEWER_ID, VIEWER_EMAIL)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    with mock_aws():
        monkeypatch.setenv("AWS_REGION", REGION)
        sm = boto3.client("secretsmanager", region_name=REGION)
        arn = sm.create_secret(Name=SECRET_NAME, SecretString=KEY)["ARN"]

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
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        monkeypatch.setenv("ARTIFACTS_RENDER_TOKEN_SECRET_ARN", arn)
        monkeypatch.setenv("DYNAMODB_ARTIFACTS_TABLE_NAME", TABLE)
        monkeypatch.setenv("ARTIFACTS_ORIGIN", ORIGIN)

        def make_client(user: User) -> TestClient:
            app = FastAPI()
            app.include_router(artifact_shares_router)
            app.include_router(shared_artifacts_router)
            app.dependency_overrides[get_current_user_from_session] = (
                lambda: user
            )
            return TestClient(app)

        yield make_client, boto3.resource("dynamodb", region_name=REGION)


def _put_version(
    ddb, *, user_id: str = OWNER_ID, artifact="art-1", version=1
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
            "title": "Shared Artifact",
            "session_id": "sess-1",
        }
    )


def _share(make_client, *, access_level="public", allowed=None, version=1) -> str:
    body: dict = {"version": version, "accessLevel": access_level}
    if allowed is not None:
        body["allowedEmails"] = allowed
    resp = make_client(OWNER).post("/artifacts/art-1/shares", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["shareId"]


def _token_from_url(url: str) -> str:
    assert url.startswith(f"{ORIGIN}/?t=")
    return url.split("?t=", 1)[1]


def _decode(url: str) -> dict:
    return jwt.decode(
        _token_from_url(url),
        KEY,
        algorithms=["HS256"],
        audience="artifact-render",
    )


def _mint(make_client, user: User, share_id: str):
    return make_client(user).post(f"/shared-artifacts/{share_id}/render-token")


# ------------------------------------------------------------------
# The claim contract
# ------------------------------------------------------------------


def test_minted_claims_address_the_owner_and_record_the_viewer(env) -> None:
    """`sub` is the OWNER, not the viewer.

    That is deliberate and load-bearing: `sub` is the DynamoDB partition
    key the render Lambda builds (PK = USER#{sub}), an ADDRESS rather
    than an identity assertion. Setting it to the viewer would point the
    Lambda at the viewer's own partition and the shared artifact would
    simply 404. The real viewer identity travels in `vwr`, and the grant
    it was issued under in `shr`, so a render log attributes the view to
    the person who actually looked instead of crediting it to the owner.

    If this test ever fails because someone "fixed" `sub` to be the
    viewer, the fix is to revert that change — not to update this test.
    """
    make_client, ddb = env
    _put_version(ddb)
    share_id = _share(make_client)

    resp = _mint(make_client, VIEWER, share_id)
    assert resp.status_code == 200
    claims = _decode(resp.json()["url"])

    assert claims["sub"] == OWNER_ID
    assert claims["vwr"] == VIEWER_ID
    assert claims["shr"] == share_id
    assert claims["sub"] != VIEWER_ID

    assert claims["iss"] == "app-api"
    assert claims["aud"] == "artifact-render"
    assert claims["aid"] == "art-1"
    assert claims["ver"] == 1
    assert claims["exp"] - claims["iat"] == 120
    assert resp.json()["expires_at"].endswith("+00:00")


def test_owner_minting_their_own_share_is_still_attributed_to_them(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    share_id = _share(make_client)

    claims = _decode(_mint(make_client, OWNER, share_id).json()["url"])
    assert claims["sub"] == OWNER_ID
    assert claims["vwr"] == OWNER_ID


def test_render_lambda_accepts_the_new_claims(env, monkeypatch) -> None:
    """The currently-deployed verifier must accept `vwr`/`shr`.

    This is what turns "PR-1 needs no Lambda change" into a verified
    fact: `_verify_token` validates a fixed claim list and has no extras
    rejection, so a token carrying the two new claims verifies against
    the handler exactly as it ships today. If a future verifier change
    ever adds strict claim checking, this test fails first — before a
    deploy silently breaks every shared artifact.
    """
    make_client, ddb = env
    _put_version(ddb)
    share_id = _share(make_client)
    monkeypatch.setattr(render_lambda, "_cached_signing_key", KEY)

    token = _token_from_url(_mint(make_client, VIEWER, share_id).json()["url"])

    verified = render_lambda._verify_token(token)
    assert verified["sub"] == OWNER_ID
    assert verified["aid"] == "art-1"
    assert verified["ver"] == 1
    assert verified["vwr"] == VIEWER_ID
    assert verified["shr"] == share_id


def test_shared_token_resolves_to_the_owners_partition(
    env, monkeypatch
) -> None:
    """End-to-end proof that `sub` is an address: the Lambda's own
    record lookup, driven by the minted claims, must land on the owner's
    row. A viewer-valued `sub` would miss it entirely."""
    make_client, ddb = env
    _put_version(ddb)
    share_id = _share(make_client)
    claims = _decode(_mint(make_client, VIEWER, share_id).json()["url"])

    # The Lambda pins its table name at module load from its own env var.
    monkeypatch.setattr(render_lambda, "_ARTIFACTS_TABLE", TABLE)
    monkeypatch.setattr(render_lambda, "_ddb_table", None)

    record = render_lambda._get_version_record(
        claims["sub"], claims["aid"], claims["ver"]
    )
    assert record["user_id"] == OWNER_ID
    assert record["content_key"].startswith(f"{OWNER_ID}/")


# ------------------------------------------------------------------
# The ACL boundary
# ------------------------------------------------------------------


def test_public_admits_an_arbitrary_authenticated_user(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    share_id = _share(make_client, access_level="public")

    stranger = _user("stranger-1", "stranger@x.com")
    resp = _mint(make_client, stranger, share_id)
    assert resp.status_code == 200
    assert _decode(resp.json()["url"])["vwr"] == "stranger-1"


def test_specific_admits_only_allowlisted_emails(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    share_id = _share(
        make_client, access_level="specific", allowed=[VIEWER_EMAIL]
    )

    assert _mint(make_client, VIEWER, share_id).status_code == 200

    stranger = _user("stranger-1", "stranger@x.com")
    assert _mint(make_client, stranger, share_id).status_code == 403


@pytest.mark.parametrize(
    "allowed_email,viewer_email",
    [
        ("Viewer@X.com", "viewer@x.com"),
        ("viewer@x.com", "VIEWER@X.COM"),
        ("ViEwEr@x.CoM", "vIeWeR@X.com"),
    ],
)
def test_specific_matches_emails_case_insensitively(
    env, allowed_email, viewer_email
) -> None:
    """Entra hands back whatever casing the directory holds, so a
    case-sensitive compare would lock out legitimately-invited people."""
    make_client, ddb = env
    _put_version(ddb)
    share_id = _share(
        make_client, access_level="specific", allowed=[allowed_email]
    )

    viewer = _user("viewer-cased", viewer_email)
    assert _mint(make_client, viewer, share_id).status_code == 200


def test_specific_denies_a_near_miss_email(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    share_id = _share(
        make_client, access_level="specific", allowed=["viewer@x.com"]
    )

    for near_miss in (
        "viewer@x.com.evil.com",
        "notviewer@x.com",
        "viewer@y.com",
        " viewer@x.com",
    ):
        impostor = _user("impostor", near_miss)
        assert _mint(make_client, impostor, share_id).status_code == 403


def test_owner_always_passes_a_specific_share(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    share_id = _share(
        make_client, access_level="specific", allowed=["someone-else@x.com"]
    )
    assert _mint(make_client, OWNER, share_id).status_code == 200


def test_revoked_share_mints_nothing(env) -> None:
    make_client, ddb = env
    _put_version(ddb)
    share_id = _share(make_client)
    assert _mint(make_client, VIEWER, share_id).status_code == 200

    make_client(OWNER).delete(f"/artifacts/shares/{share_id}")

    resp = _mint(make_client, VIEWER, share_id)
    assert resp.status_code == 404
    assert "url" not in resp.json()


def test_downgrade_to_specific_locks_out_a_former_public_viewer(env) -> None:
    """Revocation is not the only control: narrowing a live share must
    take effect on the very next mint."""
    make_client, ddb = env
    _put_version(ddb)
    share_id = _share(make_client, access_level="public")
    assert _mint(make_client, VIEWER, share_id).status_code == 200

    make_client(OWNER).patch(
        f"/artifacts/shares/{share_id}",
        json={"accessLevel": "specific", "allowedEmails": ["other@x.com"]},
    )
    assert _mint(make_client, VIEWER, share_id).status_code == 403


def test_unknown_share_id_mints_nothing(env) -> None:
    """A share id is not a capability on its own — guessing one gets a
    404, never a token."""
    make_client, ddb = env
    _put_version(ddb)
    resp = _mint(make_client, VIEWER, "not-a-real-share")
    assert resp.status_code == 404


def test_missing_version_row_404s_instead_of_minting(env) -> None:
    """A share can outlive the version it points at. Better a clean 404
    than a token that renders the Lambda's error page in the recipient's
    iframe."""
    make_client, ddb = env
    _put_version(ddb)
    share_id = _share(make_client)
    ddb.Table(TABLE).delete_item(
        Key={"PK": f"USER#{OWNER_ID}", "SK": "ARTIFACT#art-1#V#00001"}
    )

    resp = _mint(make_client, VIEWER, share_id)
    assert resp.status_code == 404
    assert "url" not in resp.json()


def test_access_check_runs_before_the_version_lookup(env) -> None:
    """A denied viewer must not be able to probe whether the owner's
    artifact version exists — both cases have to be indistinguishable
    from the outside. Here the version row is gone AND the viewer is not
    allowed; the answer must be 403 (the ACL), not 404 (the probe)."""
    make_client, ddb = env
    _put_version(ddb)
    share_id = _share(
        make_client, access_level="specific", allowed=["friend@x.com"]
    )
    ddb.Table(TABLE).delete_item(
        Key={"PK": f"USER#{OWNER_ID}", "SK": "ARTIFACT#art-1#V#00001"}
    )

    stranger = _user("stranger-1", "stranger@x.com")
    assert _mint(make_client, stranger, share_id).status_code == 403


def test_share_pins_the_version_it_was_created_for(env) -> None:
    """A share is pinned to one immutable version. A newer version of
    the same artifact must not change what the recipient sees."""
    make_client, ddb = env
    _put_version(ddb, version=1)
    share_id = _share(make_client, version=1)
    _put_version(ddb, version=2)

    claims = _decode(_mint(make_client, VIEWER, share_id).json()["url"])
    assert claims["ver"] == 1


# ------------------------------------------------------------------
# Fail-closed config
# ------------------------------------------------------------------


def test_missing_origin_is_500_and_mints_nothing(env, monkeypatch) -> None:
    """Origin resolves before any DDB call or ACL check, so a broken
    artifacts deploy fails closed rather than handing back a usable
    token embedded in a relative, unloadable URL."""
    make_client, ddb = env
    _put_version(ddb)
    share_id = _share(make_client)
    monkeypatch.delenv("ARTIFACTS_ORIGIN", raising=False)

    resp = _mint(make_client, VIEWER, share_id)
    assert resp.status_code == 500
    assert "url" not in resp.json()


def test_token_is_never_logged(env, caplog) -> None:
    """The token is a bearer credential carried in a URL. Log lines on
    this path must carry identifiers only."""
    make_client, ddb = env
    _put_version(ddb)
    share_id = _share(make_client)

    with caplog.at_level("INFO"):
        url = _mint(make_client, VIEWER, share_id).json()["url"]

    token = _token_from_url(url)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert token not in logged
    assert url not in logged
    # Identifiers are expected — that is the point of `vwr`/`shr`.
    assert share_id in logged


# ------------------------------------------------------------------
# Shared content (recipient code view)
# ------------------------------------------------------------------
#
# `GET /shared-artifacts/{id}/content` resolves the OWNER's S3 object
# after the share ACL admits the viewer. ArtifactContentService performs
# no access control of its own, so these tests are the boundary.


import boto3 as _boto3  # noqa: E402  (grouped with the content tests below)

BUCKET = "test-artifacts-content"
BODY = "<html><body>shared artifact source</body></html>"


def _put_content(
    *, user_id: str = OWNER_ID, artifact="art-1", version=1, body: str = BODY
) -> None:
    """Write the S3 object the version row's content_key points at."""
    s3 = _boto3.client("s3", region_name=REGION)
    try:
        # us-east-1 rejects an explicit LocationConstraint.
        s3.create_bucket(Bucket=BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    s3.put_object(
        Bucket=BUCKET,
        Key=f"{user_id}/{artifact}/v{version}/index.html",
        Body=body.encode("utf-8"),
        ContentType="text/html; charset=utf-8",
    )


def _content(make_client, user: User, share_id: str):
    return make_client(user).get(f"/shared-artifacts/{share_id}/content")


def test_recipient_reads_the_owners_content(env, monkeypatch) -> None:
    """The whole point: a viewer who is not the owner gets the owner's
    bytes, because the route resolves the owner from the share row after
    the ACL admits them."""
    make_client, ddb = env
    monkeypatch.setenv("S3_ARTIFACTS_BUCKET_NAME", BUCKET)
    _put_version(ddb)
    _put_content()
    share_id = _share(make_client)

    resp = _content(make_client, VIEWER, share_id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == BODY
    assert body["version"] == 1


def test_shared_content_denies_a_disallowed_viewer(env, monkeypatch) -> None:
    make_client, ddb = env
    monkeypatch.setenv("S3_ARTIFACTS_BUCKET_NAME", BUCKET)
    _put_version(ddb)
    _put_content()
    share_id = _share(
        make_client, access_level="specific", allowed=["friend@x.com"]
    )

    stranger = _user("stranger-1", "stranger@x.com")
    resp = _content(make_client, stranger, share_id)
    assert resp.status_code == 403
    assert "content" not in resp.json()


def test_shared_content_404s_after_revoke(env, monkeypatch) -> None:
    make_client, ddb = env
    monkeypatch.setenv("S3_ARTIFACTS_BUCKET_NAME", BUCKET)
    _put_version(ddb)
    _put_content()
    share_id = _share(make_client)
    assert _content(make_client, VIEWER, share_id).status_code == 200

    make_client(OWNER).delete(f"/artifacts/shares/{share_id}")

    resp = _content(make_client, VIEWER, share_id)
    assert resp.status_code == 404
    assert "content" not in resp.json()


def test_shared_content_404s_for_an_unknown_share(env, monkeypatch) -> None:
    make_client, ddb = env
    monkeypatch.setenv("S3_ARTIFACTS_BUCKET_NAME", BUCKET)
    _put_version(ddb)
    _put_content()
    assert _content(make_client, VIEWER, "nope").status_code == 404


def test_shared_content_404s_when_the_version_row_is_gone(
    env, monkeypatch
) -> None:
    make_client, ddb = env
    monkeypatch.setenv("S3_ARTIFACTS_BUCKET_NAME", BUCKET)
    _put_version(ddb)
    _put_content()
    share_id = _share(make_client)
    ddb.Table(TABLE).delete_item(
        Key={"PK": f"USER#{OWNER_ID}", "SK": "ARTIFACT#art-1#V#00001"}
    )
    assert _content(make_client, VIEWER, share_id).status_code == 404


def test_shared_content_serves_the_pinned_version_only(
    env, monkeypatch
) -> None:
    """A newer version must not leak through a share pinned to an older
    one — the share row's version is what addresses the object."""
    make_client, ddb = env
    monkeypatch.setenv("S3_ARTIFACTS_BUCKET_NAME", BUCKET)
    _put_version(ddb, version=1)
    _put_content(version=1, body="V1 BODY")
    share_id = _share(make_client, version=1)

    _put_version(ddb, version=2)
    _put_content(version=2, body="V2 BODY")

    body = _content(make_client, VIEWER, share_id).json()
    assert body["content"] == "V1 BODY"
    assert body["version"] == 1


def test_shared_content_413s_an_oversized_artifact(env, monkeypatch) -> None:
    """Recipients get the same steer-to-download signal owners do."""
    make_client, ddb = env
    monkeypatch.setenv("S3_ARTIFACTS_BUCKET_NAME", BUCKET)
    _put_version(ddb)
    _put_content(body="x" * (2 * 1024 * 1024 + 10))
    share_id = _share(make_client)

    assert _content(make_client, VIEWER, share_id).status_code == 413


def test_owner_content_route_is_unchanged_for_a_recipient(
    env, monkeypatch
) -> None:
    """The owner route must stay self-scoped: it builds its key from the
    session user, so a recipient asking it for the owner's artifact gets
    a 404 no matter what share they hold. If this ever starts returning
    200, the owner route has been made share-aware and the two access
    models have been conflated."""
    make_client, ddb = env
    monkeypatch.setenv("S3_ARTIFACTS_BUCKET_NAME", BUCKET)
    _put_version(ddb)
    _put_content()
    _share(make_client)

    resp = make_client(VIEWER).get("/artifacts/art-1/content?version=1")
    assert resp.status_code == 404
