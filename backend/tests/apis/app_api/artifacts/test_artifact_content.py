"""Tests for the app-api artifact content endpoint (panel code view).

Covers ownership scoping, the Markdown unwrap (+ its fallback), the
inline size cap, and the fail-closed config behavior.
"""

from __future__ import annotations

import base64

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from apis.app_api.artifacts import service as artifact_service
from apis.app_api.artifacts.routes import router as artifacts_router
from apis.shared.auth import User, get_current_user_from_session

TABLE = "test-user-artifacts"
BUCKET = "test-artifacts-bucket"
REGION = "us-east-1"
USER_ID = "user-123"


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


def _put(
    ddb,
    s3,
    *,
    user_id: str = USER_ID,
    artifact: str = "art-1",
    version: int = 1,
    content_type: str = "text/html; charset=utf-8",
    body: bytes = b"<h1>hi</h1>",
    write_object: bool = True,
    content_key: str | None = None,
) -> None:
    key = content_key
    if key is None:
        key = f"{user_id}/{artifact}/v{version}/index.html"
    ddb.Table(TABLE).put_item(
        Item={
            "PK": f"USER#{user_id}",
            "SK": f"ARTIFACT#{artifact}#V#{version:05d}",
            "storage": "s3",
            "content_key": key,
            "content_type": content_type,
        }
    )
    if write_object:
        s3.put_object(Bucket=BUCKET, Key=key, Body=body)


def _markdown_wrapper(md: str) -> bytes:
    b64 = base64.b64encode(md.encode("utf-8")).decode("ascii")
    return (
        "<!doctype html><html><body><main>Rendering…</main>"
        '<script type="application/x-markdown-base64" '
        f'id="md-src">{b64}</script>'
        "<script type=\"module\">/* render */</script>"
        "</body></html>"
    ).encode("utf-8")


def test_happy_path_returns_raw_source(client) -> None:
    tc, ddb, s3 = client
    _put(ddb, s3, body=b"<h1>Hello</h1>")
    resp = tc.get("/artifacts/art-1/content", params={"version": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "<h1>Hello</h1>"
    assert body["content_type"] == "text/html; charset=utf-8"
    assert body["version"] == 1


def test_markdown_is_unwrapped_to_authored_source(client) -> None:
    tc, ddb, s3 = client
    md = "# Title\n\nSome **bold** text.\n"
    _put(
        ddb,
        s3,
        content_type="text/markdown",
        body=_markdown_wrapper(md),
    )
    resp = tc.get("/artifacts/art-1/content", params={"version": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == md
    assert body["content_type"] == "text/markdown"


def test_markdown_without_src_tag_falls_back_to_raw(client) -> None:
    """A Markdown row whose object lacks the embed (legacy / future
    template) returns the raw stored bytes + real type, not an error."""
    tc, ddb, s3 = client
    _put(
        ddb,
        s3,
        content_type="text/markdown",
        body=b"<html><body>no embed here</body></html>",
    )
    resp = tc.get("/artifacts/art-1/content", params={"version": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert "no embed here" in body["content"]
    assert body["content_type"] == "text/markdown"


def test_unknown_version_is_404(client) -> None:
    tc, _, _ = client
    resp = tc.get("/artifacts/art-1/content", params={"version": 1})
    assert resp.status_code == 404


def test_other_users_artifact_is_404(client) -> None:
    tc, ddb, s3 = client
    _put(ddb, s3, user_id="someone-else")
    resp = tc.get("/artifacts/art-1/content", params={"version": 1})
    assert resp.status_code == 404


def test_missing_s3_object_is_404(client) -> None:
    tc, ddb, s3 = client
    _put(ddb, s3, write_object=False)
    resp = tc.get("/artifacts/art-1/content", params={"version": 1})
    assert resp.status_code == 404


def test_oversized_artifact_is_413(client, monkeypatch) -> None:
    tc, ddb, s3 = client
    monkeypatch.setattr(artifact_service, "_MAX_CONTENT_BYTES", 16)
    _put(ddb, s3, body=b"x" * 64)
    resp = tc.get("/artifacts/art-1/content", params={"version": 1})
    assert resp.status_code == 413


def test_missing_bucket_is_500(client, monkeypatch) -> None:
    tc, ddb, s3 = client
    _put(ddb, s3)
    monkeypatch.delenv("S3_ARTIFACTS_BUCKET_NAME", raising=False)
    artifact_service._reset_caches_for_tests()
    resp = tc.get("/artifacts/art-1/content", params={"version": 1})
    assert resp.status_code == 500


def test_version_must_be_positive(client) -> None:
    tc, ddb, s3 = client
    _put(ddb, s3)
    resp = tc.get("/artifacts/art-1/content", params={"version": 0})
    assert resp.status_code == 422


def test_requires_authentication() -> None:
    app = FastAPI()
    app.include_router(artifacts_router)
    resp = TestClient(app).get(
        "/artifacts/art-1/content", params={"version": 1}
    )
    assert resp.status_code == 401
