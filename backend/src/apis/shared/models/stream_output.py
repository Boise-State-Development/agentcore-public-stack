"""Mark a model-stream failure that happened after output reached the user.

A retry restarts generation from scratch. That is invisible when the failed
call emitted nothing, and a visible duplicate when it already streamed text:
the user sees the abandoned prefix followed by a second, full answer. The
Bedrock Converse path can tell the two apart by exception type
(``EventStreamError`` is mid-stream). The OpenAI Responses transport cannot —
the same ``openai.APIError`` or ``ModelThrottledException`` is raised whether
the failure came on the first event or the thousandth.

So :func:`tag_partial_output`, which the agent factory applies to every
model, records the fact on the exception itself, and the retry strategy reads
it back. The flag rides the exception
rather than the model or the agent because one model instance serves many
concurrent turns.
"""

import logging
from typing import Any, AsyncGenerator

logger = logging.getLogger(__name__)

__all__ = [
    "is_visible_output_chunk",
    "mark_partial_output",
    "streamed_partial_output",
    "tag_partial_output",
]

_PARTIAL_OUTPUT_ATTR = "_agentcore_streamed_partial_output"
_WRAPPED_ATTR = "_agentcore_partial_output_tagged"

# Strands StreamEvent keys that put something on the user's screen. A
# contentBlockStart alone opens an empty block in the SPA, which a restarted
# call would open a second time, so it counts too.
_VISIBLE_CHUNK_KEYS = ("contentBlockStart", "contentBlockDelta")


def is_visible_output_chunk(chunk: object) -> bool:
    """Whether a formatted stream chunk reaches the client as content."""
    return isinstance(chunk, dict) and any(key in chunk for key in _VISIBLE_CHUNK_KEYS)


def mark_partial_output(error: BaseException) -> None:
    """Record that ``error`` was raised after output had been streamed."""
    try:
        setattr(error, _PARTIAL_OUTPUT_ATTR, True)
    except (AttributeError, TypeError):  # pragma: no cover - slotted/builtin exceptions
        logger.debug("Could not mark partial output on %s", type(error).__name__)


def streamed_partial_output(error: BaseException) -> bool:
    """Whether ``error`` was marked by :func:`mark_partial_output`."""
    return getattr(error, _PARTIAL_OUTPUT_ATTR, False) is True


def tag_partial_output(model: Any) -> Any:
    """Wrap ``model.stream`` so a failure after visible output is marked.

    Works on any Strands model: it reads only the formatted chunks every
    provider yields, so Bedrock, the OpenAI family and Gemini share one
    implementation and a new provider is covered by construction. Wraps the
    instance, not the class, because the factory receives instances from
    builders it shares with other callers. Idempotent.

    Cost on the stream: one dict-key check per chunk until the first content
    chunk, then a boolean test. Nothing runs before the request goes out.
    """
    if getattr(model, _WRAPPED_ATTR, False):
        return model

    inner = model.stream

    async def stream(*args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        emitted = False
        try:
            async for chunk in inner(*args, **kwargs):
                if not emitted and is_visible_output_chunk(chunk):
                    emitted = True
                yield chunk
        except Exception as error:
            if emitted:
                mark_partial_output(error)
            raise

    model.stream = stream
    setattr(model, _WRAPPED_ATTR, True)
    return model
