"""The conversation excerpt a handoff carries (``docs/specs/agent-handoffs-and-consults.md`` §4.5).

A pure function over persisted messages: deterministic, no model call, no I/O. app-api renders it
when a handoff is created, and Phase 1's consults will render it in inference-api, so it lives in
``apis.shared`` and takes plain Converse-shaped dicts rather than either service's models::

    <conversation_excerpt turns="7-12" of="12">
    User: …
    Assistant: …
    [tool: list_assignments]
    [attachment: syllabus.pdf]
    </conversation_excerpt>

- **Text only.** User and assistant text go in verbatim; a user message's ``displayText`` wins
  over its model text, so RAG chunks, folder manifests and an earlier handoff's wrapper never
  ride along. Each tool call is one line, tool results are left out, attachments are named.
- **Bounded.** ``HANDOFF_EXCERPT_MAX_TOKENS``, estimated at 4 characters a token. CountTokens
  would cost ~80 ms per call for an answer this doesn't need. Over the cap, the oldest turns are
  dropped whole behind an ``[N earlier turns omitted]`` marker. A newest turn too large on its own
  is clipped in the middle, so its question and the end of its answer both survive.
- **Compaction-aware.** Turns before the applied compaction checkpoint are represented by that
  checkpoint's summary, which is what the conversation's own model sees for them.
- **Watermark-capable.** ``since_index`` skips messages already carried; ``end_index`` on the
  result is the next watermark. Phase 0 always starts at 0; Phase 1's repeated consults won't.

A turn starts at a user message that carries no ``toolResult``. A message that does is the
middle of a turn, and its only text worth carrying is a mid-turn steer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from apis.shared.handoff.context import EXCERPT_TAG, neutralize_close_tags

HANDOFF_EXCERPT_MAX_TOKENS = 4_000
CHARS_PER_TOKEN = 4

#: Room kept for the tags and the omitted-turns marker, so the whole block fits the cap.
_FRAME_RESERVE_CHARS = 160

_STEER_OPEN = "<user_message_during_turn>"
_STEER_CLOSE = "</user_message_during_turn>"


@dataclass(frozen=True)
class Excerpt:
    """A rendered excerpt and the facts the review sheet and the ``HANDOFF#`` row need."""

    text: str
    estimated_tokens: int
    first_turn: int
    last_turn: int
    total_turns: int
    omitted_turns: int
    end_index: int

    @property
    def is_empty(self) -> bool:
        return not self.text


@dataclass(frozen=True)
class _Unit:
    """One droppable piece of the excerpt: a turn, or the compaction summary standing in for many."""

    first_turn: int
    last_turn: int
    text: str


def render_excerpt(
    messages: Sequence[Mapping[str, Any]],
    *,
    compaction_checkpoint: int = 0,
    compaction_summary: Optional[str] = None,
    since_index: int = 0,
    max_tokens: int = HANDOFF_EXCERPT_MAX_TOKENS,
) -> Excerpt:
    """Render ``messages[since_index:]`` as a bounded ``<conversation_excerpt>`` block.

    ``messages`` are Converse-shaped dicts (``role``, ``content`` blocks keyed ``text`` /
    ``toolUse`` / ``toolResult`` / ``document`` / ``image``, optional ``metadata.displayText``),
    which is also what ``MessageResponse.model_dump(by_alias=True)`` produces. Turn numbers are
    counted over the whole conversation, so ``turns="7-12" of="12"`` means the same thing whatever
    ``since_index`` is.
    """
    since_index = max(0, since_index)
    end_index = len(messages)
    starts = _turn_starts(messages)
    total_turns = len(starts)

    units: List[_Unit] = []
    summary = (compaction_summary or "").strip()
    compacted = [n for n, start in enumerate(starts, 1) if start < compaction_checkpoint and start >= since_index]
    if summary and compacted:
        units.append(
            _Unit(
                compacted[0],
                compacted[-1],
                f"[Summary of turns {_span(compacted[0], compacted[-1])}]\n{neutralize_close_tags(summary)}",
            )
        )
    summarized = set(compacted) if summary else set()
    for number, start in enumerate(starts, 1):
        if start < since_index or number in summarized:
            continue
        stop = starts[number] if number < total_turns else end_index
        text = _render_turn(messages[start:stop])
        if text:
            units.append(_Unit(number, number, text))

    if not units:
        return Excerpt("", 0, 0, 0, total_turns, 0, end_index)

    budget = max(0, max_tokens * CHARS_PER_TOKEN - _FRAME_RESERVE_CHARS)
    kept = _fit(units, budget)
    omitted = sum(u.last_turn - u.first_turn + 1 for u in units[: len(units) - len(kept)])

    first, last = kept[0].first_turn, kept[-1].last_turn
    parts = [f'<{EXCERPT_TAG} turns="{_range_attr(first, last)}" of="{total_turns}">']
    if omitted:
        parts.append(f"[{omitted} earlier turn{'s' if omitted != 1 else ''} omitted]")
    parts.append("\n\n".join(u.text for u in kept))
    parts.append(f"</{EXCERPT_TAG}>")
    text = "\n".join(parts)
    return Excerpt(
        text=text,
        estimated_tokens=math.ceil(len(text) / CHARS_PER_TOKEN),
        first_turn=first,
        last_turn=last,
        total_turns=total_turns,
        omitted_turns=omitted,
        end_index=end_index,
    )


