"""The Converse request we send, byte for byte, pinned against a checked-in copy.

Bedrock prompt caching is exact-prefix-match, so any change to how Strands
formats a request re-writes every live conversation's cached prefix once, at
the cache-write premium — and none of it raises. A Strands bump used to be
checked for this by reading diffs. This test makes it a property CI enforces:
it formats a realistic conversation (tools, a document, an image, tool
results, signed thinking blocks, the system cachePoint ``AgentFactory``
places) through the production path, ``ModelConfig.to_bedrock_config()`` into
``CountTokensBedrockModel.format_request``, and compares the result with
``tests/fixtures/bedrock_golden_requests.json``.

A failure is a decision, not a flake. If the change is intended (a bump that
re-orders a field, a new beta, a deliberate cache-point move), price it,
say so in the PR, and regenerate:

    UPDATE_BEDROCK_GOLDEN=1 uv run python -m pytest tests/agents/main_agent/core/test_bedrock_golden_requests.py

Established on strands-agents 1.63.0 and confirmed identical on 1.61.1.
"""

import base64
import json
import os
from pathlib import Path

import pytest

from agents.main_agent.core.model_config import ModelConfig

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "bedrock_golden_requests.json"

_SIGNATURE = "EqQBCkgIARABGAIiQL" + "A" * 40

# (name, model id, inference params, thinking history, static-prefix TTL env, MCP Apps host)
CASES = [
    ("haiku45", "us.anthropic.claude-haiku-4-5-20251001-v1:0", {"max_tokens": 8192}, False, "", "true"),
    ("haiku45-global", "global.anthropic.claude-haiku-4-5-20251001-v1:0", {"max_tokens": 8192}, False, "", "true"),
    ("sonnet46-no-max-tokens", "us.anthropic.claude-sonnet-4-6", {}, False, "", "true"),
    ("sonnet46-1h-static-prefix", "us.anthropic.claude-sonnet-4-6", {"max_tokens": 8192}, False, "1h", "true"),
    ("sonnet46-mcp-apps-off", "us.anthropic.claude-sonnet-4-6", {"max_tokens": 8192}, False, "", "false"),
    (
        "opus55-thinking",
        "global.anthropic.claude-opus-5-5",
        {"max_tokens": 32000, "thinking": {"type": "adaptive"}},
        True,
        "",
        "true",
    ),
    (
        "opus47-thinking",
        "us.anthropic.claude-opus-4-7",
        {"max_tokens": 32000, "thinking": {"type": "adaptive"}},
        True,
        "",
        "true",
    ),
    ("nova-micro", "us.amazon.nova-micro-v1:0", {"max_tokens": 512}, False, "", "true"),
]


def _conversation(thinking: bool) -> list:
    reasoning = [{"reasoningContent": {"reasoningText": {"text": "let me check", "signature": _SIGNATURE}}}] if thinking else []
    return [
        {
            "role": "user",
            "content": [
                {"text": "summarise this"},
                {"document": {"format": "txt", "name": "notes", "source": {"bytes": b"hello world"}}},
            ],
        },
        {
            "role": "assistant",
            "content": reasoning + [{"text": "Looking it up."}, {"toolUse": {"toolUseId": "t1", "name": "get_thread", "input": {"id": 7}}}],
        },
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "t1",
                        "status": "success",
                        "content": [{"text": "thread body"}, {"json": {"n": 1}}],
                    }
                }
            ],
        },
        {"role": "assistant", "content": [{"text": "Done."}]},
        {
            "role": "user",
            "content": [
                {"text": "and again"},
                {"image": {"format": "png", "source": {"bytes": base64.b64decode("iVBORw0KGgo=")}}},
            ],
        },
    ]


_TOOLS = [
    {
        "name": f"tool_{i}",
        "description": f"Tool number {i}.",
        "inputSchema": {"json": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}},
    }
    for i in range(3)
]


def _system_blocks(supported: bool) -> list:
    # What AgentFactory.create_agent passes: a trailing cachePoint only where
    # bedrock_cache_points_supported().
    blocks = [{"text": "You are a helpful assistant."}]
    return blocks + [{"cachePoint": {"type": "default"}}] if supported else blocks


def _encode(value):
    if isinstance(value, bytes):
        return {"__bytes__": base64.b64encode(value).decode()}
    raise TypeError(f"unserializable {type(value).__name__} in a formatted request")


def _formatted(monkeypatch, model_id, params, thinking, ttl, mcp_apps) -> dict:
    from agents.main_agent.core.bedrock_count_tokens import CountTokensBedrockModel

    monkeypatch.setenv("AGENTCORE_PROMPT_CACHE_STATIC_PREFIX_TTL", ttl)
    monkeypatch.setenv("AGENTCORE_MCP_APPS_HOST_ENABLED", mcp_apps)
    config = ModelConfig(model_id=model_id, caching_enabled=True, inference_params=params)
    model = CountTokensBedrockModel(native_projection=False, **config.to_bedrock_config())
    request = model.format_request(
        _conversation(thinking),
        _TOOLS,
        system_prompt_content=_system_blocks(config.bedrock_cache_points_supported()),
    )
    return json.loads(json.dumps(request, default=_encode, sort_keys=True))


@pytest.fixture(autouse=True)
def _region(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")


def test_formatted_requests_match_the_golden_copy(monkeypatch):
    actual = {name: _formatted(monkeypatch, *rest) for name, *rest in CASES}

    if os.environ.get("UPDATE_BEDROCK_GOLDEN") == "1":
        FIXTURE.write_text(json.dumps(actual, sort_keys=True, indent=1) + "\n")
        pytest.skip(f"regenerated {FIXTURE.name}")

    expected = json.loads(FIXTURE.read_text())
    assert sorted(actual) == sorted(expected), "the case list changed; regenerate the fixture"
    for name in expected:
        assert actual[name] == expected[name], (
            f"the {name!r} request changed. Every live conversation re-writes its cached prefix once "
            "when this ships; if that is intended, price it in the PR and regenerate (see module docstring)"
        )
