"""Tests for DynamoDB message metadata operations.

Covers: store_message_metadata, get_message_metadata, get_session_metadata,
        get_user_messages_in_range, _convert_floats_to_decimal, _convert_decimal_to_float.
"""

import time
import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal


# ── _convert_floats_to_decimal ───────────────────────────────────────────────

class TestConvertFloatsToDecimal:
    """Unit tests for _convert_floats_to_decimal (lines 77-90)."""

    def test_float_becomes_decimal(self, storage):
        result = storage._convert_floats_to_decimal(3.14)
        assert result == Decimal("3.14")
        assert isinstance(result, Decimal)

    def test_nested_dict(self, storage):
        result = storage._convert_floats_to_decimal({"a": 1.5, "b": {"c": 2.5}})
        assert result == {"a": Decimal("1.5"), "b": {"c": Decimal("2.5")}}

    def test_list_of_floats(self, storage):
        result = storage._convert_floats_to_decimal([1.1, 2.2, 3.3])
        assert result == [Decimal("1.1"), Decimal("2.2"), Decimal("3.3")]

    def test_none_passthrough(self, storage):
        assert storage._convert_floats_to_decimal(None) is None

    def test_int_passthrough(self, storage):
        assert storage._convert_floats_to_decimal(42) == 42
        assert isinstance(storage._convert_floats_to_decimal(42), int)

    def test_string_passthrough(self, storage):
        assert storage._convert_floats_to_decimal("hello") == "hello"

    def test_already_decimal_passthrough(self, storage):
        val = Decimal("9.99")
        assert storage._convert_floats_to_decimal(val) is val


# ── _convert_decimal_to_float ────────────────────────────────────────────────

class TestConvertDecimalToFloat:
    """Unit tests for _convert_decimal_to_float (lines 92-105)."""

    def test_decimal_becomes_float(self, storage):
        result = storage._convert_decimal_to_float(Decimal("3.14"))
        assert result == pytest.approx(3.14)
        assert isinstance(result, float)

    def test_nested_dict(self, storage):
        result = storage._convert_decimal_to_float({"a": Decimal("1.5"), "b": {"c": Decimal("2.5")}})
        assert result == {"a": pytest.approx(1.5), "b": {"c": pytest.approx(2.5)}}

    def test_list_of_decimals(self, storage):
        result = storage._convert_decimal_to_float([Decimal("1.1"), Decimal("2.2")])
        assert result[0] == pytest.approx(1.1)
        assert result[1] == pytest.approx(2.2)

    def test_none_passthrough(self, storage):
        assert storage._convert_decimal_to_float(None) is None

    def test_int_passthrough(self, storage):
        assert storage._convert_decimal_to_float(42) == 42
        assert isinstance(storage._convert_decimal_to_float(42), int)


# ── store_message_metadata ───────────────────────────────────────────────────

class TestStoreMessageMetadata:
    """Tests for store_message_metadata (lines 107-166)."""

    @pytest.mark.asyncio
    async def test_writes_correct_pk_sk(self, storage, moto_dynamodb, sample_metadata):
        meta = sample_metadata()
        await storage.store_message_metadata("user-1", "sess-1", 10, meta)

        table = moto_dynamodb.Table("SessionsMetadata")
        resp = table.scan()
        items = resp["Items"]
        assert len(items) == 1

        item = items[0]
        assert item["PK"] == "USER#user-1"
        assert item["SK"].startswith("C#")
        assert item["GSI_PK"] == "SESSION#sess-1"
        assert item["GSI_SK"].startswith("C#")

    @pytest.mark.asyncio
    async def test_names_the_build_that_wrote_it(self, storage, moto_dynamodb, sample_metadata, monkeypatch):
        """release-ledger spec D1, on the second cost-row writer too."""
        import apis.shared.storage.dynamodb_storage as module

        monkeypatch.setattr(module, "build_stamp", lambda: {"appVersion": "1.33.0", "build": "8f676bb2e1a4c0d2"})
        await storage.store_message_metadata("user-1", "sess-1", 10, sample_metadata())
        (item,) = moto_dynamodb.Table("SessionsMetadata").scan()["Items"]
        assert item["appVersion"] == "1.33.0"
        assert item["build"] == "8f676bb2e1a4c0d2"

    @pytest.mark.asyncio
    async def test_sets_ttl_approximately_365_days(self, storage, moto_dynamodb, sample_metadata):
        meta = sample_metadata()
        await storage.store_message_metadata("user-1", "sess-1", 1, meta)

        table = moto_dynamodb.Table("SessionsMetadata")
        item = table.scan()["Items"][0]

        expected_ttl = int((datetime.now(timezone.utc) + timedelta(days=365)).timestamp())
        assert abs(item["ttl"] - expected_ttl) < 5  # within 5 seconds

    @pytest.mark.asyncio
    async def test_converts_floats_to_decimal(self, storage, moto_dynamodb, sample_metadata):
        meta = sample_metadata(cost=0.0105)
        await storage.store_message_metadata("user-1", "sess-1", 1, meta)

        table = moto_dynamodb.Table("SessionsMetadata")
        item = table.scan()["Items"][0]
        assert isinstance(item["cost"], Decimal)
        assert item["cost"] == Decimal("0.0105")

    @pytest.mark.asyncio
    async def test_stores_all_metadata_fields(self, storage, moto_dynamodb, sample_metadata):
        meta = sample_metadata(input_tokens=2000, output_tokens=800)
        await storage.store_message_metadata("user-1", "sess-1", 5, meta)

        table = moto_dynamodb.Table("SessionsMetadata")
        item = table.scan()["Items"][0]

        assert item["userId"] == "user-1"
        assert item["sessionId"] == "sess-1"
        assert item["messageId"] == 5
        assert item["tokenUsage"]["inputTokens"] == 2000
        assert item["tokenUsage"]["outputTokens"] == 800
        assert item["modelInfo"]["modelName"] == "Claude Sonnet 4.5"

    @pytest.mark.asyncio
    async def test_raises_on_client_error(self, storage):
        from unittest.mock import patch, MagicMock
        from botocore.exceptions import ClientError

        err = ClientError({"Error": {"Code": "500", "Message": "boom"}}, "PutItem")
        with patch.object(storage.sessions_metadata_table, "put_item", side_effect=err):
            with pytest.raises(Exception, match="Failed to store message metadata"):
                await storage.store_message_metadata("u", "s", 1, {"cost": 0.01})


