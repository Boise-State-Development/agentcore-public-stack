"""The stored continue prompt is marked on `GET /messages`, so no client shows it as the user's words.

On a model that refuses assistant prefill, Continue persists one fixed user
turn (`CONTINUATION_PROMPT`). The SPA used to render it after a reload as a
user bubble the user never typed, splitting one answer into two. The flag is
derived here, beside the constant, so the SPA never mirrors the string.
"""

import pytest

from apis.shared.models.assistant_prefill import CONTINUATION_PROMPT, is_continuation_prompt
from apis.shared.sessions.messages import _convert_message_to_response
from apis.shared.sessions.models import Message, MessageContent


def _message(role, *blocks):
    return Message(role=role, content=list(blocks))


def _text(text):
    return MessageContent(type="text", text=text)


def test_the_continue_prompt_is_flagged():
    resp = _convert_message_to_response(_message("user", _text(CONTINUATION_PROMPT)), "s1", 3)
    assert resp.continuation is True
    assert resp.id == "msg-s1-3"
    # The content stays: the flag changes how it renders, not what is stored.
    assert resp.content[0].text == CONTINUATION_PROMPT


@pytest.mark.parametrize(
    "message",
    [
        _message("user", _text("hello")),
        _message("assistant", _text(CONTINUATION_PROMPT)),
        _message("user", _text(CONTINUATION_PROMPT), _text("and also this")),
        _message("user", _text(CONTINUATION_PROMPT + " ")),
        _message("user", MessageContent(type="toolResult", toolResult={"toolUseId": "t1", "content": []})),
    ],
    ids=["ordinary-user", "assistant-role", "extra-block", "not-exact", "tool-result"],
)
def test_anything_else_is_not_flagged(message):
    resp = _convert_message_to_response(message, "s1", 0)
    assert resp.continuation is None
    # Omitted from the wire, so every other message is byte-identical to before.
    assert "continuation" not in resp.model_dump(exclude_none=True, by_alias=True)


def test_predicate_reads_role_and_text_only():
    assert is_continuation_prompt("user", [CONTINUATION_PROMPT])
    assert not is_continuation_prompt("user", [])
    assert not is_continuation_prompt("user", [None])
    assert not is_continuation_prompt(None, [CONTINUATION_PROMPT])