def _turn_starts(messages: Sequence[Mapping[str, Any]]) -> List[int]:
    """Index of the first message of each turn. Messages before the first user message join turn 1."""
    starts: List[int] = []
    for index, message in enumerate(messages):
        if message.get("role") != "user":
            continue
        blocks = _blocks(message)
        if blocks and not any("toolResult" in b for b in blocks):
            starts.append(index)
    if not starts and messages:
        return [0]
    if starts and starts[0] != 0:
        starts[0] = 0
    return starts


def _fit(units: List[_Unit], budget: int) -> List[_Unit]:
    """The newest units that fit ``budget`` together; the newest alone is clipped if it can't."""
    kept: List[_Unit] = []
    used = 0
    for unit in reversed(units):
        cost = len(unit.text) + (2 if kept else 0)
        if used + cost > budget:
            break
        kept.append(unit)
        used += cost
    if kept:
        return list(reversed(kept))
    newest = units[-1]
    return [_Unit(newest.first_turn, newest.last_turn, clip_middle(newest.text, budget))]


def clip_middle(text: str, budget: int) -> str:
    """Keep the head and tail of ``text`` within ``budget`` characters, marking the cut."""
    if len(text) <= budget:
        return text
    marker = f"\n[… {len(text) - budget} characters omitted …]\n"
    room = max(0, budget - len(marker))
    head = room // 2
    tail = room - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def _render_turn(messages: Sequence[Mapping[str, Any]]) -> str:
    lines: List[str] = []
    for message in messages:
        role = message.get("role")
        blocks = _blocks(message)
        if role == "user":
            lines.extend(_render_user(message, blocks))
        elif role == "assistant":
            lines.extend(_render_assistant(blocks))
    return "\n".join(lines)


def _render_user(message: Mapping[str, Any], blocks: List[Mapping[str, Any]]) -> List[str]:
    if any("toolResult" in b for b in blocks):
        # Mid-turn: the only user words here are a steer appended to the tool results.
        return [f"User: {_clean(steer)}" for steer in _steers(blocks) if _clean(steer)]

    lines: List[str] = []
    display = _display_text(message)
    if display is not None:
        if _clean(display):
            lines.append(f"User: {_clean(display)}")
    else:
        text = "\n".join(str(b["text"]) for b in blocks if _is_text(b)).strip()
        if text:
            lines.append(f"User: {_clean(text)}")
    lines.extend(_attachment_line(b) for b in blocks if "document" in b or "image" in b)
    return lines


def _render_assistant(blocks: List[Mapping[str, Any]]) -> List[str]:
    lines: List[str] = []
    for block in blocks:
        if "toolUse" in block:
            name = (block.get("toolUse") or {}).get("name") or "unknown"
            lines.append(f"[tool: {name}]")
        elif _is_text(block) and _clean(str(block["text"])):
            lines.append(f"Assistant: {_clean(str(block['text']))}")
    return lines


def _attachment_line(block: Mapping[str, Any]) -> str:
    if "document" in block:
        name = (block.get("document") or {}).get("name") or "document"
    else:
        name = (block.get("image") or {}).get("name") or "image"
    return f"[attachment: {_clean(str(name))}]"


def _steers(blocks: List[Mapping[str, Any]]) -> List[str]:
    found: List[str] = []
    for block in blocks:
        if not _is_text(block):
            continue
        text = str(block["text"]).strip()
        if text.startswith(_STEER_OPEN) and text.endswith(_STEER_CLOSE):
            found.append(text[len(_STEER_OPEN) : -len(_STEER_CLOSE)])
    return found


def _display_text(message: Mapping[str, Any]) -> Optional[str]:
    metadata = message.get("metadata")
    if isinstance(metadata, Mapping):
        value = metadata.get("displayText")
        if isinstance(value, str):
            return value
    return None


def _blocks(message: Mapping[str, Any]) -> List[Dict[str, Any]]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]


def _is_text(block: Mapping[str, Any]) -> bool:
    """A text block, in either the Converse shape or ``MessageContent``'s (which also has ``type``)."""
    if any(k in block for k in ("toolUse", "toolResult", "document", "image", "reasoningContent")):
        return False
    return isinstance(block.get("text"), str)


def _clean(text: str) -> str:
    return neutralize_close_tags(text.strip())


def _span(first: int, last: int) -> str:
    return str(first) if first == last else f"{first}–{last}"


def _range_attr(first: int, last: int) -> str:
    return str(first) if first == last else f"{first}-{last}"


__all__ = ["CHARS_PER_TOKEN", "Excerpt", "HANDOFF_EXCERPT_MAX_TOKENS", "clip_middle", "render_excerpt"]
