"""Tests for the artifact render Lambda.

Two layers:
  * Token verification matrix — pure stdlib HS256 logic, no AWS.
  * Handler integration — full request flow against moto-backed
    Secrets Manager, DynamoDB, and S3.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

import boto3
import pytest
from moto import mock_aws

from lambdas.artifact_render import handler

KEY = "test-signing-key-44-chars-of-entropy-aaaaaaa"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _mint(
    claims: dict[str, Any],
    *,
    key: str = KEY,
    alg: str = "HS256",
    tamper_sig: bool = False,
) -> str:
    header = _b64url(json.dumps({"alg": alg, "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(claims).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    sig = hmac.new(key.encode(), signing_input, hashlib.sha256).digest()
    sig_b64 = _b64url(sig)
    if tamper_sig:
        sig_b64 = ("A" if sig_b64[0] != "A" else "B") + sig_b64[1:]
    return f"{header}.{payload}.{sig_b64}"


def _valid_claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    base = {
        "sub": "user-123",
        "aid": "artifact-abc",
        "ver": 1,
        "sid": "session-xyz",
        "iss": "app-api",
        "aud": "artifact-render",
        "iat": now,
        "exp": now + 90,
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts from clean module-scoped caches."""
    monkeypatch.setattr(handler, "_cached_signing_key", None)
    monkeypatch.setattr(handler, "_secrets_client", None)
    monkeypatch.setattr(handler, "_s3_client", None)
    monkeypatch.setattr(handler, "_ddb_table", None)


# --------------------------------------------------------------------------
# Token verification matrix (no AWS — signing key injected directly).
# --------------------------------------------------------------------------


@pytest.fixture
def _injected_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "_cached_signing_key", KEY)


def test_valid_token_returns_claims(_injected_key: None) -> None:
    claims = handler._verify_token(_mint(_valid_claims()))
    assert claims["sub"] == "user-123"
    assert claims["aid"] == "artifact-abc"
    assert claims["ver"] == 1


def test_tampered_signature_rejected(_injected_key: None) -> None:
    with pytest.raises(handler._TokenError):
        handler._verify_token(_mint(_valid_claims(), tamper_sig=True))


def test_wrong_signing_key_rejected(_injected_key: None) -> None:
    forged = _mint(_valid_claims(), key="a-different-key")
    with pytest.raises(handler._TokenError):
        handler._verify_token(forged)


def test_alg_none_rejected(_injected_key: None) -> None:
    with pytest.raises(handler._TokenError):
        handler._verify_token(_mint(_valid_claims(), alg="none"))


def test_alg_confusion_rejected(_injected_key: None) -> None:
    with pytest.raises(handler._TokenError):
        handler._verify_token(_mint(_valid_claims(), alg="HS512"))


def test_expired_token_rejected(_injected_key: None) -> None:
    now = int(time.time())
    with pytest.raises(handler._TokenError):
        handler._verify_token(_mint(_valid_claims(iat=now - 200, exp=now - 100)))


def test_expiry_within_leeway_accepted(_injected_key: None) -> None:
    now = int(time.time())
    claims = handler._verify_token(_mint(_valid_claims(iat=now - 4, exp=now - 3)))
    assert claims["sub"] == "user-123"


def test_future_iat_rejected(_injected_key: None) -> None:
    now = int(time.time())
    with pytest.raises(handler._TokenError):
        handler._verify_token(_mint(_valid_claims(iat=now + 100, exp=now + 200)))


def test_overlong_lifetime_rejected(_injected_key: None) -> None:
    now = int(time.time())
    over = handler._MAX_TOKEN_LIFETIME_SECONDS + 60
    with pytest.raises(handler._TokenError):
        handler._verify_token(_mint(_valid_claims(iat=now, exp=now + over)))


def test_wrong_issuer_rejected(_injected_key: None) -> None:
    with pytest.raises(handler._TokenError):
        handler._verify_token(_mint(_valid_claims(iss="evil")))


def test_wrong_audience_rejected(_injected_key: None) -> None:
    with pytest.raises(handler._TokenError):
        handler._verify_token(_mint(_valid_claims(aud="some-other-service")))


