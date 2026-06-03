"""Hook that computes a per-turn context-token attribution breakdown.

Splits the authoritative projected input-token count (Bedrock-native via
``CountTokensBedrockModel``) into ``system`` / ``tools`` / ``messages``
partitions and stashes the result on the agent. The stream coordinator reads
it via :func:`get_context_breakdown` and attaches it to the turn's final
``metadata`` SSE event as ``contextBreakdown`` — answering "what is filling the
context window?" without an aggregate-only guess.

Decomposition (convention validated live against Bedrock CountTokens):

- ``systemTokens`` = ``count(system only)``
- ``toolTokens``   = ``full - count(system + messages, no tools)`` — the tool
  schemas **plus** the tool-use scaffolding Bedrock injects only when tools and
  a conversation coexist (~400 tokens). Folded into Tools by design: it is the
  true marginal cost of having tools enabled. An empty-messages baseline would
  miss the scaffolding and mis-attribute it to messages.
- ``messageTokens`` = ``full - systemTokens - toolTokens`` (residual; grows with
  the conversation, scaffolding-free). Partitions sum to ``full`` by
  construction.

``systemTokens`` / ``toolTokens`` are stable across a session (the tool
overhead is constant as the conversation grows — verified), so they are
computed once per agent at cold start (two extra CountTokens calls) and cached;
every turn afterward is pure arithmetic against the free, authoritative
``projected_input_tokens``.

Best-effort: any failure is swallowed so context attribution can never break a
model call. For non-Bedrock models ``count_tokens`` falls back to a heuristic,
so the numbers are approximate there.
"""

import logging
from typing import Any, Optional

from strands.hooks import BeforeModelCallEvent, HookProvider, HookRegistry

logger = logging.getLogger(__name__)

# Stashed on the per-session Strands agent instance.
_SPLIT_ATTR = "_context_attribution_split"          # cached stable {systemTokens, toolTokens}
_BREAKDOWN_ATTR = "_context_attribution_breakdown"  # latest per-turn breakdown dict


def get_context_breakdown(agent: Any) -> Optional[dict]:
    """Return the latest context breakdown stashed on ``agent``, or ``None``.

    Used by the stream coordinator to enrich the final ``metadata`` SSE event.
    """
    return getattr(agent, _BREAKDOWN_ATTR, None)


class ContextAttributionHook(HookProvider):
    """Compute the system / tools / messages token breakdown each turn."""

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self._on_before_model_call)

    async def _on_before_model_call(self, event: BeforeModelCallEvent) -> None:
        try:
            await self._compute(event)
        except Exception as e:  # noqa: BLE001 - attribution must never break a turn
            logger.debug("Context attribution skipped: %s", e)

    async def _compute(self, event: BeforeModelCallEvent) -> None:
        agent = event.agent
        model = agent.model
        system_prompt = getattr(agent, "system_prompt", None)
        system_prompt_content = getattr(agent, "_system_prompt_content", None)
        full = event.projected_input_tokens

        split = getattr(agent, _SPLIT_ATTR, None)
        if split is None:
            system_tokens = await model.count_tokens(
                messages=[],
                system_prompt=system_prompt,
                system_prompt_content=system_prompt_content,
            )
            # system + the current conversation, WITHOUT tools — so the
            # difference from `full` captures tool schemas + the tool-use
            # scaffolding (present only when tools and messages coexist).
            no_tools = await model.count_tokens(
                messages=agent.messages,
                system_prompt=system_prompt,
                system_prompt_content=system_prompt_content,
            )
            if full is None:
                # projected estimate unavailable — count the full request once
                # so cold start can still establish the split.
                tool_specs = agent.tool_registry.get_all_tool_specs()
                full = await model.count_tokens(
                    messages=agent.messages,
                    tool_specs=tool_specs,
                    system_prompt=system_prompt,
                    system_prompt_content=system_prompt_content,
                )
            split = {
                "systemTokens": system_tokens,
                "toolTokens": max(0, full - no_tools),
            }
            setattr(agent, _SPLIT_ATTR, split)

        if full is None:
            # No authoritative total this turn — can't place the messages
            # partition. Leave the previous breakdown (if any) untouched.
            return

        message_tokens = max(0, full - split["systemTokens"] - split["toolTokens"])
        breakdown = {
            "total": full,
            "partitions": [
                {"key": "system", "label": "System prompt", "tokens": split["systemTokens"]},
                {"key": "tools", "label": "Tools", "tokens": split["toolTokens"]},
                {"key": "messages", "label": "Messages", "tokens": message_tokens},
            ],
        }
        setattr(agent, _BREAKDOWN_ATTR, breakdown)
