"""Tests for the shared "token or consent URL?" AgentCore query.

The load-bearing property is the *vault-key agreement*: AgentCore folds
`scopes` and `customParameters` into the token-vault key, so this helper
must ask with exactly what the connector record says — the same values
`OAuthConsentHook` sends. Drift there looks up a different vault entry and
returns a consent URL for an already-authorized user, i.e. a "please
connect" prompt that reappears no matter how often they connect.

The other property is that a hard error is distinguishable from a consent
gap: callers prompt on a URL, never on None.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apis.shared.oauth.agentcore_identity import (
    CallbackUrlUnavailableError,
    WorkloadTokenUnavailableError,
)
from apis.shared.oauth.token_resolution import resolve_token_or_consent_url


def _provider(**overrides):
    base = dict(scopes=["repo", "read:user"], custom_parameters={"prompt": "consent"})
    base.update(overrides)
    return SimpleNamespace(**base)


def _patches(provider, identity_client):
    repo = SimpleNamespace(get_provider=AsyncMock(return_value=provider))
    return (
        patch(
            "apis.shared.oauth.provider_repository.get_provider_repository",
            return_value=repo,
        ),
        patch(
            "apis.shared.oauth.token_resolution.get_agentcore_identity_client",
            return_value=identity_client,
        ),
    )


class TestResolveTokenOrConsentUrl:
    @pytest.mark.asyncio
    async def test_returns_vaulted_token(self):
        identity = SimpleNamespace(
            get_token_for_user=AsyncMock(
                return_value=SimpleNamespace(
                    access_token="tok-1", authorization_url=None
                )
            )
        )
        p1, p2 = _patches(_provider(), identity)
        with p1, p2:
            result = await resolve_token_or_consent_url("github-oauth", "alice")

        assert result == {"token": "tok-1", "url": None}

    @pytest.mark.asyncio
    async def test_sends_provider_scopes_and_custom_parameters(self):
        """Vault-key agreement — see the module docstring."""
        identity = SimpleNamespace(
            get_token_for_user=AsyncMock(
                return_value=SimpleNamespace(
                    access_token="tok-1", authorization_url=None
                )
            )
        )
        p1, p2 = _patches(_provider(), identity)
        with p1, p2:
            await resolve_token_or_consent_url("github-oauth", "alice")

        kwargs = identity.get_token_for_user.await_args.kwargs
        assert kwargs["provider_name"] == "github-oauth"
        assert kwargs["user_id"] == "alice"
        assert kwargs["scopes"] == ["repo", "read:user"]
        assert kwargs["custom_parameters"] == {"prompt": "consent"}
        assert kwargs["force_authentication"] is False

    @pytest.mark.asyncio
    async def test_returns_consent_url_when_not_authorized(self):
        identity = SimpleNamespace(
            get_token_for_user=AsyncMock(
                return_value=SimpleNamespace(
                    access_token=None,
                    authorization_url="https://consent.example/authorize",
                )
            )
        )
        p1, p2 = _patches(_provider(), identity)
        with p1, p2:
            result = await resolve_token_or_consent_url("github-oauth", "alice")

        assert result == {
            "token": None,
            "url": "https://consent.example/authorize",
        }

    @pytest.mark.asyncio
    async def test_forwards_force_authentication(self):
        identity = SimpleNamespace(
            get_token_for_user=AsyncMock(
                return_value=SimpleNamespace(access_token="t", authorization_url=None)
            )
        )
        p1, p2 = _patches(_provider(), identity)
        with p1, p2:
            await resolve_token_or_consent_url(
                "github-oauth", "alice", force_authentication=True
            )

        assert identity.get_token_for_user.await_args.kwargs[
            "force_authentication"
        ] is True

    @pytest.mark.asyncio
    async def test_missing_provider_record_is_not_a_consent_gap(self):
        """A deleted connector must not produce a Connect prompt."""
        identity = SimpleNamespace(get_token_for_user=AsyncMock())
        p1, p2 = _patches(None, identity)
        with p1, p2:
            result = await resolve_token_or_consent_url("ghost", "alice")

        assert result is None
        identity.get_token_for_user.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [
            WorkloadTokenUnavailableError("no workload token"),
            CallbackUrlUnavailableError("no callback url"),
            RuntimeError("boom"),
        ],
    )
    async def test_hard_errors_return_none_not_a_url(self, exc):
        identity = SimpleNamespace(get_token_for_user=AsyncMock(side_effect=exc))
        p1, p2 = _patches(_provider(), identity)
        with p1, p2:
            result = await resolve_token_or_consent_url("github-oauth", "alice")

        # None means "couldn't ask". Callers must stay silent rather than
        # prompting a user who may well be connected.
        assert result is None

    @pytest.mark.asyncio
    async def test_provider_repository_failure_returns_none(self):
        repo = SimpleNamespace(get_provider=AsyncMock(side_effect=RuntimeError("ddb")))
        identity = SimpleNamespace(get_token_for_user=AsyncMock())
        with patch(
            "apis.shared.oauth.provider_repository.get_provider_repository",
            return_value=repo,
        ), patch(
            "apis.shared.oauth.token_resolution.get_agentcore_identity_client",
            return_value=identity,
        ):
            result = await resolve_token_or_consent_url("github-oauth", "alice")

        assert result is None
        identity.get_token_for_user.assert_not_awaited()
