"""What an archived turn is, and how one is cut from a conversation.

Pure functions over Converse-shaped messages (``{"role", "content": [...]}``)
with no AWS access, so the runtime, app-api's fork path and the backfill
script all build byte-identical objects from the same history.

A **turn** is the user message that started it plus every assistant message
that answered it, up to the next user message that carries the user's own
words. Tool-result user messages sit inside a turn, not between turns:
Strands appends them as role ``user``, so a turn that called two tools is
user, assistant, user(toolResult), assistant, user(toolResult), assistant.
The turn is keyed by the index of its first message, which is the message
that carries the SPA's ``message-{id}`` anchor, so a search hit can jump to it.

Only text is kept: the user's words and the assistant's prose. Tool calls,
tool results, reasoning and attachments are dropped. Tool results are most of
a session's bytes, the part most likely to hold third-party data, and nothing
anyone searches for from memory (docs/specs/conversation-search.md §4).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, List, Mapping, Optional, Sequence

#: Every archived turn lives under this prefix; the bucket's retention
#: lifecycle rule targets it (``ConversationArchiveConstruct``).
ARCHIVE_PREFIX = "conversations/"

#: Bumped when the object body changes shape, so a reader can tell old from new.
SCHEMA_VERSION = 1

#: Cap on a turn's text, in UTF-8 bytes. A long assistant answer still fits;
#: the Managed KB chunks internally. The user's text gets at most half, so a
#: pasted wall of text cannot crowd out the answer it was asked about.
MAX_TURN_TEXT_BYTES = 16 * 1024
MAX_USER_TEXT_BYTES = MAX_TURN_TEXT_BYTES // 2

# The marker ``PromptBuilder.build_prompt`` appends to a user message that
# carried attachments. Not the user's words, so it is not indexed.
_ATTACHED_FILES_MARKER = re.compile(r"\n*\[Attached files: [^\]]+\]\s*$")


@dataclass(frozen=True)
class ArchivedTurn:
    """One turn's text, as stored at ``key``."""

    user_id: str
    session_id: str
    message_index: int
    user_text: str
    assistant_text: str
    created_at: str
    project_id: Optional[str] = None
    assistant_id: Optional[str] = None

    @property
    def key(self) -> str:
        return archive_key(self.user_id, self.session_id, self.message_index)

    def to_json(self) -> bytes:
        body: dict = {
            "schemaVersion": SCHEMA_VERSION,
            "userId": self.user_id,
            "sessionId": self.session_id,
            "messageIndex": self.message_index,
            "userText": self.user_text,
            "assistantText": self.assistant_text,
            "createdAt": self.created_at,
        }
        if self.project_id:
            body["projectId"] = self.project_id
        if self.assistant_id:
            body["assistantId"] = self.assistant_id
        return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _require_key_part(name: str, value: str) -> str:
    # A slash would let one id address another session's objects, and an empty
    # part collapses the hierarchy the prefix delete relies on.
    if not value or "/" in value:
        raise ValueError(f"invalid {name} for an archive key")
    return value


def session_prefix(user_id: str, session_id: str) -> str:
    """The key prefix holding every archived turn of one session."""
    return (
        f"{ARCHIVE_PREFIX}{_require_key_part('user_id', user_id)}/"
        f"{_require_key_part('session_id', session_id)}/"
    )


def archive_key(user_id: str, session_id: str, message_index: int) -> str:
    """``conversations/{user}/{session}/{index:06d}.json``.

    Zero-padded so a prefix listing returns turns in conversation order.
    """
    if message_index < 0:
        raise ValueError("message_index must be non-negative")
    return f"{session_prefix(user_id, session_id)}{message_index:06d}.json"


def _blocks(message: Mapping[str, Any]) -> List[Any]:
    content = message.get("content")
    return content if isinstance(content, list) else []


def message_text(message: Mapping[str, Any]) -> str:
    """The message's prose: its ``text`` blocks joined, everything else dropped."""
    parts = []
    for block in _blocks(message):
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n\n".join(parts)