def test_missing_exp_rejected(_injected_key: None) -> None:
    claims = _valid_claims()
    del claims["exp"]
    with pytest.raises(handler._TokenError):
        handler._verify_token(_mint(claims))


def test_missing_iat_rejected(_injected_key: None) -> None:
    # `iat` is mandatory — without it the lifetime cap can't be enforced.
    claims = _valid_claims()
    del claims["iat"]
    with pytest.raises(handler._TokenError):
        handler._verify_token(_mint(claims))


@pytest.mark.parametrize("bad_iat", ["123", True])
def test_non_numeric_iat_rejected(_injected_key: None, bad_iat: Any) -> None:
    # A string or bool `iat` must not slip past the numeric guard
    # (bool is an int subclass).
    with pytest.raises(handler._TokenError):
        handler._verify_token(_mint(_valid_claims(iat=bad_iat)))


@pytest.mark.parametrize("bad_ver", [0, -1, True, "1", 1.0])
def test_invalid_version_rejected(_injected_key: None, bad_ver: Any) -> None:
    with pytest.raises(handler._TokenError):
        handler._verify_token(_mint(_valid_claims(ver=bad_ver)))


@pytest.mark.parametrize("missing", ["sub", "aid"])
def test_missing_identity_claim_rejected(_injected_key: None, missing: str) -> None:
    claims = _valid_claims()
    del claims[missing]
    with pytest.raises(handler._TokenError):
        handler._verify_token(_mint(claims))


@pytest.mark.parametrize("token", ["", "a.b", "a.b.c.d", "not-a-token"])
def test_malformed_token_rejected(_injected_key: None, token: str) -> None:
    with pytest.raises(handler._TokenError):
        handler._verify_token(token)


def test_non_dict_header_rejected(_injected_key: None) -> None:
    # Header decodes to a JSON array, not an object.
    header = _b64url(json.dumps(["HS256"]).encode())
    payload = _b64url(json.dumps(_valid_claims()).encode())
    sig = _b64url(
        hmac.new(
            KEY.encode(), f"{header}.{payload}".encode("ascii"), hashlib.sha256
        ).digest()
    )
    with pytest.raises(handler._TokenError):
        handler._verify_token(f"{header}.{payload}.{sig}")


def test_non_dict_payload_rejected(_injected_key: None) -> None:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(["not", "an", "object"]).encode())
    sig = _b64url(
        hmac.new(
            KEY.encode(), f"{header}.{payload}".encode("ascii"), hashlib.sha256
        ).digest()
    )
    with pytest.raises(handler._TokenError):
        handler._verify_token(f"{header}.{payload}.{sig}")


# --------------------------------------------------------------------------
# Handler integration (moto-backed Secrets Manager + DynamoDB + S3).
# --------------------------------------------------------------------------

SECRET_ARN_NAME = "test-artifact-render-token-key"
TABLE = "test-user-artifacts"
BUCKET = "test-artifacts-content"
CONTENT_KEY = "user-123/artifact-abc/v1/index.html"
DOC = "<!doctype html><html><body><h1>hi</h1></body></html>"


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch):
    with mock_aws():
        sm = boto3.client("secretsmanager", region_name="us-east-1")
        secret = sm.create_secret(Name=SECRET_ARN_NAME, SecretString=KEY)

        ddb = boto3.client("dynamodb", region_name="us-east-1")
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

        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        s3.put_object(Bucket=BUCKET, Key=CONTENT_KEY, Body=DOC.encode())

        monkeypatch.setattr(handler, "_RENDER_TOKEN_SECRET_ARN", secret["ARN"])
        monkeypatch.setattr(handler, "_ARTIFACTS_TABLE", TABLE)
        monkeypatch.setattr(handler, "_ARTIFACTS_BUCKET", BUCKET)
        monkeypatch.setattr(handler, "_FRAME_ANCESTOR", "https://app.example.com")

        yield {"ddb": boto3.resource("dynamodb", region_name="us-east-1")}


