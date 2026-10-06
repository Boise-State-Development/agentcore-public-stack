"""Mark a model-stream failure that happened after output reached the user.

A retry restarts generation from scratch. That is invisible when the failed
call emitted nothing, and a visible duplicate when it already streamed text:
the user sees the abandoned prefix followed by a second, full answer. The
Bedrock Converse path can tell the two apart by exception type
(``EventStreamError`` is mid-stream). The OpenAI Responses transport cannot —
the same ``openai.APIError`` or ``ModelThrottledException`` is raised whether
the failure came on the first event or the thousandth.

So the model wrapper that sees the chunks records the fact on the exception
itself, and the retry strategy reads it back. The flag rides the exception
rather than the model or the agent because one model instance serves many
concurrent turns.
"""

import logging

logger = logging.getLogger(__name__)

__all__ = [
    "is_visible_output_chunk",
    "mark_partial_output",
    "streamed_partial_output",
]

_PARTIAL_OUTPUT_ATTR = "_agentcore_streamed_partial_output"

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
