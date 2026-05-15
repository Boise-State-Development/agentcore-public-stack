"""OAuth disconnect repository tests (moto DynamoDB).

Co-tenants the existing `oauth-user-tokens` table — items use a
`DISCONNECT#{provider_id}` sort-key prefix so they cannot collide with
future per-user token storage.
"""

import boto3
import pytest

from apis.shared.oauth.disconnect_repository import OAuthDisconnectRepository


@pytest.fixture()
def disconnect_repository(oauth_tokens_table, monkeypatch):
    # Earlier tests in the suite can leave boto3's default session bound
    # to real-world SSO credentials. moto only mocks API calls, not the
    # credential resolution chain, so a stale SSO session would later try
    # to refresh against AWS and fail (`GetRoleCredentials: Not yet
    # implemented`). Resetting the default session forces boto3 to
    # rebuild it under the conftest's `AWS_ACCESS_KEY_ID=testing` env
    # vars on first use.
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    return OAuthDisconnectRepository(
        table_name="test-oauth-user-tokens",
        region="us-east-1",
    )


class TestOAuthDisconnectRepository:
    @pytest.mark.asyncio
    async def test_default_state_is_not_disconnected(self, disconnect_repository):
        assert await disconnect_repository.is_disconnected("alice", "google") is False

    @pytest.mark.asyncio
    async def test_mark_then_check(self, disconnect_repository):
        await disconnect_repository.mark_disconnected("alice", "google")
        assert await disconnect_repository.is_disconnected("alice", "google") is True

    @pytest.mark.asyncio
    async def test_per_user_isolation(self, disconnect_repository):
        await disconnect_repository.mark_disconnected("alice", "google")
        assert await disconnect_repository.is_disconnected("bob", "google") is False

    @pytest.mark.asyncio
    async def test_per_provider_isolation(self, disconnect_repository):
        await disconnect_repository.mark_disconnected("alice", "google")
        assert await disconnect_repository.is_disconnected("alice", "github") is False

    @pytest.mark.asyncio
    async def test_clear_disconnected(self, disconnect_repository):
        await disconnect_repository.mark_disconnected("alice", "google")
        await disconnect_repository.clear_disconnected("alice", "google")
        assert await disconnect_repository.is_disconnected("alice", "google") is False

    @pytest.mark.asyncio
    async def test_clear_when_not_set_is_noop(self, disconnect_repository):
        # Idempotent: clearing a flag that was never set should not raise.
        await disconnect_repository.clear_disconnected("alice", "google")
        assert await disconnect_repository.is_disconnected("alice", "google") is False

    @pytest.mark.asyncio
    async def test_mark_is_idempotent(self, disconnect_repository):
        await disconnect_repository.mark_disconnected("alice", "google")
        await disconnect_repository.mark_disconnected("alice", "google")
        assert await disconnect_repository.is_disconnected("alice", "google") is True

    @pytest.mark.asyncio
    async def test_disabled_when_table_env_unset(self, monkeypatch):
        # Without the env var set, the repo silently no-ops so local-dev
        # without OAuth wiring still boots and `/status` falls through to
        # AgentCore's vault state.
        monkeypatch.delenv("DYNAMODB_OAUTH_USER_TOKENS_TABLE_NAME", raising=False)
        repo = OAuthDisconnectRepository()
        assert repo.enabled is False
        assert await repo.is_disconnected("alice", "google") is False
        await repo.mark_disconnected("alice", "google")  # no-op, no raise
        assert await repo.is_disconnected("alice", "google") is False
