"""``model_facing_tool_specs``: hash and size only what a model is sent.

The projection is only right while it matches every formatter we run. These
tests format real Strands requests, so a formatter that starts sending another
spec key (an ``outputSchema``, say) fails here rather than letting
``toolConfigHash`` and the Responses ``prompt_cache_key`` drift from the wire.
See docs/specs/mcp-typed-tool-outputs.md §1.
"""

import copy
import json
from types import SimpleNamespace
from typing import Any, Callable, Dict, List

import pytest
from strands.models.bedrock import BedrockModel
from strands.models.openai import OpenAIModel
from strands.models.openai_responses import OpenAIResponsesModel

from agents.main_agent.session.hooks.context_itemization import itemize_tools
from agents.main_agent.session.hooks.prefix_fingerprint import compute_prefix_fingerprint
from apis.shared.models.bedrock_responses import build_prompt_cache_key
from apis.shared.models.tool_specs import (
    MODEL_FACING_TOOL_SPEC_KEYS,
    model_facing_tool_spec,
    model_facing_tool_specs,
)

MESSAGES = [{"role": "user", "content": [{"text": "hi"}]}]

PLAIN = {
    "name": "list_issues",
    "description": "List issues in a repository.",
    "inputSchema": {
        "json": {
            "type": "object",
            "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}},
            "required": ["owner", "repo"],
        }
    },
}

# What Strands registers for the same tool from a 2026-07-28 MCP server:
# `MCPAgentTool.tool_spec` adds the output schema and the annotations.
TYPED = {
    **PLAIN,
    "outputSchema": {
        "json": {
            "type": "object",
            "properties": {"issues": {"type": "array", "items": {"type": "object"}}, "totalCount": {"type": "integer"}},
        }
    },
    "annotations": {"readOnlyHint": True, "title": "List issues"},
}

OTHER = {
    "name": "calculator",
    "description": "Evaluate an expression.",
    "inputSchema": {"json": {"type": "object", "properties": {"expression": {"type": "string"}}}},
}


def _bedrock(specs: List[Dict[str, Any]]) -> Any:
    model = BedrockModel(model_id="anthropic.claude-haiku-4-5-20251001-v1:0", region_name="us-west-2")
    return model.format_request(copy.deepcopy(MESSAGES), specs, None)["toolConfig"]


def _bedrock_strict(specs: List[Dict[str, Any]]) -> Any:
    model = BedrockModel(
        model_id="anthropic.claude-haiku-4-5-20251001-v1:0", region_name="us-west-2", strict_tools=True
    )
    return model.format_request(copy.deepcopy(MESSAGES), specs, None)["toolConfig"]


def _openai_chat(specs: List[Dict[str, Any]]) -> Any:
    model = OpenAIModel(client_args={"api_key": "test"}, model_id="gpt-test")
    return model.format_request(copy.deepcopy(MESSAGES), specs, None)["tools"]


def _openai_responses(specs: List[Dict[str, Any]]) -> Any:
    model = OpenAIResponsesModel(client_args={"api_key": "test"}, model_id="gpt-test")
    return model._format_request(copy.deepcopy(MESSAGES), specs, None)["tools"]


FORMATTERS: Dict[str, Callable[[List[Dict[str, Any]]], Any]] = {
    "bedrock": _bedrock,
    "bedrock_strict": _bedrock_strict,
    "openai_chat": _openai_chat,
    "openai_responses": _openai_responses,
}


def _wire(fmt: Callable[[List[Dict[str, Any]]], Any], specs: List[Dict[str, Any]]) -> str:
    return json.dumps(fmt(copy.deepcopy(specs)), sort_keys=True)


