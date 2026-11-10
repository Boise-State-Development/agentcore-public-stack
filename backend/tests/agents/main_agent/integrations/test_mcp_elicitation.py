"""The elicitation callback, middleware and capability ad, unit by unit.

The end-to-end behaviour (pause, resume, replay over a real wire) is in
``test_mcp_elicitation_real_wire.py``; these pin the edges it cannot reach
cheaply: a request no call is waiting on, URL mode, a form we cannot render,
the log filter, and which clients get a callback at all.
"""

from __future__ import annotations

import builtins
import logging
from types import SimpleNamespace
from typing import Any, AsyncIterator

import anyio
import pytest
from mcp.types import ElicitRequestFormParams, ElicitRequestURLParams, ElicitResult, ErrorData
from mcp.types import Tool as MCPTool
from strands.interrupt import Interrupt, InterruptException
from strands.tools.mcp import MCPAgentTool
from strands.types.events import ToolResultEvent

from agents.main_agent.integrations import mcp_elicitation as elicitation
from agents.main_agent.integrations.mcp_apps import _UIExtensionClientSession
from agents.main_agent.integrations.mcp_elicitation import (
    CallElicitationSlot,
    ElicitationPending,
    _DropPendingTraceback,
    elicitation_callback,
    elicitation_middleware,
    handles,
)
from apis.shared.mcp_elicitation.forms import interrupt_name
from apis.shared.tools.models import MCPServerConfig

SCHEMA = {
    "type": "object",
    "properties": {"repository_name": {"type": "string"}},
    "required": ["repository_name"],
}


def _form_params(schema=SCHEMA, message="Type it to confirm.") -> ElicitRequestFormParams:
    return ElicitRequestFormParams(message=message, requested_schema=schema)


def _context(key: str = "confirm") -> Any:
    return SimpleNamespace(request_id=key)


async def _call(slot, params, key="confirm"):
    token = elicitation._SLOT.set(slot)
    try:
        return await elicitation_callback(_context(key), params)
    finally:
        elicitation._SLOT.reset(token)


class TestCallback:
    @pytest.mark.asyncio
    async def test_a_request_no_call_waits_on_is_refused_like_the_sdk_default(self):
        result = await elicitation_callback(_context(), _form_params())

        assert isinstance(result, ErrorData)
        assert result.message == "Elicitation not supported"

    @pytest.mark.asyncio
    async def test_url_mode_is_refused(self):
        slot = CallElicitationSlot(lookup=lambda name: None)
        params = ElicitRequestURLParams(
            mode="url", message="Sign in", url="https://example.com", elicitation_id="e1"
        )

        result = await _call(slot, params)

        assert isinstance(result, ErrorData)
        assert slot.pending is None

    @pytest.mark.asyncio
    async def test_an_unanswered_form_ends_the_call_and_is_recorded(self):
        slot = CallElicitationSlot(lookup=lambda name: None)

        with pytest.raises(ElicitationPending):
            await _call(slot, _form_params())

        assert slot.pending.name == interrupt_name(0, "confirm", SCHEMA)
        assert slot.pending.form.message == "Type it to confirm."

    @pytest.mark.asyncio
    async def test_a_stored_answer_is_returned_to_the_server(self):
        answers = {
            interrupt_name(0, "confirm", SCHEMA): {
                "action": "accept",
                "content": {"repository_name": "acme/scratch"},
            }
        }
        slot = CallElicitationSlot(lookup=answers.get)

        result = await _call(slot, _form_params())

        assert result == ElicitResult(action="accept", content={"repository_name": "acme/scratch"})

    @pytest.mark.asyncio
    async def test_a_decline_is_passed_on(self):
        slot = CallElicitationSlot(lookup=lambda name: {"action": "decline"})
        assert await _call(slot, _form_params()) == ElicitResult(action="decline")

    @pytest.mark.asyncio
    async def test_an_answer_that_does_not_fit_becomes_a_cancel(self):
        slot = CallElicitationSlot(
            lookup=lambda name: {"action": "accept", "content": {"other": "x"}}
        )
        assert await _call(slot, _form_params()) == ElicitResult(action="cancel")

    @pytest.mark.asyncio
    async def test_a_form_we_cannot_render_is_cancelled_and_flagged(self):
        slot = CallElicitationSlot(lookup=lambda name: None)
        nested = {"type": "object", "properties": {"x": {"type": "object"}}}

        result = await _call(slot, _form_params(schema=nested))

        assert result == ElicitResult(action="cancel")
        assert slot.unsupported is True
        assert slot.pending is None

    @pytest.mark.asyncio
    async def test_a_changed_schema_is_asked_again_not_answered_from_the_old_one(self):
        old = {interrupt_name(0, "confirm", SCHEMA): {"action": "accept", "content": {"repository_name": "a"}}}
        slot = CallElicitationSlot(lookup=old.get)
        changed = {
            "type": "object",
            "properties": {"repository_name": {"type": "string", "maxLength": 3}},
        }

        with pytest.raises(ElicitationPending):
            await _call(slot, _form_params(schema=changed))