# ── get_message_metadata ─────────────────────────────────────────────────────

class TestGetMessageMetadata:
    """Tests for get_message_metadata (lines 168-213)."""

    @pytest.mark.asyncio
    async def test_returns_stored_record(self, storage, sample_metadata):
        meta = sample_metadata(cost=0.05)
        await storage.store_message_metadata("user-1", "sess-1", 42, meta)

        result = await storage.get_message_metadata("user-1", "sess-1", 42)
        assert result is not None
        assert result["messageId"] == 42
        assert result["cost"] == pytest.approx(0.05)

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self, storage):
        result = await storage.get_message_metadata("user-1", "sess-1", 999)
        assert result is None

    @pytest.mark.asyncio
    async def test_filters_by_message_id(self, storage, sample_metadata):
        await storage.store_message_metadata("user-1", "sess-1", 1, sample_metadata())
        await storage.store_message_metadata("user-1", "sess-1", 2, sample_metadata())

        result = await storage.get_message_metadata("user-1", "sess-1", 2)
        assert result is not None
        assert result["messageId"] == 2

    @pytest.mark.asyncio
    async def test_removes_dynamodb_keys(self, storage, sample_metadata):
        await storage.store_message_metadata("user-1", "sess-1", 1, sample_metadata())
        result = await storage.get_message_metadata("user-1", "sess-1", 1)

        for key in ("PK", "SK", "GSI_PK", "GSI_SK", "ttl"):
            assert key not in result

    @pytest.mark.asyncio
    async def test_converts_decimal_to_float(self, storage, sample_metadata):
        await storage.store_message_metadata("user-1", "sess-1", 1, sample_metadata(cost=0.123))
        result = await storage.get_message_metadata("user-1", "sess-1", 1)

        assert isinstance(result["cost"], float)
        assert result["cost"] == pytest.approx(0.123)


# ── get_session_metadata ─────────────────────────────────────────────────────

class TestGetSessionMetadata:
    """Tests for get_session_metadata (lines 215-253)."""

    @pytest.mark.asyncio
    async def test_returns_all_messages_for_session(self, storage, sample_metadata):
        for mid in (1, 2, 3):
            await storage.store_message_metadata("user-1", "sess-1", mid, sample_metadata())

        results = await storage.get_session_metadata("user-1", "sess-1")
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_returns_empty_list_for_no_messages(self, storage):
        results = await storage.get_session_metadata("user-1", "sess-empty")
        assert results == []

    @pytest.mark.asyncio
    async def test_filters_by_user_id(self, storage, sample_metadata):
        await storage.store_message_metadata("user-A", "sess-shared", 1, sample_metadata())
        await storage.store_message_metadata("user-B", "sess-shared", 2, sample_metadata())

        results = await storage.get_session_metadata("user-A", "sess-shared")
        assert len(results) == 1
        assert results[0]["userId"] == "user-A"

    @pytest.mark.asyncio
    async def test_removes_dynamodb_keys(self, storage, sample_metadata):
        await storage.store_message_metadata("user-1", "sess-1", 1, sample_metadata())
        results = await storage.get_session_metadata("user-1", "sess-1")

        for item in results:
            for key in ("PK", "SK", "GSI_PK", "GSI_SK", "ttl"):
                assert key not in item


