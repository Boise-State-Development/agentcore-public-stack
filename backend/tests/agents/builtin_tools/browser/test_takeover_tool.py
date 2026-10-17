"""Tests for `request_user_login` and the pool's control primitives.

No AWS: the pool is driven against a fake `BrowserClient` that records
`UpdateBrowserStream` calls, and the interrupt against a fake `ToolContext`
that behaves the way Strands does — raise on the first pass, return the stored
response on the second, with the tool re-executed from the top in between.

The traps these exist to hold down:

* **Re-entry.** Strands re-runs an interrupted tool from the start, so a naive
  implementation takes control twice — the second time against a browser the
  user has already handed back.
* **Release on every path.** A turn that leaves the automation stream DISABLED
  bricks every later `browse_web` call in the conversation.
* **Reaper pinning.** Idleness is counted in agent tool calls, of which a
  takeover makes none, so the plain rule reaps the browser mid-login — but an
  unbounded exemption leaves an abandoned session billing to its TTL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from agents.builtin_tools.browser import session_pool, takeover_tool
from agents.builtin_tools.browser.takeover_tool import request_user_login
from strands.interrupt import InterruptException


def _call(tool, **kwargs):
    """Reach the underlying callable inside the Strands tool wrapper."""
    inner = getattr(tool, "_tool_func", None) or getattr(tool, "func", None) or tool
    if hasattr(inner, "__wrapped__"):
        inner = inner.__wrapped__
    return inner(**kwargs)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeState:
    """`AgentState` deep-copies on read, and so does this."""

    def __init__(self) -> None:
        self._data: Dict[str, Any] = {}

    def get(self, key: str) -> Any:
        return json.loads(json.dumps(self._data.get(key))) if key in self._data else None

    def set(self, key: str, value: Any) -> None:
        self._data[key] = json.loads(json.dumps(value))


class FakeAgent:
    def __init__(self) -> None:
        self.state = FakeState()


class FakeClient:
    """Records the stream flips a real `BrowserClient` would make."""

    def __init__(self) -> None:
        self.identifier = "browser-abc"
        self.session_id = "bs-1"
        self.stream: List[str] = []
        self.take_fails = False

    def take_control(self) -> None:
        if self.take_fails:
            raise RuntimeError("UpdateBrowserStream refused")
        self.stream.append("DISABLED")

    def release_control(self) -> None:
        self.stream.append("ENABLED")

    def stop(self) -> bool:
        self.stream.append("STOPPED")
        return True


@dataclass
class FakeCdp:
    url: str = "https://www.jstor.org/action/showLogin"
    closed: bool = False

    async def evaluate(self, expression: str, **_: Any) -> Any:
        return self.url if "location.href" in expression else None

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    """A `ToolContext` that reproduces Strands' interrupt semantics.

    `interrupt()` raises the first time and returns the stored response
    afterwards — which is exactly why the tool body runs twice.
    """

    def __init__(self, agent: FakeAgent, response: Any = None) -> None:
        self.agent = agent
        self.tool_use = {"toolUseId": "tu-1"}
        self.response = response
        self.reasons: List[Any] = []

    def interrupt(self, name: str, reason: Any = None, response: Any = None) -> Any:
        self.reasons.append(reason)
        if self.response is None:
            raise InterruptException(_FakeInterrupt(name, reason))
        return self.response


@dataclass
class _FakeInterrupt:
    name: str
    reason: Any
    id: str = "v1:tool_call:tu-1:abc"


@pytest.fixture
def pooled(monkeypatch):
    """A live session in the pool, wired to fakes instead of AWS.

    Runs with the window fit OFF: these tests pin the handover itself, which is
    exactly what the fit's rollout switch falls back to. The fit and restore
    are covered with it on in `TestWindowFitForTakeover`.
    """
    monkeypatch.setenv("BROWSER_TAKEOVER_FIT_ENABLED", "false")
    session_pool._live.clear()
    client = FakeClient()
    live = session_pool._LiveSession(
        session_id="bs-1",
        identifier="browser-abc",
        client=client,
        cdp=FakeCdp(),  # type: ignore[arg-type]
    )
    session_pool._live["bs-1"] = live

    async def _acquire(agent):
        session_pool._write_state(
            agent, {"sessionId": "bs-1", "identifier": "browser-abc"}
        )
        return live

    monkeypatch.setattr(session_pool, "acquire", _acquire)
    # The tool refuses to hand over a browser that has never navigated, so most
    # cases here need a conversation that has already browsed. `FakeAgent`
    # instances are made per test, so this patches the check rather than the
    # state; the one case that exercises the refusal overrides it.
    monkeypatch.setattr(session_pool, "has_session", lambda _agent: True)
    yield live
    session_pool._live.clear()


# ---------------------------------------------------------------------------
# The pool's control primitives
# ---------------------------------------------------------------------------


class TestTakeAndReleaseControl:
    @pytest.mark.asyncio
    async def test_take_control_disables_the_automation_stream(self, pooled) -> None:
        agent = FakeAgent()

        ref = await session_pool.take_control(agent)

        assert pooled.client.stream == ["DISABLED"]
        assert ref["browserSessionId"] == "bs-1"
        assert ref["viewport"] == session_pool.DEFAULT_VIEWPORT
        assert ref["deadlineAt"] is not None

    @pytest.mark.asyncio
    async def test_take_control_is_idempotent_and_does_not_extend_the_deadline(
        self, pooled
    ) -> None:
        agent = FakeAgent()

        first = await session_pool.take_control(agent)
        deadline = pooled.control_deadline
        second = await session_pool.take_control(agent)

        # One stream flip, not two — the resume pass must not re-take a browser
        # the user already holds.
        assert pooled.client.stream == ["DISABLED"]
        assert pooled.control_deadline == deadline
        assert first["browserSessionId"] == second["browserSessionId"]

    @pytest.mark.asyncio
    async def test_release_control_re_enables_and_is_idempotent(self, pooled) -> None:
        agent = FakeAgent()
        await session_pool.take_control(agent)

        assert await session_pool.release_control(agent) is True
        assert await session_pool.release_control(agent) is False
        assert pooled.client.stream == ["DISABLED", "ENABLED"]

    @pytest.mark.asyncio
    async def test_release_works_when_the_resume_lands_in_another_container(
        self, monkeypatch, pooled
    ) -> None:
        # The takeover happened elsewhere: this process has the state but not
        # the socket. Returning early here would leave the stream DISABLED
        # forever and brick every later browse_web call.
        agent = FakeAgent()
        session_pool._write_state(
            agent, {"sessionId": "bs-1", "identifier": "browser-abc"}
        )
        session_pool._live.clear()

        remote = FakeClient()
        monkeypatch.setattr(
            "bedrock_agentcore.tools.browser_client.BrowserClient",
            lambda **_: remote,
        )

        assert await session_pool.release_control(agent) is True
        assert remote.stream == ["ENABLED"]

    @pytest.mark.asyncio
    async def test_a_failed_release_still_drops_the_local_pin(self, pooled) -> None:
        agent = FakeAgent()
        await session_pool.take_control(agent)

        def _boom() -> None:
            raise RuntimeError("service refused")

        pooled.client.release_control = _boom

        assert await session_pool.release_control(agent) is False
        # Pin dropped anyway: keeping it on the strength of a failed API call
        # is how an abandoned browser bills to its TTL.
        assert pooled.user_controlled() is False


class TestSocketSurvivesTheHandback:
    """The bug this class exists for, seen in prod 2026-09-20.

    Disabling the automation stream makes the service close our CDP socket
    ("Disconnected by admin"). Re-enabling the stream does not revive it, so
    the handback has to unpool the dead socket — otherwise every later
    `browse_web` fails for the rest of the conversation, which is exactly what
    happened: sign-in succeeded, the stream came back ENABLED, and four
    straight browse calls still failed on the corpse.
    """

    @pytest.mark.asyncio
    async def test_handback_closes_the_dead_socket(self, pooled) -> None:
        agent = FakeAgent()
        await session_pool.take_control(agent)

        assert await session_pool.release_control(agent) is True

        # Closed, so the next `acquire` reconnects instead of reusing it —
        # but still pooled, so a second handback stays a local no-op.
        assert pooled.cdp.closed is True
        assert "bs-1" in session_pool._live

    @pytest.mark.asyncio
    async def test_next_acquire_reconnects_to_the_same_browser(
        self, pooled, monkeypatch
    ) -> None:
        agent = FakeAgent()
        await session_pool.take_control(agent)
        await session_pool.release_control(agent)

        monkeypatch.undo()  # the real `acquire`, not the fixture's stub
        session_pool._write_state(
            agent, {"sessionId": "bs-1", "identifier": "browser-abc"}
        )
        reconnected = session_pool._LiveSession(
            session_id="bs-1",
            identifier="browser-abc",
            client=pooled.client,
            cdp=FakeCdp(),  # type: ignore[arg-type]
        )

        async def _reconnect(entry: Dict[str, Any]):
            assert entry["sessionId"] == "bs-1"  # the SAME browser: the login
            session_pool._live["bs-1"] = reconnected
            return reconnected

        async def _never(*_a: Any, **_k: Any):
            raise AssertionError("started a second browser after the handback")

        monkeypatch.setattr(session_pool, "_try_reconnect", _reconnect)
        monkeypatch.setattr(session_pool, "_start_remote_session", _never)

        assert await session_pool.acquire(agent) is reconnected

    @pytest.mark.asyncio
    async def test_handback_does_not_stop_the_remote_browser(self, pooled) -> None:
        agent = FakeAgent()
        await session_pool.take_control(agent)

        await session_pool.release_control(agent)

        # Stopping it would throw away the browser the user just signed into.
        assert pooled.client.stream == ["DISABLED", "ENABLED"]

    @pytest.mark.asyncio
    async def test_socket_is_closed_even_when_the_stream_flip_fails(
        self, pooled, monkeypatch
    ) -> None:
        agent = FakeAgent()
        await session_pool.take_control(agent)

        def _refuse() -> None:
            raise RuntimeError("UpdateBrowserStream refused")

        monkeypatch.setattr(pooled.client, "release_control", _refuse)

        assert await session_pool.release_control(agent) is False
        # The socket is dead whether or not the flip landed, so it must not be
        # handed out again; `acquire` will try to reconnect and report honestly
        # if the stream really is still DISABLED.
        assert pooled.cdp.closed is True

    @pytest.mark.asyncio
    async def test_acquire_does_not_start_a_second_browser_mid_login(
        self, pooled, monkeypatch
    ) -> None:
        # The real `acquire`, not the fixture's stub: the point is what it does
        # with a pooled session whose socket the service has already killed.
        monkeypatch.undo()
        agent = FakeAgent()
        session_pool._write_state(
            agent, {"sessionId": "bs-1", "identifier": "browser-abc"}
        )
        pooled.control_state = "user"
        pooled.control_deadline = time.monotonic() + 300
        pooled.cdp.closed = True  # the handover killed it

        async def _never(*_a: Any, **_k: Any):
            raise AssertionError("started a second browser mid-login")

        monkeypatch.setattr(session_pool, "_start_remote_session", _never)
        monkeypatch.setattr(session_pool, "_try_reconnect", _never)

        assert await session_pool.acquire(agent) is pooled


class TestReaperPinning:
    @pytest.mark.asyncio
    async def test_a_session_the_user_is_driving_is_not_reaped(self, pooled) -> None:
        agent = FakeAgent()
        await session_pool.take_control(agent)
        # Idle by the agent's reckoning — a takeover makes no tool calls.
        pooled.last_used = time.monotonic() - session_pool.IDLE_REAP_SECONDS - 60

        await session_pool._reap_idle()

        assert "bs-1" in session_pool._live
        assert pooled.cdp.closed is False

    @pytest.mark.asyncio
    async def test_an_abandoned_takeover_becomes_reapable_past_the_deadline(
        self, pooled
    ) -> None:
        agent = FakeAgent()
        await session_pool.take_control(agent)
        pooled.last_used = time.monotonic() - session_pool.IDLE_REAP_SECONDS - 60
        # The user opened the live view and walked away.
        pooled.control_deadline = time.monotonic() - 1

        await session_pool._reap_idle()

        assert "bs-1" not in session_pool._live

    @pytest.mark.asyncio
    async def test_an_ordinary_idle_session_is_still_reaped(self, pooled) -> None:
        pooled.last_used = time.monotonic() - session_pool.IDLE_REAP_SECONDS - 60

        await session_pool._reap_idle()

        assert "bs-1" not in session_pool._live


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


class TestRequestUserLogin:
    @pytest.mark.asyncio
    async def test_first_pass_takes_control_and_pauses_the_turn(self, pooled) -> None:
        agent = FakeAgent()
        context = FakeContext(agent)

        with pytest.raises(InterruptException):
            await _call(
                request_user_login,
                tool_context=context,
                reason="Sign in to JSTOR so I can check the results page.",
            )

        assert pooled.client.stream == ["DISABLED"]
        payload = context.reasons[0]
        assert payload["type"] == "browser_login_required"
        assert payload["browserSessionId"] == "bs-1"
        assert payload["reason"].startswith("Sign in to JSTOR")

    @pytest.mark.asyncio
    async def test_the_interrupt_payload_carries_no_live_view_url(self, pooled) -> None:
        from apis.shared.browser_takeover import assert_no_url

        agent = FakeAgent()
        context = FakeContext(agent)

        with pytest.raises(InterruptException):
            await _call(request_user_login, tool_context=context, reason="Sign in.")

        assert_no_url(context.reasons[0])  # must not raise
        assert "liveViewUrl" not in context.reasons[0]

    @pytest.mark.asyncio
    async def test_the_page_is_named_so_the_user_knows_what_they_sign_into(
        self, pooled
    ) -> None:
        agent = FakeAgent()
        context = FakeContext(agent)

        with pytest.raises(InterruptException):
            await _call(request_user_login, tool_context=context, reason="Sign in.")

        assert context.reasons[0]["targetUrl"] == "https://www.jstor.org/action/showLogin"

    @pytest.mark.asyncio
    async def test_resume_releases_control_and_reports_success(self, pooled) -> None:
        agent = FakeAgent()

        # Pass 1: pauses.
        with pytest.raises(InterruptException):
            await _call(
                request_user_login, tool_context=FakeContext(agent), reason="Sign in."
            )
        # Pass 2: Strands re-runs the tool from the top with the response.
        result = await _call(
            request_user_login,
            tool_context=FakeContext(agent, response={"completed": True}),
            reason="Sign in.",
        )

        assert result["status"] == "success"
        assert "authenticated" in result["content"][0]["text"]
        # Exactly one DISABLED: the re-run must not take control a second time.
        assert pooled.client.stream == ["DISABLED", "ENABLED"]

    @pytest.mark.asyncio
    async def test_resume_without_the_session_does_not_start_a_second_browser(
        self, monkeypatch, pooled
    ) -> None:
        # The resume landed in a container that never saw the takeover. Without
        # the state marker the tool would re-derive its descriptor through
        # `acquire`, which starts a *fresh* browser — unauthenticated, billed,
        # and reported to the model as a successful sign-in.
        agent = FakeAgent()
        with pytest.raises(InterruptException):
            await _call(
                request_user_login, tool_context=FakeContext(agent), reason="Sign in."
            )

        async def _must_not_acquire(_agent):
            raise AssertionError("resume must not start or re-acquire a session")

        monkeypatch.setattr(session_pool, "acquire", _must_not_acquire)

        result = await _call(
            request_user_login,
            tool_context=FakeContext(agent, response={"completed": True}),
            reason="Sign in.",
        )

        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_the_takeover_marker_is_cleared_so_a_later_call_can_pause_again(
        self, pooled
    ) -> None:
        agent = FakeAgent()
        with pytest.raises(InterruptException):
            await _call(
                request_user_login, tool_context=FakeContext(agent), reason="Sign in."
            )

        assert session_pool.read_takeover(agent) is not None

        await _call(
            request_user_login,
            tool_context=FakeContext(agent, response={"completed": True}),
            reason="Sign in.",
        )

        assert session_pool.read_takeover(agent) is None

    @pytest.mark.asyncio
    async def test_a_declined_sign_in_still_releases_the_browser(self, pooled) -> None:
        agent = FakeAgent()
        with pytest.raises(InterruptException):
            await _call(
                request_user_login, tool_context=FakeContext(agent), reason="Sign in."
            )

        result = await _call(
            request_user_login,
            tool_context=FakeContext(agent, response={"skipped": True}),
            reason="Sign in.",
        )

        assert result["status"] == "success"
        assert "not ask again" in result["content"][0]["text"]
        # Release is unconditional: leaving the stream DISABLED after a decline
        # would brick every later browse_web call in the conversation.
        assert pooled.client.stream[-1] == "ENABLED"

    @pytest.mark.asyncio
    async def test_a_completion_far_past_the_deadline_reads_as_expired(
        self, pooled
    ) -> None:
        agent = FakeAgent()
        with pytest.raises(InterruptException):
            await _call(
                request_user_login, tool_context=FakeContext(agent), reason="Sign in."
            )

        # The session was released and made reapable while the user was away,
        # so we cannot honestly report an authenticated browser.
        marker = session_pool.read_takeover(agent)
        marker["ref"]["deadlineAt"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        session_pool.write_takeover(agent, marker)

        result = await _call(
            request_user_login,
            tool_context=FakeContext(agent, response={"completed": True}),
            reason="Sign in.",
        )

        assert "lapsed" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_a_completion_inside_the_grace_window_is_honoured(
        self, pooled
    ) -> None:
        agent = FakeAgent()
        with pytest.raises(InterruptException):
            await _call(
                request_user_login, tool_context=FakeContext(agent), reason="Sign in."
            )

        # Finished at 7:59, POST landed at 8:01. Throwing the sign-in away here
        # would waste a login the user actually completed.
        marker = session_pool.read_takeover(agent)
        marker["ref"]["deadlineAt"] = (
            datetime.now(timezone.utc) - timedelta(seconds=5)
        ).isoformat()
        session_pool.write_takeover(agent, marker)

        result = await _call(
            request_user_login,
            tool_context=FakeContext(agent, response={"completed": True}),
            reason="Sign in.",
        )

        assert "authenticated" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_an_unparseable_deadline_does_not_discard_a_real_sign_in(
        self, pooled
    ) -> None:
        agent = FakeAgent()
        with pytest.raises(InterruptException):
            await _call(
                request_user_login, tool_context=FakeContext(agent), reason="Sign in."
            )

        marker = session_pool.read_takeover(agent)
        marker["ref"]["deadlineAt"] = "not a timestamp"
        session_pool.write_takeover(agent, marker)

        result = await _call(
            request_user_login,
            tool_context=FakeContext(agent, response={"completed": True}),
            reason="Sign in.",
        )

        assert "authenticated" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_a_failed_handover_errors_instead_of_pausing(self, pooled) -> None:
        agent = FakeAgent()
        context = FakeContext(agent)
        pooled.client.take_fails = True

        result = await _call(request_user_login, tool_context=context, reason="Sign in.")

        assert result["status"] == "error"
        assert context.reasons == []  # the turn was never paused

    @pytest.mark.asyncio
    async def test_no_browser_session_errors_instead_of_handing_over_a_blank_tab(
        self, monkeypatch, pooled
    ) -> None:
        # The agent called this before browsing anywhere. Starting a session to
        # hand the user a blank tab bills an AgentCore browser and pauses the
        # turn to achieve nothing.
        context = FakeContext(FakeAgent())

        async def _must_not_acquire(_agent):
            raise AssertionError("must not start a session just to hand it over")

        monkeypatch.setattr(session_pool, "has_session", lambda _agent: False)
        monkeypatch.setattr(session_pool, "acquire", _must_not_acquire)

        result = await _call(request_user_login, tool_context=context, reason="Sign in.")

        assert result["status"] == "error"
        assert "browse_web" in result["content"][0]["text"]
        assert context.reasons == []

    @pytest.mark.asyncio
    async def test_a_missing_reason_is_rejected_before_anything_is_touched(
        self, pooled
    ) -> None:
        result = await _call(
            request_user_login, tool_context=FakeContext(FakeAgent()), reason="  "
        )

        assert result["status"] == "error"
        assert pooled.client.stream == []

    @pytest.mark.asyncio
    async def test_kill_switch_short_circuits_without_touching_the_browser(
        self, monkeypatch, pooled
    ) -> None:
        monkeypatch.setenv("BROWSER_TAKEOVER_ENABLED", "false")
        context = FakeContext(FakeAgent())

        result = await _call(request_user_login, tool_context=context, reason="Sign in.")

        assert result["status"] == "error"
        assert pooled.client.stream == []
        assert context.reasons == []

    @pytest.mark.asyncio
    async def test_the_tool_takes_no_credential_argument(self) -> None:
        # Structural, and deliberately so: a credential as a tool argument
        # would land in the prompt, in Memory and in the cacheable prefix, and
        # be re-read every turn for the life of the conversation.
        import inspect

        inner = getattr(request_user_login, "_tool_func", None) or request_user_login
        params = set(inspect.signature(inner).parameters)

        assert params == {"tool_context", "reason"}
        assert not params & {"username", "password", "credentials", "secret", "token"}


class TestRegistrationAndCatalog:
    """The two gates: a kill switch, and a catalog entry granted per role."""

    def test_registered_by_default(self, monkeypatch) -> None:
        monkeypatch.delenv("BROWSER_TAKEOVER_ENABLED", raising=False)
        from agents.main_agent.tools.tool_registry import create_default_registry

        assert create_default_registry().has_tool("request_user_login")

    def test_absent_when_disabled(self, monkeypatch) -> None:
        monkeypatch.setenv("BROWSER_TAKEOVER_ENABLED", "false")
        from agents.main_agent.tools.tool_registry import create_default_registry

        assert not create_default_registry().has_tool("request_user_login")

    def test_it_is_its_own_catalog_entry_not_a_browse_web_action(self) -> None:
        # RBAC granularity is exactly one tool_id: an action on `browse_web`
        # would ship an interactive browser in our AWS account to everyone who
        # can browse. Separate entry, granted separately, zero prefix tokens
        # for anyone without the grant.
        from agents.main_agent.tools.tool_catalog import TOOL_CATALOG

        assert "request_user_login" in TOOL_CATALOG
        assert TOOL_CATALOG["request_user_login"].tool_id == "request_user_login"
        assert "browse_web" in TOOL_CATALOG
        assert TOOL_CATALOG["request_user_login"] is not TOOL_CATALOG["browse_web"]


class TestViewportFidelity:
    """DCV crops unless remoteWidth/remoteHeight match the real session."""

    @pytest.mark.asyncio
    async def test_the_reported_viewport_is_the_one_the_session_started_with(
        self, monkeypatch, pooled
    ) -> None:
        # A session can outlive a config change, or be reconnected to from a
        # container whose DEFAULT_VIEWPORT differs. Reporting today's default
        # rather than the session's own would crop the stream.
        pooled.viewport = {"width": 1600, "height": 900}
        monkeypatch.setattr(
            session_pool, "DEFAULT_VIEWPORT", {"width": 1280, "height": 800}
        )

        ref = await session_pool.take_control(FakeAgent())

        assert ref["viewport"] == {"width": 1600, "height": 900}


class TestSessionUrlPolicy:
    """The Chromium URL policy every session is started with (spec D6).

    ⚠️ Session-level policies are RECOMMENDED-only — the service rejects
    MANAGED — so this constrains the *agent*, not a *human* holding the browser
    during a takeover. The real control needs `CreateBrowser`. These assert the
    policy is well-formed, is never MANAGED (which breaks every session), and
    that a misconfiguration is loud rather than silently permissive.
    """

    def test_a_policy_is_built_from_the_s3_uri(self, monkeypatch) -> None:
        monkeypatch.setenv("BROWSER_POLICY_S3", "s3://my-bucket/policies/managed.json")

        policies = session_pool._enterprise_policies()

        assert policies == [
            {
                "type": "RECOMMENDED",
                "location": {
                    "s3": {"bucket": "my-bucket", "prefix": "policies/managed.json"}
                },
            }
        ]

    def test_the_session_policy_is_never_managed(self, monkeypatch) -> None:
        # Measured on dev 2026-09-19: the API's enum accepts MANAGED but the
        # service rejects it —
        #   "Invalid value for parameter 'type'. MANAGED is not supported for
        #    session-level policies."
        # It does not degrade: StartBrowserSession fails outright, so EVERY
        # browser session dies and browse_web stops working. This test exists
        # so nobody "tries MANAGED" here again.
        monkeypatch.setenv("BROWSER_POLICY_S3", "s3://b/k.json")

        assert session_pool._enterprise_policies()[0]["type"] == "RECOMMENDED"

    def test_a_key_with_slashes_survives_intact(self, monkeypatch) -> None:
        monkeypatch.setenv("BROWSER_POLICY_S3", "s3://b/a/b/c/managed.json")

        prefix = session_pool._enterprise_policies()[0]["location"]["s3"]["prefix"]
        assert prefix == "a/b/c/managed.json"

    @pytest.mark.parametrize(
        "value", ["", "   ", "my-bucket/key.json", "https://example.com/p.json", "s3://", "s3://bucket-only"]
    )
    def test_an_unusable_setting_yields_no_policy(self, monkeypatch, value) -> None:
        monkeypatch.setenv("BROWSER_POLICY_S3", value)

        assert session_pool._enterprise_policies() is None

    def test_a_malformed_setting_is_logged_as_an_error(self, monkeypatch, caplog) -> None:
        # Silently starting an unrestricted browser is the failure mode that
        # matters here, so it must be loud in the logs.
        monkeypatch.setenv("BROWSER_POLICY_S3", "not-a-uri")

        with caplog.at_level(logging.ERROR):
            session_pool._enterprise_policies()

        assert "NO url policy" in caplog.text

    @pytest.mark.asyncio
    async def test_start_passes_the_policy_through(self, monkeypatch) -> None:
        monkeypatch.setenv("BROWSER_POLICY_S3", "s3://my-bucket/policies/managed.json")
        captured: Dict[str, Any] = {}

        class RecordingClient:
            def __init__(self, **_: Any) -> None:
                pass

            def start(self, **kwargs: Any) -> str:
                captured.update(kwargs)
                return "bs-new"

        monkeypatch.setattr(
            "bedrock_agentcore.tools.browser_client.BrowserClient", RecordingClient
        )

        await session_pool._start_remote_session()

        assert captured["enterprise_policies"][0]["type"] == "RECOMMENDED"
        assert captured["viewport"] == session_pool.DEFAULT_VIEWPORT

    @pytest.mark.asyncio
    async def test_start_omits_the_kwarg_entirely_when_unconfigured(
        self, monkeypatch
    ) -> None:
        # An older SDK or a local dev box must not get `enterprise_policies=None`
        # forwarded into the start call.
        monkeypatch.delenv("BROWSER_POLICY_S3", raising=False)
        captured: Dict[str, Any] = {}

        class RecordingClient:
            def __init__(self, **_: Any) -> None:
                pass

            def start(self, **kwargs: Any) -> str:
                captured.update(kwargs)
                return "bs-new"

        monkeypatch.setattr(
            "bedrock_agentcore.tools.browser_client.BrowserClient", RecordingClient
        )

        await session_pool._start_remote_session()

        assert "enterprise_policies" not in captured


# ---------------------------------------------------------------------------
# Sizing the handover (BROWSER_TAKEOVER_FIT_ENABLED)
# ---------------------------------------------------------------------------


class RecordingClient(FakeClient):
    """A `FakeClient` that also writes its stream flips to a shared log."""

    def __init__(self, events: List[str]) -> None:
        super().__init__()
        self.events = events

    def take_control(self) -> None:
        super().take_control()
        self.events.append("take_control")

    def release_control(self) -> None:
        super().release_control()
        self.events.append("release_control")


@dataclass
class SizingCdp(FakeCdp):
    """A CDP socket that records window resizes into the same log."""

    events: List[str] = field(default_factory=list)
    fail_sizes: bool = False

    async def set_window_size(self, width: int, height: int) -> None:
        if self.closed:
            raise RuntimeError("CDP session is closed")
        if self.fail_sizes:
            raise RuntimeError("Browser.setWindowBounds failed")
        self.events.append(f"window {width}x{height}")


@pytest.fixture
def fitted(monkeypatch, pooled):
    """`pooled`, with the fit switch on and every CDP/stream call logged.

    The order is the whole point: CDP is refused (HTTP 403) while the stream is
    DISABLED, so a resize after `take_control` or before `release_control`
    cannot land.
    """
    monkeypatch.setenv("BROWSER_TAKEOVER_FIT_ENABLED", "true")
    events: List[str] = []
    pooled.client = RecordingClient(events)
    pooled.cdp = SizingCdp(events=events)  # type: ignore[assignment]
    connects: List[Any] = []

    async def _connect(client: Any):
        connects.append(client)
        events.append("connect")
        return SizingCdp(events=events)

    async def _never(*_a: Any, **_k: Any):
        raise AssertionError("started a second browser")

    monkeypatch.setattr(session_pool, "_connect", _connect)
    monkeypatch.setattr(session_pool, "_start_remote_session", _never)
    pooled.events = events  # type: ignore[attr-defined]
    pooled.connects = connects  # type: ignore[attr-defined]
    return pooled


class TestWindowFitForTakeover:
    @pytest.mark.asyncio
    async def test_the_window_is_fitted_before_the_stream_is_disabled(
        self, fitted
    ) -> None:
        ref = await session_pool.take_control(FakeAgent())

        assert fitted.events == ["window 768x832", "take_control"]
        # The viewer pins DCV's display to this, so it must be the window's
        # real size, not the agent's.
        assert ref["viewport"] == {"width": 768, "height": 832}
        assert fitted.takeover_viewport == {"width": 768, "height": 832}
        # The agent's own size is kept apart, for the restore.
        assert fitted.viewport == session_pool.DEFAULT_VIEWPORT

    @pytest.mark.asyncio
    async def test_the_agent_window_is_restored_after_the_stream_is_re_enabled(
        self, fitted
    ) -> None:
        agent = FakeAgent()
        await session_pool.take_control(agent)
        old_socket = fitted.cdp
        old_socket.closed = True  # the service kills it on DISABLED

        assert await session_pool.release_control(agent) is True

        assert fitted.events == [
            "window 768x832",
            "take_control",
            "release_control",
            "connect",
            "window 1280x800",
        ]
        # Reconnected to the SAME browser, where the user's login now lives,
        # and the fresh socket is pooled for the agent's next browse.
        assert fitted.connects == [fitted.client]
        assert fitted.cdp is not old_socket and fitted.cdp.closed is False
        assert fitted.takeover_viewport is None

    @pytest.mark.asyncio
    async def test_a_failed_restore_does_not_fail_the_handback(
        self, fitted, monkeypatch, caplog
    ) -> None:
        agent = FakeAgent()
        await session_pool.take_control(agent)

        async def _refused(_client: Any):
            raise RuntimeError("HTTP 403")

        monkeypatch.setattr(session_pool, "_connect", _refused)

        with caplog.at_level(logging.WARNING):
            assert await session_pool.release_control(agent) is True
        assert fitted.client.stream == ["DISABLED", "ENABLED"]
        assert "could not restore the agent window" in caplog.text

    @pytest.mark.asyncio
    async def test_a_failed_fit_still_hands_over_at_the_agent_size(
        self, fitted, caplog
    ) -> None:
        agent = FakeAgent()
        fitted.cdp.fail_sizes = True

        with caplog.at_level(logging.WARNING):
            ref = await session_pool.take_control(agent)

        assert fitted.client.stream == ["DISABLED"]
        assert ref["viewport"] == session_pool.DEFAULT_VIEWPORT
        assert "could not fit session" in caplog.text

        # Nothing was refit, so nothing is restored: no reconnect, no resize.
        await session_pool.release_control(agent)
        assert fitted.connects == []
        assert fitted.events == ["take_control", "release_control"]

    @pytest.mark.asyncio
    async def test_a_refused_handover_puts_the_window_back(self, fitted) -> None:
        fitted.client.take_fails = True

        with pytest.raises(RuntimeError):
            await session_pool.take_control(FakeAgent())

        # The agent keeps the browser, so it must keep its own window size.
        assert fitted.events == ["window 768x832", "window 1280x800"]
        assert fitted.takeover_viewport is None

    @pytest.mark.asyncio
    async def test_the_resume_pass_does_not_refit(self, fitted) -> None:
        agent = FakeAgent()
        await session_pool.take_control(agent)
        again = await session_pool.take_control(agent)

        # CDP is refused by now; a second resize would only log a failure.
        assert fitted.events == ["window 768x832", "take_control"]
        assert again["viewport"] == {"width": 768, "height": 832}

    @pytest.mark.asyncio
    async def test_a_handback_in_another_container_restores_by_reconnecting(
        self, fitted, monkeypatch
    ) -> None:
        agent = FakeAgent()
        session_pool._write_state(
            agent,
            {"sessionId": "bs-1", "identifier": "browser-abc", "viewport": {"width": 1280, "height": 800}},
        )
        session_pool._live.clear()
        remote = RecordingClient(fitted.events)
        monkeypatch.setattr(
            "bedrock_agentcore.tools.browser_client.BrowserClient",
            lambda **_: remote,
        )

        assert await session_pool.release_control(agent) is True

        assert fitted.events == ["release_control", "connect", "window 1280x800"]
        assert "bs-1" in session_pool._live

    @pytest.mark.asyncio
    async def test_switch_off_leaves_the_window_alone(
        self, fitted, monkeypatch
    ) -> None:
        monkeypatch.setenv("BROWSER_TAKEOVER_FIT_ENABLED", "false")
        agent = FakeAgent()

        ref = await session_pool.take_control(agent)
        await session_pool.release_control(agent)

        assert ref["viewport"] == session_pool.DEFAULT_VIEWPORT
        assert fitted.events == ["take_control", "release_control"]
        assert fitted.connects == []

    @pytest.mark.asyncio
    async def test_the_tool_pauses_on_the_fitted_size_and_restores_on_resume(
        self, fitted
    ) -> None:
        agent = FakeAgent()
        context = FakeContext(agent)
        with pytest.raises(InterruptException):
            await _call(request_user_login, tool_context=context, reason="Sign in.")

        assert context.reasons[0]["viewport"] == {"width": 768, "height": 832}

        fitted.cdp.closed = True
        result = await _call(
            request_user_login,
            tool_context=FakeContext(agent, response={"completed": True}),
            reason="Sign in.",
        )

        assert result["status"] == "success"
        assert fitted.events[-3:] == ["release_control", "connect", "window 1280x800"]


class TestSnapDisplaySize:
    """DCV takes widths >= 640 and snaps both sides down to multiples of 8."""

    @pytest.mark.parametrize(
        ("requested", "snapped"),
        [
            ((768, 832), (768, 832)),
            ((600, 900), (640, 896)),
            ((1001, 700), (1000, 696)),
            ((800, 400), (800, 600)),
            ((800, 1500), (800, 1000)),
        ],
    )
    def test_sizes_are_what_the_display_will_accept(self, requested, snapped) -> None:
        size = session_pool.snap_display_size(*requested)
        assert (size["width"], size["height"]) == snapped

    def test_the_takeover_size_survives_snapping_unchanged(self) -> None:
        size = session_pool.snap_display_size(**session_pool.TAKEOVER_VIEWPORT)
        assert size == session_pool.TAKEOVER_VIEWPORT
