"""The part of a tool spec that a model is actually sent.

A registered Strands ``ToolSpec`` can carry more than any model formatter
reads. On MCP protocol 2026-07-28 Strands copies a tool's ``outputSchema`` onto
its spec (``MCPAgentTool.tool_spec``), and it has long copied MCP
``annotations`` too. Bedrock Converse, OpenAI chat, OpenAI Responses and
Anthropic all build their tool definitions from ``name``, ``description`` and
``inputSchema`` alone. ``strict`` comes from model config, not the spec.

Anything that hashes or sizes "the tools the model sees" must go through
:func:`model_facing_tool_specs`. That covers the ``toolConfigHash`` fingerprint,
the Responses ``prompt_cache_key`` and the context breakdown's per-origin
apportioning. Otherwise they track bytes that never reach the wire:

- GitHub's 43 tools carry +77 KB of output schema;
- the hash rotates with no cache miss behind it, which sends a cost
  investigation after a false lead;
- the cache key moves and routes a Responses request to a cold cache;
- GitHub's share of the tool tokens is overstated about 2.6×.

See ``docs/specs/mcp-typed-tool-outputs.md`` §1. ``test_tool_specs.py`` pins
this projection against each formatter's real output, so a formatter that
starts forwarding another key fails CI instead of drifting.
"""

from typing import Any, List, Optional

# Every key a model formatter reads from a ToolSpec, in no particular order
# (canonical JSON sorts keys). Add one here only when a formatter starts
# sending it; the pinning test names the formatters to check.
MODEL_FACING_TOOL_SPEC_KEYS = ("name", "description", "inputSchema")


def model_facing_tool_spec(spec: Any) -> Any:
    """``spec`` without the keys no formatter sends. A non-dict passes through."""
    if not isinstance(spec, dict):
        return spec
    return {key: spec[key] for key in MODEL_FACING_TOOL_SPEC_KEYS if key in spec}


def model_facing_tool_specs(specs: Optional[Any]) -> List[Any]:
    """Each spec projected by :func:`model_facing_tool_spec`, order kept.

    Order is kept on purpose. An order flip is a real cache-buster, and the
    fingerprint must still see it.
    """
    if not specs:
        return []
    return [model_facing_tool_spec(spec) for spec in specs]


__all__: List[str] = [
    "MODEL_FACING_TOOL_SPEC_KEYS",
    "model_facing_tool_spec",
    "model_facing_tool_specs",
]
