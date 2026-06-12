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
from apis.shared.oauth.agentcore_identity import (
    TokenResult,
    WorkloadTokenUnavailableError,
)
from agents.main_agent.session.hooks.oauth_consent import (
    OAuthConsentHook,
    _looks_like_auth_failure,
)


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
    async def test_disconnected_lookup_bypasses_token_cache(self):
        """When the durable disconnect flag is set, the in-process token
        cache must not short-circuit — even on the same replica that holds
        a warm token. Confirms the source-of-truth reordering: DDB first,
        then cache."""
        oauth_token_cache.set("alice", "google", "stale-cached-token")

        identity = MagicMock()
        identity.get_token_for_user = AsyncMock(
            return_value=TokenResult(authorization_url="https://accounts/consent")
        )

        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: "google",
            scopes_lookup=lambda _: ["openid"],
            disconnected_lookup=lambda _pid: True,
        )
        event = _make_event(provider_id="google")

        with patch(
            "agents.main_agent.session.hooks.oauth_consent.get_agentcore_identity_client",
            return_value=identity,
        ):
            with pytest.raises(InterruptException):
                await hook._gate(event)

        # Identity was consulted with force_authentication=True so AgentCore
        # bypasses its vault and returns a fresh consent URL.
        identity.get_token_for_user.assert_called_once()
        kwargs = identity.get_token_for_user.call_args.kwargs
        assert kwargs["force_authentication"] is True

    @pytest.mark.asyncio
    async def test_force_authentication_sends_prompt_consent_for_google(self):
        """Google only re-issues a refresh token on subsequent grants if
        the user is shown the consent screen — so the explicit re-consent
        path (force_authentication=True) must propagate prompt=consent
        through to AgentCore Identity. Without it, a Disconnect/Reconnect
        cycle leaves the vault with an access token but no refresh token,
        putting the user back in the hourly-reconsent loop."""
        identity = MagicMock()
        identity.get_token_for_user = AsyncMock(
            return_value=TokenResult(authorization_url="https://accounts/consent")
        )

        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: "google",
            scopes_lookup=lambda _: ["openid"],
            provider_type_lookup=lambda _: "google",
            disconnected_lookup=lambda _pid: True,
        )
        event = _make_event(provider_id="google")

        with patch(
            "agents.main_agent.session.hooks.oauth_consent.get_agentcore_identity_client",
            return_value=identity,
        ):
            with pytest.raises(InterruptException):
                await hook._gate(event)

        kwargs = identity.get_token_for_user.call_args.kwargs
        assert kwargs["force_authentication"] is True
        assert kwargs["custom_parameters"] == {
            "access_type": "offline",
            "prompt": "consent",
        }

    @pytest.mark.asyncio
    async def test_silent_refresh_does_not_send_prompt_consent(self):
        """The refresh path (force_authentication=False) must not send
        prompt=consent — that would force the consent screen on every
        silent refresh, which is the exact UX we're trying to avoid."""
        identity = MagicMock()
        identity.get_token_for_user = AsyncMock(
            return_value=TokenResult(access_token="refreshed-token")
        )

        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: "google",
            scopes_lookup=lambda _: ["openid"],
            provider_type_lookup=lambda _: "google",
        )
        event = _make_event(provider_id="google")

        with patch(
            "agents.main_agent.session.hooks.oauth_consent.get_agentcore_identity_client",
            return_value=identity,
        ):
            await hook._gate(event)

        kwargs = identity.get_token_for_user.call_args.kwargs
        assert kwargs["force_authentication"] is False
        assert kwargs["custom_parameters"] == {"access_type": "offline"}
        assert "prompt" not in kwargs["custom_parameters"]

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
        identity.get_token_for_user.assert_called_once()
        kwargs = identity.get_token_for_user.call_args.kwargs
        assert kwargs["provider_name"] == "google"
        assert kwargs["scopes"] == ["openid"]
        assert kwargs["user_id"] == "alice"
        assert kwargs["force_authentication"] is False


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


