"""Keep media blocks in conversation history off a model that reads text only.

``_adapt_attachments_for_model`` (inference_api/chat/routes.py) keeps this
turn's documents and images away from a model whose catalog row declares
``inputModalities == ["TEXT"]``. History is the other way in. A conversation
that sent an image to Claude and then switched to ``zai.glm-5`` still carries
that ``image`` block, and Bedrock rejects the whole request ("This model
doesn't support the image content block"), so the conversation is stuck on
that model. Restore turns a document into its digest, but an image survives
restore unless compaction truncates it. A document survives too while the
session stays warm: a model switch builds a second agent that shares the live
message list (``_adopt_session_conversation``), and nothing re-restores it.

The fix is a projection taken at the model seam, per request:

- **Never mutates.** The message list is aliased across every agent serving the
  session (#741), so replacing a block in place would strip it from a vision
  agent too. Only the lists and dicts on the path to a replaced block are
  copied; everything else is shared with the input, and a history with no
  media is returned as the same object.
- **Deterministic.** A placeholder depends only on the block it replaces, so
  the projected history is byte-stable turn to turn (the prompt-cache
  contract). A model switch re-writes the prefix anyway; the projection must
  not make it flip after that.
- **Only on text-only models.** ``AgentFactory`` installs it only when the
  turn's model is text-only, so every other model's call path is unchanged
  and pays nothing.
"""

from __future__ import annotations

from typing import Any, List, Optional

# Content-block keys a TEXT-only model rejects. A toolResult's content can
# carry the same blocks and is projected the same way.
_MEDIA_KEYS = ("image", "document", "video")

_REASON = "the current model reads text only"


def _placeholder(kind: str, payload: Any) -> str:
    """The text that stands in for one media block. Pure: same block, same text."""
    if kind == "document":
        name = payload.get("name") if isinstance(payload, dict) else None
        if isinstance(name, str) and name.strip():
            return f'[Document "{name.strip()}" omitted: {_REASON}]'
        return f"[Document omitted: {_REASON}]"
    return f"[{kind.capitalize()} omitted: {_REASON}]"


def _project_block(block: Any) -> Any:
    if not isinstance(block, dict):
        return block
    for key in _MEDIA_KEYS:
        if key in block:
            return {"text": _placeholder(key, block[key])}
    tool_result = block.get("toolResult")
    if not isinstance(tool_result, dict):
        return block
    items = tool_result.get("content")
    if not isinstance(items, list):
        return block
    projected = _project_content(items)
    if projected is items:
        return block
    return {**block, "toolResult": {**tool_result, "content": projected}}


def _project_content(content: List[Any]) -> List[Any]:
    replaced: Optional[List[Any]] = None
    for i, block in enumerate(content):
        projected = _project_block(block)
        if projected is not block:
            if replaced is None:
                replaced = list(content)
            replaced[i] = projected
    return content if replaced is None else replaced


def project_text_only_messages(messages: List[Any]) -> List[Any]:
    """``messages`` with every image, document and video block replaced by text.

    Returns ``messages`` itself when there is nothing to replace. Otherwise
    returns a new list that shares every untouched message with the input.
    """
    replaced: Optional[List[Any]] = None
    for i, message in enumerate(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        projected = _project_content(content)
        if projected is not content:
            if replaced is None:
                replaced = list(messages)
            replaced[i] = {**message, "content": projected}
    return messages if replaced is None else replaced


def text_only_input(model: Any) -> Any:
    """Make ``model`` see its message history through the text-only projection.

    Wraps the instance's ``stream`` (the request) and ``count_tokens`` (Strands'
    pre-call estimate, which should count what is actually sent), plus
    ``native_count_tokens`` where the model has one (the context-attribution
    hook's background count). The wrappers are set on the instance rather than
    a subclass because the OpenAI-compatible models come from shared builders,
    and an instance attribute keeps every ``isinstance`` check on the model
    (``BedrockModel`` included) true.
    """
    stream = model.stream
    count_tokens = model.count_tokens
    native_count_tokens = getattr(model, "native_count_tokens", None)

    def projected_stream(messages: List[Any], *args: Any, **kwargs: Any) -> Any:
        return stream(project_text_only_messages(messages), *args, **kwargs)

    async def projected_count_tokens(messages: List[Any], *args: Any, **kwargs: Any) -> int:
        return await count_tokens(project_text_only_messages(messages), *args, **kwargs)

    model.stream = projected_stream
    model.count_tokens = projected_count_tokens
    if native_count_tokens is not None:

        def projected_native_count_tokens(messages: List[Any], *args: Any, **kwargs: Any) -> Optional[int]:
            return native_count_tokens(project_text_only_messages(messages), *args, **kwargs)

        model.native_count_tokens = projected_native_count_tokens
    return model