class TestRounds:
    def test_a_key_seen_again_starts_the_next_round(self):
        slot = CallElicitationSlot(lookup=lambda name: None)

        first = slot.next_name("k", SCHEMA)
        other = slot.next_name("j", SCHEMA)
        again = slot.next_name("k", SCHEMA)

        assert first == interrupt_name(0, "k", SCHEMA)
        assert other == interrupt_name(0, "j", SCHEMA)
        assert again == interrupt_name(1, "k", SCHEMA)


def _mcp_tool(client) -> MCPAgentTool:
    return MCPAgentTool(
        MCPTool(name="delete_repository", input_schema={"type": "object", "properties": {}}),
        client,
    )


class TestHandles:
    def test_only_tools_from_an_opted_in_client(self):
        assert handles(_mcp_tool(SimpleNamespace(elicitation_enabled=True)))
        assert not handles(_mcp_tool(SimpleNamespace(elicitation_enabled=False)))
        assert not handles(_mcp_tool(SimpleNamespace()))
        assert not handles(SimpleNamespace(tool_name="ask_user_question"))
        assert not handles(None)


def _middleware_context(tool, answers=None):
    answers = answers or {}

    def interrupt(name, *, reason=None, response=None):
        if name in answers:
            return SimpleNamespace(response=answers[name])
        raise InterruptException(Interrupt(id=f"v1:{name}", name=name, reason=reason))

    return SimpleNamespace(
        tool=tool, tool_use={"toolUseId": "tu-1", "name": "gh___delete_repository"}, interrupt=interrupt
    )


async def _drain(context, next_fn) -> list:
    return [event async for event in elicitation_middleware(context, next_fn)]


class TestMiddleware:
    @pytest.mark.asyncio
    async def test_other_tools_pass_straight_through(self):
        result = ToolResultEvent({"toolUseId": "tu-1", "status": "success", "content": []})
        seen = []

        async def next_fn(ctx) -> AsyncIterator[Any]:
            seen.append(elicitation._SLOT.get())
            yield result

        events = await _drain(_middleware_context(SimpleNamespace()), next_fn)

        assert events == [result]
        assert seen == [None]

    @pytest.mark.asyncio
    async def test_a_recorded_form_raises_the_interrupt_with_the_form_as_reason(self):
        client = SimpleNamespace(elicitation_enabled=True, context_label="GitHub")

        async def next_fn(ctx) -> AsyncIterator[Any]:
            with pytest.raises(ElicitationPending):
                await elicitation_callback(_context(), _form_params())
            yield ToolResultEvent({"toolUseId": "tu-1", "status": "error", "content": []})

        with pytest.raises(InterruptException) as raised:
            await _drain(_middleware_context(_mcp_tool(client)), next_fn)

        reason = raised.value.interrupt.reason
        assert reason["type"] == "mcp_elicitation_required"
        assert reason["toolUseId"] == "tu-1"
        assert reason["toolName"] == "gh___delete_repository"
        assert reason["serverName"] == "GitHub"
        assert elicitation._SLOT.get() is None

    @pytest.mark.asyncio
    async def test_a_form_we_cannot_render_tells_the_model_why(self):
        client = SimpleNamespace(elicitation_enabled=True, context_label="GitHub")
        nested = {"type": "object", "properties": {"x": {"type": "object"}}}

        async def next_fn(ctx) -> AsyncIterator[Any]:
            await elicitation_callback(_context(), _form_params(schema=nested))
            yield ToolResultEvent(
                {"toolUseId": "tu-1", "status": "error", "content": [{"text": "cancelled"}]}
            )

        (event,) = await _drain(_middleware_context(_mcp_tool(client)), next_fn)

        texts = [block["text"] for block in event.tool_result["content"]]
        assert texts[0] == "cancelled"
        assert "GitHub asked the user for input in a form this app cannot show" in texts[1]