def _put_record(ddb, **overrides: Any) -> None:
    item = {
        "PK": "USER#user-123",
        "SK": "ARTIFACT#artifact-abc#V#00001",
        "storage": "s3",
        "content_key": CONTENT_KEY,
        "content_type": "text/html; charset=utf-8",
    }
    item.update(overrides)
    ddb.Table(TABLE).put_item(Item=item)


def _event(token: str | None, method: str = "GET") -> dict[str, Any]:
    return {
        "requestContext": {"http": {"method": method}},
        "queryStringParameters": {"t": token} if token else {},
        "rawQueryString": f"t={token}" if token else "",
    }


def test_happy_path_returns_content(aws_env) -> None:
    _put_record(aws_env["ddb"])
    resp = handler.handler(_event(_mint(_valid_claims())), None)
    assert resp["statusCode"] == 200
    assert resp["body"] == DOC
    assert resp["headers"]["cache-control"] == "no-store"
    assert "frame-ancestors https://app.example.com" in (
        resp["headers"]["content-security-policy"]
    )


def test_secret_fetched_from_secrets_manager(aws_env) -> None:
    # _cached_signing_key is None (reset fixture) so this exercises the
    # real Secrets Manager round-trip, not an injected key.
    _put_record(aws_env["ddb"])
    resp = handler.handler(_event(_mint(_valid_claims())), None)
    assert resp["statusCode"] == 200


def test_head_request_omits_body(aws_env) -> None:
    _put_record(aws_env["ddb"])
    resp = handler.handler(_event(_mint(_valid_claims()), method="HEAD"), None)
    assert resp["statusCode"] == 200
    assert resp["body"] == ""


def test_token_from_raw_query_string(aws_env) -> None:
    _put_record(aws_env["ddb"])
    token = _mint(_valid_claims())
    event = {
        "requestContext": {"http": {"method": "GET"}},
        "queryStringParameters": None,
        "rawQueryString": f"t={token}",
    }
    assert handler.handler(event, None)["statusCode"] == 200


def test_missing_token_is_403(aws_env) -> None:
    resp = handler.handler(_event(None), None)
    assert resp["statusCode"] == 403


def test_tampered_token_is_403(aws_env) -> None:
    _put_record(aws_env["ddb"])
    bad = _mint(_valid_claims(), tamper_sig=True)
    assert handler.handler(_event(bad), None)["statusCode"] == 403


def test_non_get_method_is_405(aws_env) -> None:
    resp = handler.handler(_event(_mint(_valid_claims()), method="POST"), None)
    assert resp["statusCode"] == 405


def test_missing_version_record_is_404(aws_env) -> None:
    resp = handler.handler(_event(_mint(_valid_claims())), None)
    assert resp["statusCode"] == 404


def test_unsupported_storage_is_500(aws_env) -> None:
    _put_record(aws_env["ddb"], storage="inline")
    resp = handler.handler(_event(_mint(_valid_claims())), None)
    assert resp["statusCode"] == 500


def test_record_without_content_key_is_404(aws_env) -> None:
    ddb = aws_env["ddb"]
    ddb.Table(TABLE).put_item(
        Item={
            "PK": "USER#user-123",
            "SK": "ARTIFACT#artifact-abc#V#00001",
            "storage": "s3",
        }
    )
    resp = handler.handler(_event(_mint(_valid_claims())), None)
    assert resp["statusCode"] == 404


def test_missing_s3_object_is_404(aws_env) -> None:
    _put_record(aws_env["ddb"], content_key="user-123/artifact-abc/v1/gone.html")
    resp = handler.handler(_event(_mint(_valid_claims())), None)
    assert resp["statusCode"] == 404


def test_version_pins_exact_sk(aws_env) -> None:
    # Token asks for v2; only v1 exists → must 404, never fall back to HEAD.
    _put_record(aws_env["ddb"])
    resp = handler.handler(_event(_mint(_valid_claims(ver=2))), None)
    assert resp["statusCode"] == 404


def test_oversized_content_is_500(aws_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "_MAX_CONTENT_BYTES", 16)
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket=BUCKET, Key=CONTENT_KEY, Body=b"x" * 64
    )
    _put_record(aws_env["ddb"])
    resp = handler.handler(_event(_mint(_valid_claims())), None)
    assert resp["statusCode"] == 500
