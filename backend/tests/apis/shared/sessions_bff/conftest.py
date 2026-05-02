"""Shared fixtures for sessions_bff tests.

Provides a moto-backed BFF sessions table that matches the schema provisioned
by Phase 1 CDK (`PK`, `SK`, `ttl` enabled), plus a ready-to-use repository
and a sample SessionRecord factory.
"""

from __future__ import annotations

import time

import boto3
import pytest
from moto import mock_aws

from apis.shared.sessions_bff.models import SessionRecord
from apis.shared.sessions_bff.repository import SessionRepository

BFF_SESSIONS_TABLE = "test-bff-sessions"


def _create_bff_sessions_table(dynamodb) -> None:
    dynamodb.create_table(
        TableName=BFF_SESSIONS_TABLE,
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


@pytest.fixture
def bff_aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("BFF_SESSIONS_TABLE_NAME", BFF_SESSIONS_TABLE)


@pytest.fixture
def moto_bff_dynamodb(bff_aws_env):
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        _create_bff_sessions_table(dynamodb)
        yield dynamodb


@pytest.fixture
def repository(moto_bff_dynamodb) -> SessionRepository:
    return SessionRepository(table_name=BFF_SESSIONS_TABLE)


@pytest.fixture
def sample_record():
    """Factory for building SessionRecord instances with sensible defaults."""

    def _make(
        *,
        session_id: str = "sess-001",
        user_id: str = "user-sub-001",
        username: str = "alice",
        access_token: str = "access.token.value",
        refresh_token: str = "refresh.token.value",
        id_token: str = "id.token.value",
        access_token_exp: int | None = None,
        ttl: int | None = None,
    ) -> SessionRecord:
        now = int(time.time())
        return SessionRecord(
            session_id=session_id,
            user_id=user_id,
            username=username,
            cognito_access_token=access_token,
            cognito_refresh_token=refresh_token,
            id_token=id_token,
            access_token_exp=access_token_exp if access_token_exp is not None else now + 3600,
            csrf_secret="csrf-secret-deadbeef",
            created_at=now,
            last_seen_at=now,
            ttl=ttl if ttl is not None else now + 28800,
        )

    return _make
