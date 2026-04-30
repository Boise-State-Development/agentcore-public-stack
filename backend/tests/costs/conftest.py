"""Shared test fixtures for cost tracking and DynamoDB storage tests.

Sets up moto-backed DynamoDB tables matching production schema:
- SessionsMetadata (message-level cost records)
- UserCostSummary (pre-aggregated monthly summaries)
- SystemCostRollup (system-wide rollups and active user tracking)
"""

import os
import pytest
import boto3
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from moto import mock_aws


# ── Table names (match production env var defaults) ──────────────────────────

SESSIONS_TABLE = "SessionsMetadata"
COST_SUMMARY_TABLE = "UserCostSummary"
SYSTEM_ROLLUP_TABLE = "SystemCostRollup"


# ── DynamoDB table creation helpers ──────────────────────────────────────────

def _create_sessions_metadata_table(dynamodb):
    """Create SessionsMetadata table with GSIs."""
    dynamodb.create_table(
        TableName=SESSIONS_TABLE,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI_PK", "AttributeType": "S"},
            {"AttributeName": "GSI_SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "SessionLookupIndex",
                "KeySchema": [
                    {"AttributeName": "GSI_PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI_SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "UserTimestampIndex",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _create_cost_summary_table(dynamodb):
    """Create UserCostSummary table with PeriodCostIndex GSI."""
    dynamodb.create_table(
        TableName=COST_SUMMARY_TABLE,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI2PK", "AttributeType": "S"},
            {"AttributeName": "GSI2SK", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "PeriodCostIndex",
                "KeySchema": [
                    {"AttributeName": "GSI2PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI2SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _create_system_rollup_table(dynamodb):
    """Create SystemCostRollup table (no GSIs)."""
    dynamodb.create_table(
        TableName=SYSTEM_ROLLUP_TABLE,
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


# ── Core fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def aws_env(monkeypatch):
    """Set AWS env vars needed by DynamoDBStorage."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", SESSIONS_TABLE)
    monkeypatch.setenv("DYNAMODB_COST_SUMMARY_TABLE_NAME", COST_SUMMARY_TABLE)
    monkeypatch.setenv("DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME", SYSTEM_ROLLUP_TABLE)


@pytest.fixture
def moto_dynamodb(aws_env):
    """Provide moto-backed DynamoDB with all 3 tables created."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        _create_sessions_metadata_table(dynamodb)
        _create_cost_summary_table(dynamodb)
        _create_system_rollup_table(dynamodb)
        yield dynamodb


@pytest.fixture
def storage(moto_dynamodb):
    """Provide a DynamoDBStorage instance backed by moto tables."""
    from apis.shared.storage.dynamodb_storage import DynamoDBStorage
    return DynamoDBStorage()


@pytest.fixture
def mock_storage():
    """Provide a fully mocked DynamoDBStorage (all async methods mocked)."""
    mock = AsyncMock()
    mock.get_user_cost_summary = AsyncMock(return_value=None)
    mock.update_user_cost_summary = AsyncMock()
    mock.get_user_messages_in_range = AsyncMock(return_value=[])
    mock.get_top_users_by_cost = AsyncMock(return_value=[])
    mock.get_system_summary = AsyncMock(return_value=None)
    mock.get_daily_trends = AsyncMock(return_value=[])
    mock.get_model_usage = AsyncMock(return_value=[])
    mock.track_active_user = AsyncMock(return_value=(True, True))
    mock.track_active_user_for_model = AsyncMock(return_value=True)
    mock.store_message_metadata = AsyncMock()
    mock.get_session_metadata = AsyncMock(return_value=[])
    mock.get_message_metadata = AsyncMock(return_value=None)
    return mock


# ── Sample data factories ────────────────────────────────────────────────────

@pytest.fixture
def sample_metadata():
    """Return a factory for message metadata dicts."""
    def _make(
        cost=0.0105,
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=0,
        cache_write_tokens=0,
        model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        model_name="Claude Sonnet 4.5",
        provider="bedrock",
        timestamp=None,
        session_id="sess-001",
    ):
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        return {
            "cost": cost,
            "tokenUsage": {
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "cacheReadInputTokens": cache_read_tokens,
                "cacheWriteInputTokens": cache_write_tokens,
                "totalTokens": input_tokens + output_tokens + cache_read_tokens + cache_write_tokens,
            },
            "modelInfo": {
                "modelId": model_id,
                "modelName": model_name,
                "provider": provider,
                "pricingSnapshot": {
                    "inputPricePerMtok": 3.0,
                    "outputPricePerMtok": 15.0,
                    "cacheReadPricePerMtok": 0.30,
                    "cacheWritePricePerMtok": 3.75,
                    "currency": "USD",
                    "snapshotAt": ts,
                },
            },
            "attribution": {
                "userId": "test-user",
                "sessionId": session_id,
                "timestamp": ts,
            },
            "latency": {
                "timeToFirstToken": 250,
                "endToEndLatency": 1200,
            },
        }
    return _make


@pytest.fixture
def sample_usage_delta():
    """Return a sample usage_delta dict for update_user_cost_summary."""
    return {
        "inputTokens": 1000,
        "outputTokens": 500,
        "cacheReadInputTokens": 200,
        "cacheWriteInputTokens": 100,
    }


@pytest.fixture
def sample_cost_summary_item():
    """Return a factory for DynamoDB UserCostSummary items (as returned from storage)."""
    def _make(
        user_id="test-user",
        period="2025-01",
        total_cost=42.50,
        total_requests=156,
        input_tokens=125000,
        output_tokens=75000,
        cache_read_tokens=50000,
        cache_write_tokens=10000,
        cache_savings=2.50,
    ):
        return {
            "userId": user_id,
            "periodStart": f"{period}-01T00:00:00Z",
            "periodEnd": f"{period}-31T23:59:59Z",
            "totalCost": total_cost,
            "totalRequests": total_requests,
            "totalInputTokens": input_tokens,
            "totalOutputTokens": output_tokens,
            "totalCacheReadTokens": cache_read_tokens,
            "totalCacheWriteTokens": cache_write_tokens,
            "cacheSavings": cache_savings,
            "modelBreakdown": {
                "us_anthropic_claude_sonnet_4_5": {
                    "modelName": "Claude Sonnet 4.5",
                    "provider": "bedrock",
                    "cost": total_cost * 0.7,
                    "requests": int(total_requests * 0.7),
                    "inputTokens": int(input_tokens * 0.7),
                    "outputTokens": int(output_tokens * 0.7),
                    "cacheReadTokens": cache_read_tokens,
                    "cacheWriteTokens": cache_write_tokens,
                },
                "gpt_4o": {
                    "modelName": "GPT-4o",
                    "provider": "openai",
                    "cost": total_cost * 0.3,
                    "requests": total_requests - int(total_requests * 0.7),
                    "inputTokens": input_tokens - int(input_tokens * 0.7),
                    "outputTokens": output_tokens - int(output_tokens * 0.7),
                    "cacheReadTokens": 0,
                    "cacheWriteTokens": 0,
                },
            },
            "lastUpdated": datetime.now(timezone.utc).isoformat(),
        }
    return _make


@pytest.fixture
def sample_managed_models():
    """Return a list of mock ManagedModel objects for pricing tests."""
    bedrock_model = MagicMock()
    bedrock_model.model_id = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    bedrock_model.model_name = "Claude Sonnet 4.5"
    bedrock_model.provider = "bedrock"
    bedrock_model.input_price_per_million_tokens = 3.0
    bedrock_model.output_price_per_million_tokens = 15.0
    bedrock_model.cache_write_price_per_million_tokens = 3.75
    bedrock_model.cache_read_price_per_million_tokens = 0.30

    openai_model = MagicMock()
    openai_model.model_id = "gpt-4o"
    openai_model.model_name = "GPT-4o"
    openai_model.provider = "openai"
    openai_model.input_price_per_million_tokens = 5.0
    openai_model.output_price_per_million_tokens = 15.0
    openai_model.cache_write_price_per_million_tokens = None
    openai_model.cache_read_price_per_million_tokens = None

    gemini_model = MagicMock()
    gemini_model.model_id = "gemini-2.0-flash"
    gemini_model.model_name = "Gemini 2.0 Flash"
    gemini_model.provider = "gemini"
    gemini_model.input_price_per_million_tokens = 0.10
    gemini_model.output_price_per_million_tokens = 0.40
    gemini_model.cache_write_price_per_million_tokens = None
    gemini_model.cache_read_price_per_million_tokens = None

    return [bedrock_model, openai_model, gemini_model]