class TestOAuthConsentHookSkillFoldedTools:
    """Regression guard for the skills-mode consent gap.

    A skill-bound external MCP tool executes through the `skill_executor`
    meta-tool, so `selected_tool` is the executor (not an MCPAgentTool) and
    `provider_lookup` returns None. Before the `tool_use_provider_lookup`
    fallback existed, the gate silently skipped these calls: the tool ran
    tokenless, returned an auth error, and the model apologized in text
    instead of the turn pausing with an `oauth_required` interrupt.
    """

    @staticmethod
    def _tool_use_lookup(tool_use: dict) -> str | None:
        # Stands in for skills/mcp_binding.make_folded_tool_provider_lookup
        # (covered by its own tests); resolves the executor's folded target.
        if tool_use.get("name") != "skill_executor":
            return None
        if (tool_use.get("input") or {}).get("tool_name") == "gmail_search":
            return "google"
        return None

    def _executor_event(self):
        event = _make_event(provider_id=None)
        event.tool_use = {
            "toolUseId": "tu_skill",
            "name": "skill_executor",
            "input": {
                "skill_name": "gmail-for-employees",
                "tool_name": "gmail_search",
                "tool_input": {"query": "from:hr"},
            },
        }
        return event

    @pytest.mark.asyncio
    async def test_skill_bound_tool_without_token_pauses_with_oauth_required(self):
        identity = MagicMock()
        identity.get_token_for_user = AsyncMock(
            return_value=TokenResult(authorization_url="https://accounts/consent")
        )

        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: None,  # executor isn't an MCPAgentTool
            scopes_lookup=lambda _: ["gmail.readonly"],
            tool_use_provider_lookup=self._tool_use_lookup,
        )
        event = self._executor_event()

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

    @pytest.mark.asyncio
    async def test_skill_bound_tool_with_cached_token_proceeds(self):
        oauth_token_cache.set("alice", "google", "warm-token")
        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: None,
            scopes_lookup=lambda _: [],
            tool_use_provider_lookup=self._tool_use_lookup,
        )
        event = self._executor_event()

        await hook._gate(event)

        assert event.cancel_tool is None

    @pytest.mark.asyncio
    async def test_401_from_folded_tool_clears_cache_and_retries(self):
        """The fold preserves error status (FoldedMCPTool returns a
        ToolResult-shaped dict), so the AfterToolCallEvent retry path must
        work through the executor too."""
        oauth_token_cache.set("alice", "google", "stale-token")
        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: None,
            scopes_lookup=lambda _: [],
            tool_use_provider_lookup=self._tool_use_lookup,
        )
        event = self._executor_event()
        event.invocation_state = {}
        event.result = {
            "toolUseId": "tu_skill",
            "status": "error",
            "content": [{"text": "Error calling tool (HTTP 401): Unauthorized"}],
        }
        event.retry = False

        await hook._handle_auth_failure(event)

        assert event.retry is True
        assert oauth_token_cache.get("alice", "google") is None

    @pytest.mark.asyncio
    async def test_without_tool_use_lookup_executor_is_not_gated(self):
        """Plain ChatAgent wiring (no lookup) keeps the old behavior."""
        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: None,
            scopes_lookup=lambda _: [],
        )
        event = self._executor_event()

        await hook._gate(event)

        assert event.cancel_tool is None


class TestParallelToolCallsSameProvider:
    """Regression guard for the OAuth interrupt collision concern.

    `_gate` calls `event.interrupt(name=f"oauth:{provider_id}")` with a
    name that is *not* unique across parallel tool calls to the same
    provider. We rely on Strands' BeforeToolCallEvent._interrupt_id to
    fold `tool_use.toolUseId` into the final id, so two parallel calls
    produce distinct entries in `_interrupt_state.interrupts`.

    If Strands ever drops `toolUseId` from the id formula, this test
    fails and we'd need to incorporate it ourselves in the hook.
    """

    def test_parallel_tool_calls_same_provider_produce_distinct_interrupt_ids(self):
        from strands.hooks import BeforeToolCallEvent

        agent = MagicMock()
        event_a = BeforeToolCallEvent(
            agent=agent,
            selected_tool=MagicMock(),
            tool_use={"toolUseId": "tu_parallel_a", "name": "search"},
            invocation_state={},
        )
        event_b = BeforeToolCallEvent(
            agent=agent,
            selected_tool=MagicMock(),
            tool_use={"toolUseId": "tu_parallel_b", "name": "search"},
            invocation_state={},
        )

        id_a = event_a._interrupt_id("oauth:google")
        id_b = event_b._interrupt_id("oauth:google")

        assert id_a != id_b, (
            "Strands no longer disambiguates BeforeToolCallEvent interrupts "
            "by toolUseId. OAuthConsentHook must now incorporate toolUseId "
            "into the interrupt name to prevent parallel-call collision."
        )
        assert "tu_parallel_a" in id_a
        assert "tu_parallel_b" in id_b