class TestLogFilter:
    def _record(self, exc) -> logging.LogRecord:
        return logging.LogRecord("strands", logging.ERROR, __file__, 1, "tool execution failed", (), (type(exc), exc, None))

    def test_a_pause_is_not_logged_as_a_failure(self):
        assert _DropPendingTraceback().filter(self._record(ElicitationPending("n"))) is False

    def test_a_pause_inside_an_exception_group_is_dropped_too(self):
        group = builtins.ExceptionGroup("tasks", [ElicitationPending("n")])
        assert _DropPendingTraceback().filter(self._record(group)) is False

    def test_a_real_failure_is_still_logged(self):
        assert _DropPendingTraceback().filter(self._record(RuntimeError("boom"))) is True
        plain = logging.LogRecord("strands", logging.INFO, __file__, 1, "hello", (), None)
        assert _DropPendingTraceback().filter(plain) is True


def _session(**kwargs) -> _UIExtensionClientSession:
    _send_a, recv_a = anyio.create_memory_object_stream(1)
    send_b, _recv_b = anyio.create_memory_object_stream(1)
    return _UIExtensionClientSession(recv_a, send_b, **kwargs)


class TestCapabilityAd:
    def test_form_only_on_2026_07_28(self):
        caps = _session(elicitation_callback=elicitation_callback)._build_capabilities("2026-07-28")
        assert caps.model_dump(by_alias=True, exclude_none=True)["elicitation"] == {"form": {}}

    def test_nothing_on_a_legacy_handshake(self):
        caps = _session(elicitation_callback=elicitation_callback)._build_capabilities("2025-11-25")
        assert caps.elicitation is None

    @pytest.mark.parametrize("version", ["2025-11-25", "2026-07-28"])
    def test_without_a_callback_the_ad_is_unchanged(self, version):
        from mcp.client.session import ClientSession

        ours = _session()._build_capabilities(version)
        _send_a, recv_a = anyio.create_memory_object_stream(1)
        send_b, _recv_b = anyio.create_memory_object_stream(1)
        sdks = ClientSession(recv_a, send_b)._build_capabilities(version)

        assert ours.elicitation is None
        assert ours.model_dump(exclude={"extensions"}) == sdks.model_dump(exclude={"extensions"})


def _config(opted_in: bool) -> MCPServerConfig:
    return MCPServerConfig(
        server_url="https://api.githubcopilot.com/mcp/",
        auth_type="none",
        elicitation_enabled=opted_in,
    )


class TestWhichClientsGetACallback:
    @pytest.mark.parametrize(
        ("flag", "opted_in", "expected"),
        [("true", True, True), ("true", False, False), ("", True, False), ("false", True, False)],
    )
    def test_the_flag_and_the_server_must_both_say_yes(self, monkeypatch, flag, opted_in, expected):
        from agents.main_agent.integrations.external_mcp_client import create_external_mcp_client

        monkeypatch.setenv("MCP_ELICITATION_ENABLED", flag)
        client = create_external_mcp_client(_config(opted_in), oauth_token="tok")

        assert client.elicitation_enabled is expected
        assert (client._elicitation_callback is elicitation_callback) is expected


class TestServerConfig:
    def test_the_setting_round_trips(self):
        stored = _config(True).to_dict()
        assert stored["elicitationEnabled"] is True
        assert MCPServerConfig.from_dict(stored).elicitation_enabled is True

    def test_an_existing_row_is_unchanged(self):
        stored = _config(False).to_dict()
        assert "elicitationEnabled" not in stored
        assert MCPServerConfig.from_dict(stored).elicitation_enabled is False

    def test_the_admin_payloads_carry_it(self):
        from apis.shared.tools.models import MCPServerConfigRequest, MCPServerConfigResponse

        request = MCPServerConfigRequest.model_validate(
            {"serverUrl": "https://api.githubcopilot.com/mcp/", "elicitationEnabled": True}
        )
        assert request.to_model().elicitation_enabled is True
        response = MCPServerConfigResponse.from_model(_config(True))
        assert response.model_dump(by_alias=True)["elicitationEnabled"] is True
