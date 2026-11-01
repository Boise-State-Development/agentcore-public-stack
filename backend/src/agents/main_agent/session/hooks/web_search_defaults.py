"""Default AgentCore Web Search's `maxResults` to 5 when the model omits it.

The Web Search connector (`apis.shared.tools.gateway_connectors.WEB_SEARCH`)
returns 10 results unless asked for fewer, at about 535 tokens each, so an
unqualified search puts ~5.3k tokens into the conversation where Brave puts
~1.6k (`docs/specs/web-search-default-comparison-findings.md`). Five results
measured ~2.6k. The connector fixes the tool's schema and description, so
what the model is told cannot change; filling in the argument at call time is
the only lever that leaves the cacheable prefix alone.

``BeforeToolCallEvent``
    For a call whose tool name ends in ``___WebSearch`` (the Gateway names it
    ``<target>___WebSearch`` and admins name the target) and whose input has
    no ``maxResults``, hand Strands a copy of the tool use with
    ``maxResults=5``. A ``maxResults`` the model chose passes through as is.

The copy matters. ``event.tool_use`` is the same dict as the ``toolUse`` block
in the assistant message already in history (and already persisted), so
writing into it would make the live conversation disagree with the stored one
and re-write the prompt cache after the next restore. Strands runs the tool
with ``event.tool_use`` as the hook leaves it, so replacing it changes only
what the Gateway receives; the model's own request stays byte-identical in
history and the SSE ``tool_use`` it streamed.

No IO and no prompt text: a string suffix test and a dict copy on a Web
Search call, a suffix test on any other. Registered only while
``WEB_SEARCH_DEFAULT_MAX_RESULTS_ENABLED`` is on, so the default costs nothing.
"""

from __future__ import annotations

from typing import Any

from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry

from apis.shared.tools.gateway_connectors import WEB_SEARCH

#: What an unqualified search asks for. Five halves the payload against the
#: connector's own ten (measured 2,648 vs 5,349 tokens over 30 queries).
WEB_SEARCH_DEFAULT_MAX_RESULTS = 5

_MAX_RESULTS_KEY = "maxResults"
_TOOL_SUFFIX = f"___{WEB_SEARCH.tool_name}"


def is_web_search_tool(tool_name: Any) -> bool:
    """Whether `tool_name` is a Gateway Web Search target's tool, under any target name."""
    return isinstance(tool_name, str) and tool_name.endswith(_TOOL_SUFFIX)


class WebSearchDefaultsHook(HookProvider):
    """Fill in Web Search's `maxResults` when the model leaves it out."""

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before_tool_call)

    def _before_tool_call(self, event: BeforeToolCallEvent) -> None:
        if event.cancel_tool:
            return
        tool_use = event.tool_use
        if not is_web_search_tool(tool_use.get("name")):
            return
        tool_input = tool_use.get("input")
        if tool_input is None:
            tool_input = {}
        if not isinstance(tool_input, dict):
            return
        # An explicit null asks for nothing in particular, the same as leaving
        # the key out; the connector would answer it with ten.
        if tool_input.get(_MAX_RESULTS_KEY) is not None:
            return
        event.tool_use = {
            **tool_use,
            "input": {**tool_input, _MAX_RESULTS_KEY: WEB_SEARCH_DEFAULT_MAX_RESULTS},
        }