def is_turn_start(message: Mapping[str, Any]) -> bool:
    """Whether this message is a user turn rather than a tool round-trip.

    A user message that carries a ``toolResult`` is Strands handing tool output
    back to the model, even when mid-turn steering appended the user's typed
    follow-up to it: that follow-up belongs to the turn already running.
    """
    if message.get("role") != "user":
        return False
    blocks = [b for b in _blocks(message) if isinstance(b, dict)]
    if any("toolResult" in b for b in blocks):
        return False
    return any(isinstance(b.get("text"), str) and b["text"].strip() for b in blocks)


def clean_user_text(text: str) -> str:
    """The user's words without the attachments marker the prompt builder adds."""
    return _ATTACHED_FILES_MARKER.sub("", text).strip()


def _truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    # `ignore` drops a multi-byte character the cut landed inside.
    return encoded[:max_bytes].decode("utf-8", "ignore")


def build_turn(
    *,
    user_id: str,
    session_id: str,
    message_index: int,
    user_text: str,
    assistant_messages: Iterable[Mapping[str, Any]],
    created_at: str,
    project_id: Optional[str] = None,
    assistant_id: Optional[str] = None,
) -> Optional[ArchivedTurn]:
    """An archived turn, capped at ``MAX_TURN_TEXT_BYTES``; None if it holds no text."""
    user = _truncate_utf8(clean_user_text(user_text), MAX_USER_TEXT_BYTES)
    assistant = "\n\n".join(
        t for t in (message_text(m) for m in assistant_messages if m.get("role") == "assistant") if t
    )
    assistant = _truncate_utf8(assistant, MAX_TURN_TEXT_BYTES - len(user.encode("utf-8")))
    if not user and not assistant:
        return None
    return ArchivedTurn(
        user_id=user_id,
        session_id=session_id,
        message_index=message_index,
        user_text=user,
        assistant_text=assistant,
        created_at=created_at,
        project_id=project_id or None,
        assistant_id=assistant_id or None,
    )


def with_user_text(turn: ArchivedTurn, user_text: str) -> ArchivedTurn:
    """``turn`` with its user text replaced, the caps applied again."""
    user = _truncate_utf8(clean_user_text(user_text), MAX_USER_TEXT_BYTES)
    assistant = _truncate_utf8(turn.assistant_text, MAX_TURN_TEXT_BYTES - len(user.encode("utf-8")))
    return replace(turn, user_text=user, assistant_text=assistant)


def last_turn_start(messages: Sequence[Mapping[str, Any]]) -> Optional[int]:
    """Position of the last turn's opening user message, or None."""
    for pos in range(len(messages) - 1, -1, -1):
        if is_turn_start(messages[pos]):
            return pos
    return None


def split_turns(
    messages: Sequence[Mapping[str, Any]],
    *,
    user_id: str,
    session_id: str,
    created_at: str,
    base_index: int = 0,
    project_id: Optional[str] = None,
    assistant_id: Optional[str] = None,
) -> List[ArchivedTurn]:
    """Every turn in ``messages``, where ``messages[0]`` sits at ``base_index``.

    Messages before the first turn start (a history that opens mid-turn) are
    skipped: they have no user words to anchor a result to.
    """
    turns: List[ArchivedTurn] = []
    start: Optional[int] = None

    def close(end: int) -> None:
        if start is None:
            return
        turn = build_turn(
            user_id=user_id,
            session_id=session_id,
            message_index=base_index + start,
            user_text=message_text(messages[start]),
            assistant_messages=messages[start + 1 : end],
            created_at=created_at,
            project_id=project_id,
            assistant_id=assistant_id,
        )
        if turn is not None:
            turns.append(turn)

    for pos, message in enumerate(messages):
        if is_turn_start(message):
            close(pos)
            start = pos
    close(len(messages))
    return turns
