"""Client-executed tools: the spec model, the interrupting tool, the result coercion.

What matters here, in order of how badly it would go wrong:

* The spec list is prompt input from a client, so its bounds are enforced
  before it reaches ``toolConfig``.
* The tool body must run the client's answer back as the tool result on resume
  and must never raise, since an exception out of a resumed tool strands the
  turn paused forever.
* A turn with no client tools must key and behave exactly as before the field
  existed; the web app never sends it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from apis.shared.client_tools import (
    MAX_CLIENT_TOOLS,
    MAX_DESCRIPTION_CHARS,
    MAX_RESULT_CHARS,
    ClientToolCallEvent,
    ClientToolSpec,
    build_client_tools,
    client_tools_digest,
    coerce_result,
    validate_client_tools,
)


def spec(name: str = "run_command", **overrides) -> ClientToolSpec:
    fields = {
        "name": name,
        "description": "Run a shell command on the user's machine.",
        "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    }
    fields.update(overrides)
    return ClientToolSpec.model_validate(fields)


class TestSpecBounds:
    def test_a_well_formed_spec_round_trips(self):
        s = spec()
        assert s.wire_name == "local_run_command"
        assert s.model_dump(by_alias=True)["inputSchema"]["type"] == "object"

    @pytest.mark.parametrize("bad", ["", "1abc", "has-dash", "has space", "x" * 65, "ünïcode"])
    def test_names_outside_the_bedrock_shape_are_rejected(self, bad):
        with pytest.raises(ValidationError):
            spec(name=bad)

    def test_description_is_required_and_bounded(self):
        with pytest.raises(ValidationError):
            spec(description="   ")
        with pytest.raises(ValidationError):
            spec(description="x" * (MAX_DESCRIPTION_CHARS + 1))
        assert spec(description="  ok  ").description == "ok"

    def test_schema_must_be_an_object_schema(self):
        with pytest.raises(ValidationError):
            spec(inputSchema={"type": "string"})

    def test_schema_size_is_capped(self):
        huge = {"type": "object", "properties": {f"p{i}": {"type": "string", "description": "d" * 100} for i in range(200)}}
        with pytest.raises(ValidationError):
            spec(inputSchema=huge)

    def test_unknown_fields_are_rejected(self):
        with pytest.raises(ValidationError):
            spec(extra="nope")

    def test_default_schema_is_an_empty_object(self):
        s = ClientToolSpec(name="noop", description="Does nothing.")
        assert s.input_schema == {"type": "object", "properties": {}}


class TestListValidation:
    def test_none_and_empty_are_empty(self):
        assert validate_client_tools(None) == []
        assert validate_client_tools([]) == []

    def test_count_is_capped(self):
        with pytest.raises(ValueError, match=str(MAX_CLIENT_TOOLS)):
            validate_client_tools([spec(f"t{i}") for i in range(MAX_CLIENT_TOOLS + 1)])

    def test_duplicates_are_rejected(self):
        with pytest.raises(ValueError, match="twice"):
            validate_client_tools([spec("a"), spec("a")])


class TestDigest:
    def test_no_tools_is_the_empty_string(self):
        assert client_tools_digest(None) == ""
        assert client_tools_digest([]) == ""

    def test_order_independent_and_content_sensitive(self):
        a, b = spec("a"), spec("b")
        assert client_tools_digest([a, b]) == client_tools_digest([b, a])
        assert client_tools_digest([a]) != client_tools_digest([a, b])
        assert client_tools_digest([spec("a")]) != client_tools_digest([spec("a", description="Different.")])


class TestResultCoercion:
    def test_dict_shape(self):
        r = coerce_result({"output": "hi", "isError": True})
        assert r.output == "hi" and r.is_error and not r.declined

    def test_bare_string(self):
        assert coerce_result("plain").output == "plain"

    def test_declined(self):
        assert coerce_result({"declined": True}).declined

    def test_garbage_becomes_an_error_result_not_an_exception(self):
        r = coerce_result(42)
        assert r.is_error and "unreadable" in r.output

    def test_output_is_truncated(self):
        r = coerce_result({"output": "x" * (MAX_RESULT_CHARS + 500)})
        assert len(r.output) < MAX_RESULT_CHARS + 100
        assert "truncated" in r.output


class _Ctx:
    """A ToolContext double: `interrupt()` returns the client's response."""

    def __init__(self, response):
        self.tool_use = {"toolUseId": "tu-1"}
        self.response = response
        self.calls: list = []

    def interrupt(self, name, reason=None, response=None):
        self.calls.append((name, reason))
        return self.response


