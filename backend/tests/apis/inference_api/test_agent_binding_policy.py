"""Agent Marketplace Phase 7 — does this turn's Agent bind the conversation? (D11)

The rule is small; what it guards is not. Binding does two things that must move together:
it *refuses* a second Agent (and any Agent at all once the thread has messages), and it
*persists* ``preferences.assistant_id``, which the SPA self-heals its ``assistantId`` query
param from on every later load.

Split them and you get one of two bugs, both silent:

* validate but don't persist → the first mention works, the second is refused as an
  attempt to "change assistants mid-session";
* persist but don't validate → one `@` annexes the whole conversation, and every later
  message goes to the mentioned Agent whether the user wanted that or not.

So both call sites in ``routes.py`` ask this one question.
"""

import pytest

from apis.inference_api.chat.agent_binding_policy import binds_conversation


@pytest.mark.parametrize(
    "is_agent_mention,is_preview,expected",
    [
        # An ordinary Agent conversation: validated and persisted.
        (False, False, True),
        # A mention borrows the Agent for one turn (D11).
        (True, False, False),
        # An editor preview persists nothing by design.
        (False, True, False),
        # A mention inside a preview is still not a binding.
        (True, True, False),
    ],
)
def test_binding_matrix(is_agent_mention, is_preview, expected):
    assert (
        binds_conversation(is_agent_mention=is_agent_mention, is_preview=is_preview)
        is expected
    )


def test_a_plain_agent_turn_is_the_only_thing_that_binds():
    """Stated positively, because this is the case the two guards exist for."""
    assert binds_conversation(is_agent_mention=False, is_preview=False) is True


def test_a_mention_never_binds_however_it_is_combined():
    """A mention is the user naming who answers *this* message — never a commitment."""
    assert not binds_conversation(is_agent_mention=True, is_preview=False)
    assert not binds_conversation(is_agent_mention=True, is_preview=True)