class TestProjectionMatchesTheWire:
    @pytest.mark.parametrize("formatter", sorted(FORMATTERS))
    def test_a_formatter_sends_nothing_the_projection_drops(self, formatter):
        fmt = FORMATTERS[formatter]
        assert _wire(fmt, [TYPED, OTHER]) == _wire(fmt, model_facing_tool_specs([TYPED, OTHER]))

    @pytest.mark.parametrize("formatter", sorted(FORMATTERS))
    @pytest.mark.parametrize("key", MODEL_FACING_TOOL_SPEC_KEYS)
    def test_a_formatter_reads_every_key_the_projection_keeps(self, formatter, key):
        # Changing a kept key changes the request, so none of them is dead weight.
        fmt = FORMATTERS[formatter]
        changed = copy.deepcopy(PLAIN)
        if key == "inputSchema":
            changed[key]["json"]["properties"]["state"] = {"type": "string"}
        else:
            changed[key] = changed[key] + "-changed"
        assert _wire(fmt, [PLAIN]) != _wire(fmt, [changed])


class TestProjection:
    def test_keeps_exactly_the_model_facing_keys(self):
        assert model_facing_tool_spec(TYPED) == PLAIN

    def test_keeps_order(self):
        assert [s["name"] for s in model_facing_tool_specs([TYPED, OTHER])] == ["list_issues", "calculator"]

    def test_does_not_mutate_the_registered_spec(self):
        spec = copy.deepcopy(TYPED)
        model_facing_tool_spec(spec)
        assert spec == TYPED

    def test_empty_and_non_dict_inputs(self):
        assert model_facing_tool_specs(None) == []
        assert model_facing_tool_specs([]) == []
        assert model_facing_tool_spec("x") == "x"


class TestConsumers:
    def test_tool_config_hash_ignores_output_schema_and_annotations(self):
        typed = compute_prefix_fingerprint([TYPED, OTHER], "system", MESSAGES)
        plain = compute_prefix_fingerprint([PLAIN, OTHER], "system", MESSAGES)
        assert typed["toolConfigHash"] == plain["toolConfigHash"]

    def test_tool_config_hash_still_sees_a_reorder(self):
        a = compute_prefix_fingerprint([PLAIN, OTHER], "system", MESSAGES)
        b = compute_prefix_fingerprint([OTHER, PLAIN], "system", MESSAGES)
        assert a["toolConfigHash"] != b["toolConfigHash"]

    def test_tool_config_hash_still_sees_an_input_schema_change(self):
        changed = copy.deepcopy(PLAIN)
        changed["inputSchema"]["json"]["properties"]["state"] = {"type": "string"}
        a = compute_prefix_fingerprint([PLAIN], "system", MESSAGES)
        b = compute_prefix_fingerprint([changed], "system", MESSAGES)
        assert a["toolConfigHash"] != b["toolConfigHash"]

    def test_responses_cache_key_ignores_output_schema(self):
        assert build_prompt_cache_key("system", [TYPED, OTHER]) == build_prompt_cache_key("system", [PLAIN, OTHER])

    def test_responses_cache_key_still_sees_a_reorder(self):
        assert build_prompt_cache_key("system", [PLAIN, OTHER]) != build_prompt_cache_key("system", [OTHER, PLAIN])

    def test_itemization_does_not_charge_an_origin_for_its_output_schema(self):
        class _Client:
            context_label = "GitHub"
            server_url = "https://api.githubcopilot.com/mcp/"

        def _tool(spec, client=None):
            tool = SimpleNamespace(tool_spec=spec, tool_name=spec["name"])
            if client is not None:
                tool.mcp_client = client
            return tool

        def _agent(github_spec):
            registry = SimpleNamespace(
                registry={
                    "calculator": _tool(OTHER),
                    "list_issues": _tool(github_spec, _Client()),
                },
                dynamic_tools={},
            )
            return SimpleNamespace(tool_registry=registry)

        heavy = copy.deepcopy(TYPED)
        heavy["outputSchema"]["json"]["description"] = "x" * 50_000
        typed_rows = itemize_tools(_agent(heavy), 1_000)
        plain_rows = itemize_tools(_agent(PLAIN), 1_000)
        assert typed_rows is not None and len(typed_rows) == 2
        assert typed_rows == plain_rows