def _body(tool):
    return tool._tool_func if hasattr(tool, "_tool_func") else tool.original_function


class TestBuiltTool:
    def test_none_builds_nothing(self):
        assert build_client_tools(None) == []

    def test_spec_reaches_the_tool_config_with_the_prefix(self):
        [tool] = build_client_tools([spec()])
        assert tool.tool_name == "local_run_command"
        assert tool.tool_spec["inputSchema"]["json"]["required"] == ["command"]
        assert "user's own machine" in tool.tool_spec["description"]

    def test_interrupt_reason_names_the_client_tool_and_carries_the_arguments(self):
        [tool] = build_client_tools([spec()])
        ctx = _Ctx({"output": "ok"})
        _body(tool)(tool_context=ctx, command="ls -la")
        [(name, reason)] = ctx.calls
        assert name == "client_tool_call"
        assert reason == {"type": "client_tool_call", "toolUseId": "tu-1", "toolName": "run_command", "arguments": {"command": "ls -la"}}

    def test_the_clients_output_is_the_tool_result(self):
        [tool] = build_client_tools([spec()])
        out = _body(tool)(tool_context=_Ctx({"output": "file1\nfile2\n"}), command="ls")
        assert out == {"status": "success", "content": [{"text": "file1\nfile2\n"}]}

    def test_an_error_result_is_an_error_status(self):
        [tool] = build_client_tools([spec()])
        out = _body(tool)(tool_context=_Ctx({"output": "exit 1", "isError": True}), command="false")
        assert out["status"] == "error"

    def test_declining_tells_the_model_not_to_retry(self):
        [tool] = build_client_tools([spec()])
        out = _body(tool)(tool_context=_Ctx({"declined": True}), command="rm -rf /")
        assert out["status"] == "error"
        assert "declined" in out["content"][0]["text"]
        assert "Do not retry" in out["content"][0]["text"]

    def test_empty_output_is_said_rather_than_blank(self):
        [tool] = build_client_tools([spec()])
        out = _body(tool)(tool_context=_Ctx({"output": ""}), command="true")
        assert out["content"][0]["text"] == "(no output)"


class TestEvent:
    def test_sse_shape(self):
        sse = ClientToolCallEvent(interrupt_id="i1", tool_use_id="tu-1", tool_name="run_command", arguments={"command": "ls"}).to_sse_format()
        assert sse.startswith("event: client_tool_call\n")
        payload = json.loads(sse.split("data: ", 1)[1])
        assert payload == {
            "type": "client_tool_call",
            "interruptId": "i1",
            "toolUseId": "tu-1",
            "toolName": "run_command",
            "arguments": {"command": "ls"},
        }


class TestInvocationRequest:
    def test_web_requests_carry_none(self):
        from apis.inference_api.chat.models import InvocationRequest

        assert InvocationRequest(session_id="s", message="m").client_tools is None

    def test_specs_are_validated_on_the_request(self):
        from apis.inference_api.chat.models import InvocationRequest

        req = InvocationRequest(session_id="s", message="m", client_tools=[spec().model_dump(by_alias=True)])
        assert req.client_tools and req.client_tools[0].wire_name == "local_run_command"
        with pytest.raises(ValidationError):
            InvocationRequest(session_id="s", message="m", client_tools=[spec("a").model_dump(by_alias=True)] * 2)

    def test_an_empty_list_is_none(self):
        from apis.inference_api.chat.models import InvocationRequest

        assert InvocationRequest(session_id="s", message="m", client_tools=[]).client_tools is None


class TestCacheKey:
    BASE = dict(
        session_id="s",
        user_id="u",
        enabled_tools=["t"],
        model_id="m",
        inference_params={},
        system_prompt=None,
        caching_enabled=False,
        provider="bedrock",
        freshness_hash="f",
        agent_type="chat",
    )

    def test_no_digest_keys_as_before(self):
        from apis.inference_api.chat import service

        assert service._create_cache_key(**self.BASE) == service._create_cache_key(**self.BASE, client_tools_digest="")

    def test_a_digest_changes_only_the_tools_element(self):
        from apis.inference_api.chat import service

        plain = service._create_cache_key(**self.BASE)
        with_tools = service._create_cache_key(**self.BASE, client_tools_digest="abc")
        assert len(plain) == len(with_tools)
        assert [i for i, (a, b) in enumerate(zip(plain, with_tools)) if a != b] == [2]


