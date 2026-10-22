"""Deleting a document mid-upload must not strand its byte reservation.

Feature: managed-kb-migration Requirement 12.6 (release on every abandon path).

Found by deleting an `uploading` document in dev. Three defects surfaced; this file
covers the byte-cap one, which is the invisible and cumulative one.

The request-time reservation is taken when the upload URL is issued, and it is
released by whichever terminal path the document reaches: a client-reported upload
failure, the stale sweep, or the ingestion consumer's own terminal paths. **A deleted
document reaches none of them.** Its row goes to `deleting` and nothing settles the
bytes.

Left unfixed the leak has the worst possible shape: silent, cumulative, and delayed.
Every cancelled upload permanently shaves bytes off that assistant's allowance, and it
presents months later as "uploads stopped working" with no failure anywhere near the
deletes that caused it — precisely what `byte_cap.release`'s own docstring warns about.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from moto import mock_aws

from apis.app_api.documents.services.document_service import soft_delete_document

REGION = "us-east-1"
TABLE = "test-delete-releases-bytes"
ASSISTANT_ID = "ast-del01"
DOCUMENT_ID = "DOC-del01"
OWNER = "user-del01"
SIZE = 4096


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
def owned(table):
    """`soft_delete_document` verifies ownership via `assistants.service.get_assistant`.

    Patched rather than seeded: that lookup reads the assistant METADATA row and its
    own permission model, none of which this suite is about. Stubbing it keeps the
    test aimed at the byte accounting.
    """
    with patch(
        "apis.shared.assistants.service.get_assistant",
        new_callable=AsyncMock,
        return_value=SimpleNamespace(assistant_id=ASSISTANT_ID, owner_id=OWNER),
    ):
        yield table


def _seed(table, *, engine="managed", status="uploading", size=SIZE, reserved=SIZE):
    """A managed KB holding a reservation, plus the uploading document that took it."""
    item = {
        "PK": f"AST#{ASSISTANT_ID}",
        "SK": f"KB#{ASSISTANT_ID}",
        "appKbId": ASSISTANT_ID,
        "ownerUserId": OWNER,
        "storedBytes": 0,
        "reservedBytes": reserved,
        "totalBytes": reserved,
    }
    if engine == "managed":
        item["retrievalEngine"] = "managed"
    table.put_item(Item=item)
    table.put_item(
        Item={
            "PK": f"AST#{ASSISTANT_ID}",
            "SK": f"DOC#{DOCUMENT_ID}",
            "documentId": DOCUMENT_ID,
            "assistantId": ASSISTANT_ID,
            "filename": "half-uploaded.pdf",
            "contentType": "application/pdf",
            "sizeBytes": size,
            "s3Key": f"assistants/{ASSISTANT_ID}/documents/{DOCUMENT_ID}/half-uploaded.pdf",
            "status": status,
            "createdAt": "2026-09-11T00:00:00Z",
            "updatedAt": "2026-09-11T00:00:00Z",
        }
    )


def _kb(table):
    return table.get_item(
        Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"KB#{ASSISTANT_ID}"}
    )["Item"]


def _doc(table):
    return table.get_item(
        Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"DOC#{DOCUMENT_ID}"}
    )["Item"]


class TestDeleteReleasesTheReservation:
    @pytest.mark.asyncio
    async def test_deleting_an_uploading_document_returns_its_bytes(self, owned, table):
        """MUTATION GUARD: remove the `release_reservation_if_managed` call from
        `soft_delete_document` and this fails with reservedBytes still at 4096 — the
        allowance permanently smaller for a document that never stored a byte."""
        _seed(table)

        document = await soft_delete_document(ASSISTANT_ID, DOCUMENT_ID, OWNER)

        assert document is not None
        assert _doc(table)["status"] == "deleting"
        assert int(_kb(table)["reservedBytes"]) == 0
        assert int(_kb(table)["totalBytes"]) == 0

    @pytest.mark.asyncio
    async def test_the_release_is_exactly_once(self, owned, table):
        """A second settlement would over-credit the allowance, letting an owner
        exceed their cap by deleting repeatedly. `settle_once` stamps the DOC# row, so
        a re-delete (idempotent by design) releases nothing further."""
        _seed(table)

        await soft_delete_document(ASSISTANT_ID, DOCUMENT_ID, OWNER)
        await soft_delete_document(ASSISTANT_ID, DOCUMENT_ID, OWNER)

        assert int(_kb(table)["reservedBytes"]) == 0
        assert int(_kb(table)["totalBytes"]) == 0
        assert _doc(table).get("byteCapSettled")

    @pytest.mark.asyncio
    async def test_a_legacy_knowledge_base_is_untouched(self, owned, table):
        """Legacy KBs are uncapped, so they hold no reservation to return. Touching
        their counters here would invent accounting for a backend that has none."""
        _seed(table, engine="s3vectors")

        await soft_delete_document(ASSISTANT_ID, DOCUMENT_ID, OWNER)

        assert int(_kb(table)["reservedBytes"]) == SIZE
        assert not _doc(table).get("byteCapSettled")

    @pytest.mark.asyncio
    async def test_a_completed_document_already_settled_is_not_double_credited(self, owned, table):
        """The ordinary case: ingestion finished, so the reservation was already
        committed to storedBytes. Deleting must not now ALSO release it — that would
        drive the counters negative. The stamp from the earlier settlement is what
        stops it."""
        _seed(table, status="complete", reserved=0)
        table.update_item(
            Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"DOC#{DOCUMENT_ID}"},
            UpdateExpression="SET byteCapSettled = :t",
            ExpressionAttributeValues={":t": True},
        )
        table.update_item(
            Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"KB#{ASSISTANT_ID}"},
            UpdateExpression="SET storedBytes = :s, totalBytes = :s",
            ExpressionAttributeValues={":s": SIZE},
        )

        await soft_delete_document(ASSISTANT_ID, DOCUMENT_ID, OWNER)

        kb = _kb(table)
        assert int(kb["reservedBytes"]) == 0
        assert int(kb["totalBytes"]) == SIZE
        assert int(kb["storedBytes"]) == SIZE

    @pytest.mark.asyncio
    async def test_a_zero_size_row_is_a_no_op(self, owned, table):
        """Imported documents are created with sizeBytes=0 — nothing was reserved at
        request time, so there is nothing to give back."""
        _seed(table, size=0, reserved=0)

        await soft_delete_document(ASSISTANT_ID, DOCUMENT_ID, OWNER)

        assert int(_kb(table)["reservedBytes"]) == 0
        assert not _doc(table).get("byteCapSettled")

    @pytest.mark.asyncio
    async def test_a_missing_document_is_none_and_changes_nothing(self, owned, table):
        _seed(table)

        result = await soft_delete_document(ASSISTANT_ID, "DOC-does-not-exist", OWNER)

        assert result is None
        assert int(_kb(table)["reservedBytes"]) == SIZE