class TestOAuthConsentHookAuthFailureRetry:
    """The AfterToolCallEvent handler turns a 401-style tool error into a
    retry that re-fetches the token from AgentCore Identity. The retry
    deliberately does NOT write the durable disconnect flag — most 401s
    are just an expired access token, and AgentCore can refresh
    transparently using the refresh_token in the vault. Forcing a
    disconnect on every 401 caused users to reconnect hourly even though
    refresh would have worked."""

    def _after_event(
        self,
        provider_id: str | None,
        result_text: str,
        *,
        result_status: str = "error",
        tool_use_id: str = "tu_1",
        tool_name: str = "whoami",
    ) -> MagicMock:
        event = MagicMock()
        event.selected_tool = MagicMock()
        event.tool_use = {"name": tool_name, "toolUseId": tool_use_id}
        event.invocation_state = {}
        event.result = {
            "toolUseId": tool_use_id,
            "status": result_status,
            "content": [{"text": result_text}],
        }
        event.retry = False
        return event

    @pytest.mark.asyncio
    async def test_401_clears_cache_and_retries(self):
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
        # Local cache cleared so the BeforeToolCallEvent retry doesn't
        # short-circuit on this replica — it'll re-fetch from AgentCore
        # Identity, which can refresh transparently.
        assert oauth_token_cache.get("alice", "google") is None

    @pytest.mark.asyncio
    async def test_401_does_not_write_durable_disconnect_flag(self):
        """Regression guard for the hourly-reconnect bug: writing the
        disconnect flag here would force `_gate` to call AgentCore with
        `force_authentication=True`, bypassing the vault and prompting
        the user to reconsent even though the refresh_token was valid.
        The flag is reserved for the explicit Disconnect button in the
        settings page."""
        # The hook no longer accepts a mark_disconnected hook at all —
        # the parameter and the `_record_disconnect` method are gone.
        # If either reappears, the tests for the consent flow itself
        # should explicitly opt back in.
        import inspect

        sig = inspect.signature(OAuthConsentHook.__init__)
        assert "mark_disconnected" not in sig.parameters
        assert not hasattr(OAuthConsentHook, "_record_disconnect")

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
        oauth_token_cache.set("alice", "google", "good-token")
        event = self._after_event("google", "Network unreachable")

        await hook._handle_auth_failure(event)

        assert event.retry is False
        # Cache untouched — the failure wasn't auth-related.
        assert oauth_token_cache.get("alice", "google") == "good-token"

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

        # Same tool_use, second failure must surrender so the user sees
        # the error instead of looping.
        event2 = self._after_event("google", "401 Unauthorized")
        await hook._handle_auth_failure(event2)
        assert event2.retry is False

    @pytest.mark.asyncio
    async def test_caps_retry_across_tool_calls_in_same_turn(self):
        """A misconfigured provider would otherwise spawn a consent prompt
        on every tool call in a turn. Cap at one retry per provider per
        turn so subsequent 401s for the same provider just surface to the
        model instead of triggering another consent flow."""
        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: "google",
            scopes_lookup=lambda _: [],
        )

        # First tool call 401s — retry path fires.
        event1 = self._after_event(
            "google", "401 Unauthorized", tool_use_id="tu_1", tool_name="search"
        )
        await hook._handle_auth_failure(event1)
        assert event1.retry is True

        # A *different* tool call (different toolUseId) on the same
        # provider 401s later in the same turn. The per-turn cap must
        # block another retry even though invocation_state is fresh.
        event2 = self._after_event(
            "google", "401 Unauthorized", tool_use_id="tu_2", tool_name="list"
        )
        await hook._handle_auth_failure(event2)
        assert event2.retry is False

    @pytest.mark.asyncio
    async def test_before_invocation_event_resets_per_turn_budget(self):
        """The agent instance is cached across turns by `get_agent`, so
        the per-provider retry budget on the hook must be reset whenever
        a new agent invocation begins (fresh turn or resume)."""
        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: "google",
            scopes_lookup=lambda _: [],
        )

        event1 = self._after_event("google", "401 Unauthorized", tool_use_id="tu_1")
        await hook._handle_auth_failure(event1)
        assert event1.retry is True

        # Simulate a new turn starting (Strands fires BeforeInvocationEvent
        # on each `agent.stream_async` call, including resume).
        hook._on_invocation_start(MagicMock())

        event2 = self._after_event("google", "401 Unauthorized", tool_use_id="tu_2")
        await hook._handle_auth_failure(event2)
        assert event2.retry is True

    @pytest.mark.asyncio
    async def test_cap_is_per_provider_not_global(self):
        """One provider hitting its cap mustn't starve a different
        provider that just happens to 401 later in the same turn."""
        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda tool: getattr(tool, "_provider", None),
            scopes_lookup=lambda _: [],
        )

        google_event = self._after_event(
            "google", "401 Unauthorized", tool_use_id="tu_g"
        )
        google_event.selected_tool._provider = "google"
        await hook._handle_auth_failure(google_event)
        assert google_event.retry is True

        slack_event = self._after_event(
            "slack", "401 Unauthorized", tool_use_id="tu_s"
        )
        slack_event.selected_tool._provider = "slack"
        await hook._handle_auth_failure(slack_event)
        assert slack_event.retry is True


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

    @pytest.mark.asyncio
    async def test_provider_type_lookup_forwards_custom_parameters(self):
        """When the provider is Google, the hook forwards
        `custom_parameters={"access_type": "offline"}` to AgentCore Identity
        so Google issues a refresh token (vault entry would otherwise expire
        after ~1 hour with no refresh path)."""
        identity = MagicMock()
        identity.get_token_for_user = AsyncMock(
            return_value=TokenResult(access_token="t")
        )

        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: "google",
            scopes_lookup=lambda _: ["openid"],
            provider_type_lookup=lambda _: "google",
        )

        event = _make_event(provider_id="google")
        with patch(
            "agents.main_agent.session.hooks.oauth_consent.get_agentcore_identity_client",
            return_value=identity,
        ):
            await hook._gate(event)

        identity.get_token_for_user.assert_called_once()
        assert identity.get_token_for_user.call_args.kwargs["custom_parameters"] == {
            "access_type": "offline",
        }

    @pytest.mark.asyncio
    async def test_admin_custom_parameters_merge_with_baseline(self):
        """Hook merges admin-supplied extras (e.g. Google `hd=` for Workspace
        domain restriction) with the vendor baseline before forwarding to
        AgentCore. Baseline still wins on conflict."""
        identity = MagicMock()
        identity.get_token_for_user = AsyncMock(
            return_value=TokenResult(access_token="t")
        )

        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: "google",
            scopes_lookup=lambda _: ["openid"],
            provider_type_lookup=lambda _: "google",
            custom_parameters_lookup=lambda _: {
                "hd": "mycompany.com",
                "access_type": "online",  # admin attempts override; ignored
            },
        )

        event = _make_event(provider_id="google")
        with patch(
            "agents.main_agent.session.hooks.oauth_consent.get_agentcore_identity_client",
            return_value=identity,
        ):
            await hook._gate(event)

        identity.get_token_for_user.assert_called_once()
        assert identity.get_token_for_user.call_args.kwargs["custom_parameters"] == {
            "access_type": "offline",  # baseline wins
            "hd": "mycompany.com",
        }

    @pytest.mark.asyncio
    async def test_no_provider_type_lookup_omits_custom_parameters(self):
        """When the lookup is omitted (legacy callers / non-Google vendors),
        no `custom_parameters` is sent — AgentCore handles vendor defaults
        and we don't accidentally inject Google-specific keys elsewhere."""
        identity = MagicMock()
        identity.get_token_for_user = AsyncMock(
            return_value=TokenResult(access_token="t")
        )

        hook = OAuthConsentHook(
            user_id="alice",
            provider_lookup=lambda _tool: "github",
            scopes_lookup=lambda _: ["read:user"],
            # no provider_type_lookup
        )

        event = _make_event(provider_id="github")
        with patch(
            "agents.main_agent.session.hooks.oauth_consent.get_agentcore_identity_client",
            return_value=identity,
        ):
            await hook._gate(event)

        identity.get_token_for_user.assert_called_once()
        assert identity.get_token_for_user.call_args.kwargs["custom_parameters"] is None