class TestSnapshotRoundTrip:
    def test_resume_rebuilds_the_tools_from_the_snapshot(self):
        from apis.inference_api.chat.routes import _client_tools_digest_from_snapshot, _client_tools_from_snapshot

        snapshot = SimpleNamespace(client_tools=json.dumps([spec().model_dump(by_alias=True)]))
        tools = _client_tools_from_snapshot(snapshot)
        assert tools and tools[0].tool_name == "local_run_command"
        assert _client_tools_digest_from_snapshot(snapshot) == client_tools_digest([spec()])

    def test_a_snapshot_without_tools_rebuilds_none(self):
        from apis.inference_api.chat.routes import _client_tools_digest_from_snapshot, _client_tools_from_snapshot

        assert _client_tools_from_snapshot(SimpleNamespace(client_tools=None)) is None
        assert _client_tools_digest_from_snapshot(SimpleNamespace()) == ""

    def test_a_corrupt_snapshot_misses_rather_than_raising(self):
        from apis.inference_api.chat.routes import _client_tools_from_snapshot

        assert _client_tools_from_snapshot(SimpleNamespace(client_tools="not json")) is None

    def test_pending_interrupt_accepts_the_new_kind(self):
        from apis.shared.sessions.models import PendingInterrupt

        p = PendingInterrupt(interrupt_id="i", kind="client_tool", tool_name="run_command", tool_input="{}", created_at="t")
        assert p.kind == "client_tool"


class TestSnapshotRecording:
    """`_record_client_tools_on` is what makes resume possible: it is the only
    writer of the field the resume path reads. Round-trip the two."""

    def test_record_then_rebuild_yields_the_same_tools_and_digest(self):
        from apis.inference_api.chat.models import InvocationRequest
        from apis.inference_api.chat.routes import (
            _client_tools_digest,
            _client_tools_digest_from_snapshot,
            _client_tools_from_snapshot,
            _record_client_tools_on,
        )

        request = InvocationRequest(
            session_id="s", message="m", client_tools=[spec().model_dump(by_alias=True), spec("read_file").model_dump(by_alias=True)]
        )
        agent = SimpleNamespace(_construction_snapshot={"model_id": "m"})
        _record_client_tools_on(agent, request)

        snapshot = SimpleNamespace(client_tools=agent._construction_snapshot["client_tools"])
        rebuilt = _client_tools_from_snapshot(snapshot)
        assert [t.tool_name for t in rebuilt] == ["local_run_command", "local_read_file"]
        assert _client_tools_digest_from_snapshot(snapshot) == _client_tools_digest(request)

    def test_nothing_is_recorded_without_client_tools(self):
        from apis.inference_api.chat.models import InvocationRequest
        from apis.inference_api.chat.routes import _record_client_tools_on

        agent = SimpleNamespace(_construction_snapshot={"model_id": "m"})
        _record_client_tools_on(agent, InvocationRequest(session_id="s", message="m"))
        assert "client_tools" not in agent._construction_snapshot

    def test_an_agent_without_a_snapshot_dict_is_left_alone(self):
        from apis.inference_api.chat.models import InvocationRequest
        from apis.inference_api.chat.routes import _record_client_tools_on

        agent = SimpleNamespace()
        _record_client_tools_on(agent, InvocationRequest(session_id="s", message="m", client_tools=[spec().model_dump(by_alias=True)]))
        assert not hasattr(agent, "_construction_snapshot")


class TestRequestValidationMessages:
    """A misbehaving client gets a 422 whose message says what to fix."""

    @pytest.mark.parametrize(
        ("tools", "fragment"),
        [
            ([spec(f"t{i}").model_dump(by_alias=True) for i in range(MAX_CLIENT_TOOLS + 1)], "at most"),
            ([spec("dup").model_dump(by_alias=True)] * 2, "twice"),
            ([{"name": "bad-name", "description": "d"}], "client tool name"),
            ([{"name": "ok", "description": ""}], "must not be empty"),
            ([{"name": "ok", "description": "d", "inputSchema": {"type": "array"}}], '"type": "object"'),
        ],
    )
    def test_messages(self, tools, fragment):
        from apis.inference_api.chat.models import InvocationRequest

        with pytest.raises(ValidationError) as excinfo:
            InvocationRequest(session_id="s", message="m", client_tools=tools)
        assert fragment in str(excinfo.value)
