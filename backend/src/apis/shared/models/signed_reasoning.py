"""Keep reasoning blocks Claude cannot verify out of its history.

Claude's extended thinking returns each ``reasoningContent`` block with a
``signature``, and Claude checks that signature when the block is replayed.
Other models produce reasoning too, but unsigned: Strands' OpenAI-compatible
models stream reasoning as ``reasoningContent`` text with no signature, and
the session stores it in history like any other block. A conversation that ran
a turn on GPT and then switches to Claude would replay that unsigned block,
and Strands' Bedrock formatter passes it through as is (it only omits an empty
signature). A model switch now continues the conversation in place instead of
starting a new one, so this path is reachable from the model picker.

The projection drops every ``reasoningContent`` block that carries neither a
signature nor ``redactedContent``. The reasoning was the other model's private
scratch work; its answer, tool calls and tool results all stay.

Same rules as ``text_only_input``:

- **Never mutates.** The message list is shared across every agent serving the
  session (#741), and the GPT agent that wrote the block may still read it.
- **Deterministic.** Which blocks go depends only on the blocks, so the
  projected history is byte-stable turn to turn (the prompt-cache contract).
  A Claude-only history has nothing to drop and is returned as the same list.
- **Only on Claude via Converse.** ``AgentFactory`` installs it only there.
"""

from __future__ import annotations

from typing import Any, List, Optional

from apis.shared.models.history_projection import project_model_history

# Stands in for a message whose only content was unsigned reasoning, because
# Bedrock rejects a message with no content blocks.
REASONING_OMITTED_TEXT = "[Reasoning from another model omitted]"


def _is_unsigned_reasoning(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    reasoning = block.get("reasoningContent")
    if not isinstance(reasoning, dict):
        return False
    if reasoning.get("redactedContent") or reasoning.get("signature"):
        return False
    text = reasoning.get("reasoningText")
    return not (isinstance(text, dict) and text.get("signature"))


def project_signed_reasoning_messages(messages: List[Any]) -> List[Any]:
    """``messages`` without the reasoning blocks Claude cannot verify.

    Returns ``messages`` itself when there is nothing to drop. Otherwise returns
    a new list that shares every untouched message with the input.
    """
    replaced: Optional[List[Any]] = None
    for i, message in enumerate(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list) or not any(_is_unsigned_reasoning(b) for b in content):
            continue
        kept = [b for b in content if not _is_unsigned_reasoning(b)] or [{"text": REASONING_OMITTED_TEXT}]
        if replaced is None:
            replaced = list(messages)
        replaced[i] = {**message, "content": kept}
    return messages if replaced is None else replaced


def signed_reasoning_input(model: Any) -> Any:
    """Make ``model`` see its history without unsigned reasoning blocks."""
    return project_model_history(model, project_signed_reasoning_messages)
