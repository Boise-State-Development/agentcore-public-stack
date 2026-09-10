"""Request-time byte-cap enforcement on the document upload-URL endpoint.

Feature: managed-kb-migration — enforce the managed-KB Byte_Cap at document
upload time (completes Requirement 12.11; the interactive upload path was the one
byte-adding path left uncapped).

The endpoint reserves the client-declared size against the binding cap BEFORE it
creates the DOC# row or issues a presigned URL, so an over-cap upload is refused
with a 413 carrying the numbers (Req 12.12) rather than after the bytes are
staged. The reservation is provisional — the authoritative gate is the S3-HEAD
reconcile at ingestion (Req 12.3) — but it gives fast, friendly feedback and is
released if a later step of the same request fails (Req 12.6). Legacy KBs are
never checked.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from apis.app_api.documents.routes import router
from apis.shared.auth import get_current_user_from_session
from apis.shared.auth.models import User

ROUTES_MODULE = "apis.app_api.documents.routes"
REGION = "us-east-1"
TABLE = "test-upload-byte-cap"
ASSISTANT_ID = "ast-cap01"
USER_ID = "user-001"


@pytest.fixture()
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", TABLE)

    with mock_aws():
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
        yield boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


@pytest.fixture()
def app():
    _app = FastAPI()
    _app.include_router(router)
    _app.dependency_overrides[get_current_user_from_session] = lambda: User(
        user_id=USER_ID, email=f"{USER_ID}@example.com", name="Test User", roles=["User"]
    )
    return _app


def _seed_managed_kb(table, **overrides):
    item = {
        "PK": f"AST#{ASSISTANT_ID}",
        "SK": f"KB#{ASSISTANT_ID}",
        "appKbId": ASSISTANT_ID,
        "ownerUserId": USER_ID,
        "retrievalEngine": "managed",
        "storedBytes": 0,
        "reservedBytes": 0,
        "totalBytes": 0,
    }
    item.update(overrides)
    table.put_item(Item=item)


def _kb(table):
    return table.get_item(
        Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"KB#{ASSISTANT_ID}"}
    ).get("Item")


def _post(app, size_bytes):
    return TestClient(app).post(
        f"/assistants/{ASSISTANT_ID}/documents/upload-url",
        json={"filename": "report.pdf", "contentType": "application/pdf", "sizeBytes": size_bytes},
    )


def _owner():
    return (SimpleNamespace(owner_id=USER_ID), "owner")


class TestManagedUploadIsCapped:
    def test_an_over_cap_upload_is_rejected_413_with_the_numbers(self, table, app, monkeypatch):
        """NAMED mutation guard: dropping the request-time reserve makes this pass
        as a 200. The cap is 5000 bytes and the file is 10000."""
        monkeypatch.setenv("MANAGED_KB_PER_OWNER_DEFAULT_BYTES", "5000")
        monkeypatch.setenv("MANAGED_KB_PER_KB_CEILING_BYTES", "5000")
        _seed_managed_kb(table)

        with patch(
            f"{ROUTES_MODULE}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=_owner(),
        ), patch(
            f"{ROUTES_MODULE}.create_document", new_callable=AsyncMock
        ) as create, patch(
            f"{ROUTES_MODULE}.generate_upload_url", new_callable=AsyncMock
        ) as gen:
            resp = _post(app, 10000)

        assert resp.status_code == 413
        detail = resp.json()["detail"]
        assert "10000" in detail and "5000" in detail  # requested and cap (Req 12.12)
        # The row was never created and no URL was issued.
        create.assert_not_awaited()
        gen.assert_not_awaited()
        # No bytes were reserved (the reserve raised before mutating on a fits check;
        # n > cap short-circuits without a write).
        assert int(_kb(table)["reservedBytes"]) == 0

    def test_an_under_cap_upload_succeeds_and_reserves_the_declared_size(self, table, app):
        _seed_managed_kb(table)

        with patch(
            f"{ROUTES_MODULE}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=_owner(),
        ), patch(
            f"{ROUTES_MODULE}.create_document", new_callable=AsyncMock
        ), patch(
            f"{ROUTES_MODULE}.generate_upload_url",
            new_callable=AsyncMock,
            return_value=("https://signed.example/put", None),
        ):
            resp = _post(app, 2048)

        assert resp.status_code == 200
        body = resp.json()
        assert body["uploadUrl"] == "https://signed.example/put"
        assert body["documentId"]
        assert int(_kb(table)["reservedBytes"]) == 2048  # provisional reservation

    def test_the_elevated_tier_is_read_from_the_record(self, table, app, monkeypatch):
        """A 10000-byte file that fails the 5000 default cap succeeds when the
        owner carries elevatedByteCap=true and the elevated cap is 100000."""
        monkeypatch.setenv("MANAGED_KB_PER_OWNER_DEFAULT_BYTES", "5000")
        monkeypatch.setenv("MANAGED_KB_PER_OWNER_ELEVATED_BYTES", "100000")
        _seed_managed_kb(table, elevatedByteCap=True)

        with patch(
            f"{ROUTES_MODULE}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=_owner(),
        ), patch(
            f"{ROUTES_MODULE}.create_document", new_callable=AsyncMock
        ), patch(
            f"{ROUTES_MODULE}.generate_upload_url",
            new_callable=AsyncMock,
            return_value=("https://signed.example/put", None),
        ):
            resp = _post(app, 10000)

        assert resp.status_code == 200
        assert int(_kb(table)["reservedBytes"]) == 10000

    def test_a_later_step_failing_releases_the_reservation(self, table, app):
        """Req 12.6: if URL generation fails after the reserve, the bytes are
        returned rather than leaked."""
        _seed_managed_kb(table)

        with patch(
            f"{ROUTES_MODULE}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=_owner(),
        ), patch(
            f"{ROUTES_MODULE}.create_document", new_callable=AsyncMock
        ), patch(
            f"{ROUTES_MODULE}.generate_upload_url",
            new_callable=AsyncMock,
            side_effect=RuntimeError("s3 signer down"),
        ):
            resp = _post(app, 2048)

        assert resp.status_code == 500
        assert int(_kb(table)["reservedBytes"]) == 0, "reservation leaked on a failed request"


class TestLegacyUploadIsNotCapped:
    def test_a_legacy_kb_is_never_reserved_against(self, table, app):
        """No KB_Record -> legacy -> uncapped. The upload succeeds and nothing is
        written to a KB accounting record."""
        # deliberately no _seed_managed_kb

        with patch(
            f"{ROUTES_MODULE}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=_owner(),
        ), patch(
            f"{ROUTES_MODULE}.create_document", new_callable=AsyncMock
        ), patch(
            f"{ROUTES_MODULE}.generate_upload_url",
            new_callable=AsyncMock,
            return_value=("https://signed.example/put", None),
        ):
            resp = _post(app, 10_000_000_000)  # 10 GB, would fail any managed cap

        assert resp.status_code == 200
        assert _kb(table) is None, "a legacy upload created a byte-cap record"
