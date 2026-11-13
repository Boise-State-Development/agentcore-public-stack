"""A model that declines at the provider gets a visible answer, not a blank one.

Some models decline a request at the provider rather than in words: the call
ends with stop reason ``content_filtered`` (Strands' name for a provider-side
refusal; Bedrock Converse reports it for Claude's ``refusal``) or
``guardrail_intervened``, having streamed no text. Before this, the chat path
had no case for it: the user saw an empty assistant bubble, a reload showed the
same, and the eval harness scored it as an empty answer. Measured on dev
2026-11-13: Opus 5.5 declined both harmful-request refusal cases this way on
every sample (one model call, zero output tokens, ``content_filtered``).

Two halves, one notice:

- :class:`DeclinedAnswerHook` (``AfterModelCallEvent``) adds the notice to the
  call's message before Strands appends it to the conversation, so history,
  persistence and a reload all carry it. Strands hands the hook the same
  message object it then appends (``event_loop.py``, the model-call retry loop).
- The stream coordinator streams the same notice as a text block at that
  call's ``message_stop`` (:func:`needs_notice`), because the call's stream
  events reach the client before the hook runs.

Cost and latency: the hook runs after the model has answered, never before a
first token, and does nothing unless the stop reason is one of
:data:`DECLINE_STOP_REASONS`. The notice is a constant, so a conversation
that carries it is byte-stable turn to turn (prompt-cache contract), and it is
about 30 tokens of history, once.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

from strands.hooks import AfterModelCallEvent, HookProvider, HookRegistry

logger = logging.getLogger(__name__)

#: Stop reasons meaning the provider declined to produce the answer.
DECLINE_STOP_REASONS = frozenset({"content_filtered", "guardrail_intervened"})

#: What the user sees, and what the conversation keeps, in place of nothing.
#: Platform-voiced on purpose: the model wrote none of it.
DECLINED_NOTICE = (
    "The model declined to respond to this request, so there is no answer. "
    "You can rephrase it or ask something else."
)


def _has_visible_content(content: Iterable[Any]) -> bool:
    for block in content or ():
        if not isinstance(block, Mapping):
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            return True
        if "toolUse" in block:
            return True
    return False


def needs_notice(stop_reason: Any, streamed_text: bool) -> bool:
    """Whether a call that stopped with ``stop_reason`` should show the notice."""
    return stop_reason in DECLINE_STOP_REASONS and not streamed_text


class DeclinedAnswerHook(HookProvider):
    """Adds :data:`DECLINED_NOTICE` to a declined call's empty message."""

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterModelCallEvent, self._on_after_model_call)

    def _on_after_model_call(self, event: AfterModelCallEvent) -> None:
        response = getattr(event, "stop_response", None)
        if response is None or response.stop_reason not in DECLINE_STOP_REASONS:
            return
        try:
            message = response.message
            content = message.get("content")
            if not isinstance(content, list):
                content = []
                message["content"] = content
            if _has_visible_content(content):
                return
            content.append({"text": DECLINED_NOTICE})
            logger.info("model declined at the provider (stop_reason=%s); notice added", response.stop_reason)
        except Exception as exc:  # noqa: BLE001 - never break a turn over a notice
            logger.debug("declined-answer notice skipped: %s", exc)
