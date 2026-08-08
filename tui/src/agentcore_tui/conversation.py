"""Conversation state: the domain layer between the wire and the widgets.

This exists because conversation state used to be nine mutable attributes on the
``App``. That works for exactly one screen. A conversation list, a history
browser and an assistant preview all need the same state, and reaching
``self.app._history`` from a second screen is how a UI becomes impossible to
change.

Nothing here imports Textual or httpx. It is a plain object graph, so it can be
unit-tested directly and reused by any screen.

Two facts about the platform are encoded here deliberately:

* **Session ids are minted by the client.** There is no create-session endpoint;
  the SPA generates a uuid4 on the first turn and the server persists against
  it. So a conversation has an id from the moment it exists, before any request.
* **A message is not a wire DTO.** Server-side messages carry an id, a
  timestamp, token usage, and later attachments and a tool trace. Storing the
  two-field request shape would mean rewriting every call site when history
  arrives.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

from .usage import Usage

Role = Literal["user", "assistant", "system"]


def new_session_id() -> str:
    """Mint a conversation id.

    uuid4, matching the SPA (``chat-request.service.ts`` does the same) so ids
    from either client are indistinguishable server-side.
    """
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True)
class Message:
    """One message in a conversation.

    Frozen: a message is a record of something that happened. Streaming builds
    the text in a buffer and appends the finished message once, rather than
    mutating a stored one, which keeps "what is in the store" unambiguous while
    a turn is in flight.
    """

    role: Role
    content: str
    #: Client-side identity, for widget keys and reconciliation with server ids.
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)
    #: Extended-thinking content, kept separate so it can be rendered muted.
    reasoning: str = ""
    #: Token counts, when the server reported them for this turn.
    usage: Usage | None = None

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str, *, reasoning: str = "", usage: Usage | None = None) -> Message:
        return cls(role="assistant", content=content, reasoning=reasoning, usage=usage)

    def with_usage(self, usage: Usage | None) -> Message:
        return replace(self, usage=usage)


class ConversationStore:
    """Owns one conversation.

    Mutable by design — it is the single place the turn controller and the
    screens agree on. Kept free of change notifications for now: there is one
    reader. When a second screen observes it, add subscribers here rather than
    letting callers poll.
    """

    __slots__ = ("_messages", "_session_id", "_title")

    def __init__(self, *, session_id: str | None = None, title: str | None = None) -> None:
        self._session_id = session_id or new_session_id()
        self._title = title
        self._messages: list[Message] = []

    # -- identity ------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def title(self) -> str | None:
        return self._title

    def set_title(self, title: str | None) -> None:
        """Adopt a server-generated title.

        Titles can arrive *after* the stream's terminal event, so this must be
        callable outside a turn.
        """
        self._title = title or None

    # -- contents ------------------------------------------------------------

    @property
    def messages(self) -> Sequence[Message]:
        """Read-only view. Mutate through :meth:`append`."""
        return tuple(self._messages)

    def __len__(self) -> int:
        return len(self._messages)

    def __iter__(self) -> Iterator[Message]:
        return iter(self._messages)

    @property
    def is_empty(self) -> bool:
        return not self._messages

    def append(self, message: Message) -> Message:
        self._messages.append(message)
        return message

    def append_user(self, content: str) -> Message:
        return self.append(Message.user(content))

    def append_assistant(self, content: str, *, reasoning: str = "", usage: Usage | None = None) -> Message:
        return self.append(Message.assistant(content, reasoning=reasoning, usage=usage))

    def replace_all(self, messages: Sequence[Message]) -> None:
        """Load history wholesale, as when opening a stored conversation."""
        self._messages = list(messages)

    # -- derived -------------------------------------------------------------

    @property
    def turns(self) -> int:
        """Completed exchanges, counted by user messages."""
        return sum(1 for message in self._messages if message.role == "user")

    @property
    def last_assistant(self) -> Message | None:
        for message in reversed(self._messages):
            if message.role == "assistant":
                return message
        return None

    @property
    def latest_usage(self) -> Usage | None:
        message = self.last_assistant
        return message.usage if message else None

    def to_markdown(self) -> str:
        """The whole conversation as Markdown, for the clipboard."""
        headings = {"user": "You", "assistant": "Assistant", "system": "System"}
        return "\n\n".join(f"## {headings.get(m.role, m.role)}\n\n{m.content}" for m in self._messages)

    # -- lifecycle -----------------------------------------------------------

    def reset(self) -> str:
        """Start a fresh conversation, returning the new session id.

        A new id rather than a cleared list: the old conversation may already be
        persisted server-side under the previous id, and reusing it would append
        to it.
        """
        self._messages.clear()
        self._title = None
        self._session_id = new_session_id()
        return self._session_id

    def adopt(self, session_id: str, messages: Sequence[Message], *, title: str | None = None) -> None:
        """Become an existing server-side conversation.

        The counterpart to :meth:`reset`: that one walks away from a
        conversation, this one walks into one. Both the id *and* the messages
        have to change together — adopting the history while keeping the local id
        would send the next turn to a conversation the user is not looking at,
        and adopting the id without the history would show an empty transcript
        for a conversation the server has messages for.
        """
        self._session_id = session_id
        self._messages = list(messages)
        self._title = title or None
