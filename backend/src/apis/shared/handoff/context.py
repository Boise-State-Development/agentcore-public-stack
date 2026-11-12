"""How a handoff's context reaches the Agent (``docs/specs/agent-handoffs-and-consults.md`` §4.2).

The excerpt (``excerpt.py``) is what the user reviews and may edit. This module wraps it the way
the Agent will receive it, and joins it to the request. app-api uses it to build ``contextBlock``
for the review sheet's full-prompt preview. inference-api will use the same functions on the
target's first turn (PR-0.2), so the preview and the model message are produced by one
implementation and cannot drift.

Everything here is pure and deterministic.
"""

from __future__ import annotations

import hashlib
from typing import Protocol, Sequence


class DeliveryArtifactLine(Protocol):
    """What a delivery's artifact line names. ``DeliveryArtifact`` satisfies it."""

    artifact_id: str
    version: int
    title: str
    content_type: str


HANDOFF_CONTEXT_TAG = "handoff_context"
EXCERPT_TAG = "conversation_excerpt"
ARTIFACT_TAG = "artifact"

#: One line, constant, so the wrapper is the same for every handoff. It tells the Agent where the
#: block came from; the block itself is the user's material, not instructions to it.
_CONTEXT_NOTE = "The user brought this context from another of their conversations."

#: Tags a body may not open or close (spec §9.6 rule 5): the wrapper's own, Phase 2's
#: ``<conversation_message>``, mid-turn steering's wrapper, and a delivery's artifact line. A body
#: that could write any of them could end its block and go on as the user, or name an artifact
#: the user never chose to send.
_FORGEABLE_TAGS = (HANDOFF_CONTEXT_TAG, "conversation_message", "user_message_during_turn", ARTIFACT_TAG)


def escape_attr(value: str) -> str:
    """Escape a value for a double-quoted attribute on a wrapper tag."""
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def neutralize_close_tags(text: str) -> str:
    """Stop text from ending or forging a block.

    The excerpt carries conversation text verbatim, and the user can edit the card before sending,
    so either could contain a closing tag. Left alone it would end the block and the rest would
    read as prompt text outside it (the house pattern in ``apis/shared/memory/hydration.py``). The
    excerpt's own tag is only defused when closing, because the excerpt opens with it; every other
    wrapper tag is defused opening and closing.
    """
    text = text.replace(f"</{EXCERPT_TAG}", f"<\\/{EXCERPT_TAG}")
    for tag in _FORGEABLE_TAGS:
        text = text.replace(f"</{tag}", f"<\\/{tag}").replace(f"<{tag}", f"<\\{tag}")
    return text


def normalize_context_text(text: str) -> str:
    """The form a context card is hashed and wrapped in.

    A browser textarea can hand back ``\\r\\n`` line endings and stray trailing whitespace. Without
    this, a card the user opened and closed unchanged would hash differently from the excerpt and
    be recorded as edited.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def context_hash(text: str) -> str:
    """SHA-256 of a context card's normalized text: ``excerptHash`` on create, compared on send."""
    return hashlib.sha256(normalize_context_text(text).encode("utf-8")).hexdigest()


def wrap_handoff_context(text: str, source_title: str) -> str:
    """Wrap a context card as the Agent receives it. Empty text gives an empty block.

    The excerpt's own ``<conversation_excerpt>`` tag is kept, not re-neutralized. Only closing tags
    are defused, so a card the user edited cannot close the outer block either.
    """
    body = normalize_context_text(text)
    if not body:
        return ""
    body = _neutralize_inner(body)
    title = escape_attr(source_title.strip() or "Untitled conversation")
    return (
        f'<{HANDOFF_CONTEXT_TAG} from_conversation="{title}">\n'
        f"{_CONTEXT_NOTE}\n"
        f"{body}\n"
        f"</{HANDOFF_CONTEXT_TAG}>"
    )


def render_delivery_block(text: str, from_title: str, artifacts: Sequence[DeliveryArtifactLine] = ()) -> str:
    """A delivery as the receiving model reads it (spec §4.8): 0a's wrapper, plus one line per artifact.

    The same wrapper as a handoff's context, so the model sees one format for context from another
    conversation. Artifacts are references, never bodies: one self-closing line each, every value
    attribute-escaped so a title can't close the block. Empty text and no artifacts give ``""``.
    """
    body = neutralize_close_tags(normalize_context_text(text))
    lines = [
        f'<{ARTIFACT_TAG} id="{escape_attr(a.artifact_id)}" version="{int(a.version)}" '
        f'title="{escape_attr(a.title)}" type="{escape_attr(a.content_type)}"/>'
        for a in artifacts
    ]
    inner = "\n".join(part for part in [body, *lines] if part)
    if not inner:
        return ""
    title = escape_attr(from_title.strip() or "Untitled conversation")
    return (
        f'<{HANDOFF_CONTEXT_TAG} from_conversation="{title}">\n'
        f"{_CONTEXT_NOTE}\n"
        f"{inner}\n"
        f"</{HANDOFF_CONTEXT_TAG}>"
    )


def build_handoff_message(context_block: str, request: str) -> str:
    """The target's first model message: the wrapped context, a blank line, then the request."""
    request = request.strip()
    if not context_block:
        return request
    if not request:
        return context_block
    return f"{context_block}\n\n{request}"


def _neutralize_inner(body: str) -> str:
    """Defuse closing tags inside the excerpt body, keeping the excerpt's own final close tag.

    The renderer already neutralized the conversation text it carries. An edited card may not have
    been, and may close the excerpt part way through, so everything before the excerpt's final
    closing tag is defused again here, and a handoff closing tag is defused everywhere.
    """
    close = f"</{EXCERPT_TAG}>"
    if body.endswith(close):
        inner = body[: -len(close)]
        return neutralize_close_tags(inner) + close
    return neutralize_close_tags(body)


__all__ = [
    "ARTIFACT_TAG",
    "DeliveryArtifactLine",
    "EXCERPT_TAG",
    "HANDOFF_CONTEXT_TAG",
    "build_handoff_message",
    "context_hash",
    "escape_attr",
    "neutralize_close_tags",
    "normalize_context_text",
    "render_delivery_block",
    "wrap_handoff_context",
]