class TestLooksLikeAuthFailure:
    """Detector must fire on genuine auth failures and ignore everything
    else — paths containing `401`, non-error statuses, benign prose.
    """

    def _err(self, text: str) -> dict:
        return {"status": "error", "content": [{"text": text}]}

    def _ok(self, text: str) -> dict:
        return {"status": "success", "content": [{"text": text}]}

    @pytest.mark.parametrize(
        "text",
        [
            # HTTP 401 in various shapes.
            "HTTP 401 Unauthorized",
            "Request failed: 401",
            "status=401 message=unauthorized",
            "401 Client Error: Unauthorized for url: https://...",
            # "Unauthorized" paired with an HTTP/status/code keyword.
            "HTTP response: Unauthorized",
            "status code Unauthorized",
            # Unambiguous OAuth/token signals stand alone.
            "The server rejected the OAuth token",
            "invalid_token",
            "invalid-token",
            "invalid token",
            "expired_token",
            "token expired",
            "token_expired",
            "oauth token expired",
            "oauth token has expired",
            # Refresh-token revocation surfaces with this OAuth error code.
            "invalid_grant: Token has been expired or revoked",
            # Google API auth signals.
            "Request had invalid authentication credentials",
            "Request had invalid_authentication_credentials",
            'status "UNAUTHENTICATED"',
        ],
    )
    def test_matches_genuine_auth_errors(self, text):
        assert _looks_like_auth_failure(self._err(text)) is True

    @pytest.mark.parametrize(
        "text",
        [
            # Path segments containing 401 — previously false-positive.
            "GET /v1/401/foo failed with 500",
            "https://example.com/api/401/items returned empty",
            # Digits embedded in other numbers.
            "returned 4010 rows",
            "status 14011",
            # Token as substring of longer words should not match.
            "refreshtokenRequired",
            "ExpiredTokens",  # plural — not \btoken\b
            # Prose mentions of "unauthorized" without HTTP/status context.
            # Previously fired off the bare \bunauthorized\b alternative;
            # tightening means application-level "not authorized" prose no
            # longer triggers an OAuth re-auth.
            "The weather today is unauthorized-feeling, but fine",
            "Unauthorized",  # bare — too ambiguous on its own
            "You are not authorized to view this calendar entry",
            # Prose that shouldn't trigger anything.
            "Everything is fine, nothing to see here",
            "Rate limit exceeded",
            "500 Internal Server Error",
            # "PERMISSION_DENIED" is intentionally NOT matched — it's a
            # scope/ACL problem at the provider, not an OAuth credential
            # failure, and re-consenting won't change the outcome.
            "PERMISSION_DENIED",
            "Insufficient permissions",
        ],
    )
    def test_avoids_false_positives(self, text):
        assert _looks_like_auth_failure(self._err(text)) is False

    def test_ignores_non_error_status(self):
        # Even an auth-shaped body doesn't count if status is success.
        assert _looks_like_auth_failure(self._ok("401 Unauthorized")) is False

    def test_ignores_non_dict_result(self):
        assert _looks_like_auth_failure("HTTP 401 Unauthorized") is False
        assert _looks_like_auth_failure(None) is False
        assert _looks_like_auth_failure(["401"]) is False

    def test_ignores_missing_content(self):
        assert _looks_like_auth_failure({"status": "error"}) is False
        assert _looks_like_auth_failure({"status": "error", "content": None}) is False
        assert _looks_like_auth_failure({"status": "error", "content": []}) is False

    def test_ignores_non_dict_content_blocks(self):
        result = {"status": "error", "content": ["401 Unauthorized"]}
        assert _looks_like_auth_failure(result) is False