# ── get_user_messages_in_range ───────────────────────────────────────────────

class TestGetUserMessagesInRange:
    """Tests for get_user_messages_in_range (lines 529-604).

    store_message_metadata does NOT set GSI1PK/GSI1SK, so we insert
    items directly into the moto table with those attributes.
    """

    def _insert_range_item(self, moto_dynamodb, user_id, timestamp_iso, session_id="sess-1",
                           message_id=1, cost=0.01, input_tokens=100, output_tokens=50):
        """Helper: put an item with GSI1PK/GSI1SK for range queries."""
        import uuid
        table = moto_dynamodb.Table("SessionsMetadata")
        table.put_item(Item={
            "PK": f"USER#{user_id}",
            "SK": f"C#{timestamp_iso}#{uuid.uuid4()}",
            "GSI_PK": f"SESSION#{session_id}",
            "GSI_SK": f"C#{timestamp_iso}",
            "GSI1PK": f"USER#{user_id}",
            "GSI1SK": timestamp_iso,
            "userId": user_id,
            "sessionId": session_id,
            "messageId": message_id,
            "timestamp": timestamp_iso,
            "cost": Decimal(str(cost)),
            "tokenUsage": {
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "cacheReadInputTokens": 0,
                "cacheWriteInputTokens": 0,
                "totalTokens": input_tokens + output_tokens,
            },
            "modelInfo": {
                "modelId": "claude-sonnet",
                "modelName": "Claude Sonnet",
                "provider": "bedrock",
                "pricingSnapshot": {"inputPricePerMtok": Decimal("3.0")},
            },
        })

    @pytest.mark.asyncio
    async def test_returns_messages_in_date_range(self, storage, moto_dynamodb):
        self._insert_range_item(moto_dynamodb, "user-1", "2025-01-15T10:00:00+00:00", message_id=1)
        self._insert_range_item(moto_dynamodb, "user-1", "2025-01-20T10:00:00+00:00", message_id=2)
        self._insert_range_item(moto_dynamodb, "user-1", "2025-02-10T10:00:00+00:00", message_id=3)

        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 1, 31, 23, 59, 59, tzinfo=timezone.utc)
        results = await storage.get_user_messages_in_range("user-1", start, end)

        assert len(results) == 2
        msg_ids = {r["messageId"] for r in results}
        assert msg_ids == {1, 2}

    @pytest.mark.asyncio
    async def test_empty_range_returns_empty_list(self, storage, moto_dynamodb):
        self._insert_range_item(moto_dynamodb, "user-1", "2025-03-01T10:00:00+00:00")

        start = datetime(2025, 6, 1, tzinfo=timezone.utc)
        end = datetime(2025, 6, 30, tzinfo=timezone.utc)
        results = await storage.get_user_messages_in_range("user-1", start, end)
        assert results == []

    @pytest.mark.asyncio
    async def test_flattens_nested_structures(self, storage, moto_dynamodb):
        self._insert_range_item(
            moto_dynamodb, "user-1", "2025-01-15T10:00:00+00:00",
            cost=0.05, input_tokens=500, output_tokens=200,
            session_id="sess-x", message_id=7,
        )

        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 1, 31, tzinfo=timezone.utc)
        results = await storage.get_user_messages_in_range("user-1", start, end)

        assert len(results) == 1
        r = results[0]
        assert r["cost"] == pytest.approx(0.05)
        assert r["inputTokens"] == 500
        assert r["outputTokens"] == 200
        assert r["cacheReadTokens"] == 0
        assert r["cacheWriteTokens"] == 0
        assert r["modelId"] == "claude-sonnet"
        assert r["modelName"] == "Claude Sonnet"
        assert r["provider"] == "bedrock"
        assert r["sessionId"] == "sess-x"
        assert r["messageId"] == 7
        assert "pricingSnapshot" in r

    @pytest.mark.asyncio
    async def test_does_not_include_other_users(self, storage, moto_dynamodb):
        self._insert_range_item(moto_dynamodb, "user-1", "2025-01-15T10:00:00+00:00")
        self._insert_range_item(moto_dynamodb, "user-2", "2025-01-15T10:00:00+00:00")

        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 1, 31, tzinfo=timezone.utc)
        results = await storage.get_user_messages_in_range("user-1", start, end)
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_flattened_output_has_no_dynamodb_keys(self, storage, moto_dynamodb):
        self._insert_range_item(moto_dynamodb, "user-1", "2025-01-10T00:00:00+00:00")

        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 1, 31, tzinfo=timezone.utc)
        results = await storage.get_user_messages_in_range("user-1", start, end)

        for key in ("PK", "SK", "GSI1PK", "GSI1SK", "GSI_PK", "GSI_SK", "ttl"):
            assert key not in results[0]
