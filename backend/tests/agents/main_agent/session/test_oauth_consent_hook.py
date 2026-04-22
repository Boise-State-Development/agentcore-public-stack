"""Tests for OAuthConsentHook.

Covers the lazy token-resolution path: hook fires before each tool call,
asks AgentCore Identity for the user's token, caches it on a hit, and
raises a Strands interrupt with the consent URL on a miss. Resume is
exercised by pre-seeding the interrupt with a response so the second
`event.interrupt(...)` returns instead of raising.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from strands.interrupt import Interrupt, InterruptException

from agents.main_agent.integrations import oauth_token_cache
from agents.main_agent.integrations.agentcore_identity import (
    TokenResult,
    WorkloadTokenUnavailableError,
)
from agents.main_agent.session.hooks.oauth_consent import OAuthConsentHook


@pytest.fixture(autouse=True)
def _clear_cache():
    """Token cache is process-global; isolate between tests."""
    oauth_token_cache.clear_user("alice")
    yield
    oauth_token_cache.clear_user("alice")


def _make_event(provider_id: str | None, *, agent=None) -> MagicMock:
    """Build a stand-in for `BeforeToolCallEvent`.

    The hook reads `event.selected_tool` (passed straight to
    `provider_lookup`) and calls `event.interrupt(...)`. We forward
    `interrupt` to a real `_Interruptible.interrupt` style implementation
    so the test exercises the same raise/return semantics as the SDK.
    """
    event = MagicMock()
    event.selected_tool = MagicMock()
    event.cancel_tool = None

    agent = agent or MagicMock()
    agent._interrupt_state = MagicMock()
    agent._interrupt_state.interrupts = {}

    def interrupt(name: str, reason=None, response=None):
        # Mirror the SDK: deterministic id keyed on the name so the second
        # call returns the response instead of raising.
        interrupt_id = f"v1:before_tool_call:tu_test:{name}"
        existing = agent._interrupt_state.interrupts.setdefault(
            interrupt_id, Interrupt(interrupt_id, name, reason, response)
        )
        if existing.response is not None:
            return existing.response
        raise InterruptException(existing)

    event.interrupt = interrupt
    event._agent = agent  # for tests that want to inspect interrupt state
    return event


class TestOAuthConsentHookCacheHit:
    @pytest.mark.asyncio
    async def test_no_op_when_tool_not_oauth_gated(self):
        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: None,
            scopes_lookup=lambda _: [],
        )
        event = _make_event(provider_id=None)

        await hook._gate(event)

        assert event.cancel_tool is None

    @pytest.mark.asyncio
    async def test_uses_cached_token_without_calling_identity(self):
        oauth_token_cache.set("alice", "google", "cached-token")

        identity = MagicMock()
        identity.get_token_for_user = AsyncMock()

        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: "google",
            scopes_lookup=lambda _: ["openid"],
        )
        event = _make_event(provider_id="google")

        with patch(
            "agents.main_agent.session.hooks.oauth_consent.get_agentcore_identity_client",
            return_value=identity,
        ):
            await hook._gate(event)

        identity.get_token_for_user.assert_not_called()
        assert event.cancel_tool is None


class TestOAuthConsentHookVaultHit:
    @pytest.mark.asyncio
    async def test_warms_cache_when_vault_returns_token(self):
        identity = MagicMock()
        identity.get_token_for_user = AsyncMock(
            return_value=TokenResult(access_token="tok-from-vault")
        )

        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: "google",
            scopes_lookup=lambda _: ["openid"],
        )
        event = _make_event(provider_id="google")

        with patch(
            "agents.main_agent.session.hooks.oauth_consent.get_agentcore_identity_client",
            return_value=identity,
        ):
            await hook._gate(event)

        assert oauth_token_cache.get("alice", "google") == "tok-from-vault"
        identity.get_token_for_user.assert_called_once_with(
            provider_name="google",
            scopes=["openid"],
            user_id="alice",
            force_authentication=False,
        )


class TestOAuthConsentHookConsentRequired:
    @pytest.mark.asyncio
    async def test_raises_interrupt_with_oauth_required_reason(self):
        identity = MagicMock()
        identity.get_token_for_user = AsyncMock(
            return_value=TokenResult(authorization_url="https://accounts/consent")
        )

        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: "google",
            scopes_lookup=lambda _: ["openid"],
        )
        event = _make_event(provider_id="google")

        with patch(
            "agents.main_agent.session.hooks.oauth_consent.get_agentcore_identity_client",
            return_value=identity,
        ):
            with pytest.raises(InterruptException) as excinfo:
                await hook._gate(event)

        interrupt = excinfo.value.interrupt
        assert interrupt.name == "oauth:google"
        assert interrupt.reason == {
            "type": "oauth_required",
            "providerId": "google",
            "authorizationUrl": "https://accounts/consent",
        }
        # Cache stays empty until consent actually completes.
        assert oauth_token_cache.get("alice", "google") is None

    @pytest.mark.asyncio
    async def test_resume_warms_cache_with_post_consent_token(self):
        """On resume the SDK pre-populates the interrupt's response so
        `event.interrupt(...)` returns. The hook then re-fetches from the
        vault (which now has a token) and primes the cache so subsequent
        MCP requests pick up the bearer token without another round trip."""
        identity = MagicMock()
        # First call: consent required. Second call (post-consent): token.
        identity.get_token_for_user = AsyncMock(
            side_effect=[
                TokenResult(authorization_url="https://accounts/consent"),
                TokenResult(access_token="post-consent-token"),
            ]
        )

        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: "google",
            scopes_lookup=lambda _: ["openid"],
        )
        event = _make_event(provider_id="google")

        # Pre-seed the interrupt with a response — simulates the SDK
        # restoring `_interrupt_state` before re-running the hook on resume.
        agent = event._agent
        interrupt_id = "v1:before_tool_call:tu_test:oauth:google"
        agent._interrupt_state.interrupts[interrupt_id] = Interrupt(
            interrupt_id, "oauth:google", reason=None, response="consented"
        )

        with patch(
            "agents.main_agent.session.hooks.oauth_consent.get_agentcore_identity_client",
            return_value=identity,
        ):
            await hook._gate(event)

        assert oauth_token_cache.get("alice", "google") == "post-consent-token"
        assert event.cancel_tool is None

    @pytest.mark.asyncio
    async def test_resume_without_token_cancels_tool(self):
        """If the user closes the popup mid-flow, AgentCore's vault stays
        empty. Resuming surfaces this as a cancel_tool so the model
        gets a tool_error and can apologize/replan instead of looping."""
        identity = MagicMock()
        identity.get_token_for_user = AsyncMock(
            side_effect=[
                TokenResult(authorization_url="https://accounts/consent"),
                TokenResult(authorization_url="https://accounts/consent"),
            ]
        )

        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: "google",
            scopes_lookup=lambda _: ["openid"],
        )
        event = _make_event(provider_id="google")
        agent = event._agent
        interrupt_id = "v1:before_tool_call:tu_test:oauth:google"
        agent._interrupt_state.interrupts[interrupt_id] = Interrupt(
            interrupt_id, "oauth:google", reason=None, response="consented"
        )

        with patch(
            "agents.main_agent.session.hooks.oauth_consent.get_agentcore_identity_client",
            return_value=identity,
        ):
            await hook._gate(event)

        assert event.cancel_tool is not None
        assert "google" in event.cancel_tool


class TestOAuthConsentHookAuthFailureRetry:
    """The AfterToolCallEvent handler turns a 401-style tool error into
    a retry that forces re-consent at AgentCore Identity."""

    def _after_event(
        self,
        provider_id: str | None,
        result_text: str,
        *,
        result_status: str = "error",
    ) -> MagicMock:
        event = MagicMock()
        event.selected_tool = MagicMock()
        event.tool_use = {"name": "whoami", "toolUseId": "tu_1"}
        event.invocation_state = {}
        event.result = {
            "toolUseId": "tu_1",
            "status": result_status,
            "content": [{"text": result_text}],
        }
        event.retry = False
        return event

    @pytest.mark.asyncio
    async def test_401_marks_force_reauth_and_retries(self):
        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: "google",
            scopes_lookup=lambda _: [],
        )
        oauth_token_cache.set("alice", "google", "stale-token")
        event = self._after_event(
            "google",
            "Error executing tool whoami: Google rejected the OAuth token (401).",
        )

        await hook._handle_auth_failure(event)

        assert event.retry is True
        assert oauth_token_cache.needs_force_reauth("alice", "google") is True
        # Cache cleared so the BeforeToolCallEvent retry doesn't short-circuit.
        assert oauth_token_cache.get("alice", "google") is None

    @pytest.mark.asyncio
    async def test_non_oauth_tool_is_ignored(self):
        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: None,
            scopes_lookup=lambda _: [],
        )
        event = self._after_event(None, "401 Unauthorized")

        await hook._handle_auth_failure(event)

        assert event.retry is False

    @pytest.mark.asyncio
    async def test_non_auth_error_is_ignored(self):
        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: "google",
            scopes_lookup=lambda _: [],
        )
        event = self._after_event("google", "Network unreachable")

        await hook._handle_auth_failure(event)

        assert event.retry is False
        assert oauth_token_cache.needs_force_reauth("alice", "google") is False

    @pytest.mark.asyncio
    async def test_does_not_retry_twice_for_same_tool_use(self):
        """Second 401 in the same retry cycle must not loop forever."""
        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: "google",
            scopes_lookup=lambda _: [],
        )
        event1 = self._after_event("google", "401 Unauthorized")
        await hook._handle_auth_failure(event1)
        assert event1.retry is True

        # Same tool_use_id, same invocation_state — second failure must
        # surrender so the user sees the error.
        event2 = self._after_event("google", "401 Unauthorized")
        event2.invocation_state = event1.invocation_state
        await hook._handle_auth_failure(event2)
        assert event2.retry is False


class TestOAuthConsentHookErrors:
    @pytest.mark.asyncio
    async def test_workload_token_unavailable_lets_tool_proceed(self):
        """A misconfigured runtime context shouldn't crash the agent; the
        tool runs, the MCP server 401s, and the failure surfaces as a
        normal tool_error the user can act on."""
        identity = MagicMock()
        identity.get_token_for_user = AsyncMock(
            side_effect=WorkloadTokenUnavailableError("no ctx")
        )

        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: "google",
            scopes_lookup=lambda _: ["openid"],
        )
        event = _make_event(provider_id="google")

        with patch(
            "agents.main_agent.session.hooks.oauth_consent.get_agentcore_identity_client",
            return_value=identity,
        ):
            await hook._gate(event)  # must not raise

        assert event.cancel_tool is None
        assert oauth_token_cache.get("alice", "google") is None

    @pytest.mark.asyncio
    async def test_scopes_lookup_can_be_async(self):
        """Hook accepts async scopes_lookup so callers can read directly
        from an async repository without a sync wrapper."""
        identity = MagicMock()
        identity.get_token_for_user = AsyncMock(
            return_value=TokenResult(access_token="t")
        )

        async def async_scopes(_pid: str) -> list[str]:
            return ["openid", "profile"]

        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: "google",
            scopes_lookup=async_scopes,
        )
        event = _make_event(provider_id="google")

        with patch(
            "agents.main_agent.session.hooks.oauth_consent.get_agentcore_identity_client",
            return_value=identity,
        ):
            await hook._gate(event)

        kwargs = identity.get_token_for_user.call_args.kwargs
        assert kwargs["scopes"] == ["openid", "profile"]

    @pytest.mark.asyncio
    async def test_scopes_lookup_is_cached_across_calls(self):
        """Repeated tool calls for the same provider hit the scopes lookup
        once per hook lifetime (one agent invocation)."""
        identity = MagicMock()
        identity.get_token_for_user = AsyncMock(
            return_value=TokenResult(access_token="t")
        )

        scopes_lookup = MagicMock(return_value=["openid"])

        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: "google",
            scopes_lookup=scopes_lookup,
        )

        # First call hits identity (and the lookup).
        event1 = _make_event(provider_id="google")
        with patch(
            "agents.main_agent.session.hooks.oauth_consent.get_agentcore_identity_client",
            return_value=identity,
        ):
            await hook._gate(event1)

        # Cache now warm — second call short-circuits before identity.
        # Force a vault fetch by clearing the token cache.
        oauth_token_cache.clear_user("alice")
        event2 = _make_event(provider_id="google")
        with patch(
            "agents.main_agent.session.hooks.oauth_consent.get_agentcore_identity_client",
            return_value=identity,
        ):
            await hook._gate(event2)

        assert scopes_lookup.call_count == 1
