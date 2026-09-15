"""Agent Marketplace Phase 7 (D11, revised) — does this turn's Agent bind the conversation?

The rule is small; what it guards is not. Binding does two things that must move together:
it *refuses* a second Agent (and any Agent at all once the thread has messages), and it
*persists* ``preferences.assistant_id``, which the SPA self-heals its ``assistantId`` query
param from on every later load.

Split them and you get one of two bugs, both silent:

* validate but don't persist → the first mention works, the second is refused as an
  attempt to "change assistants mid-session";
* persist but don't validate → one `@` annexes the whole conversation, and every later
  message goes to the mentioned Agent whether the user wanted that or not.

So both call sites in ``routes.py`` ask this one question — and, since the thread-empty
case was added, they must ask it with the *same arguments*.

**Why a mention on an empty thread now binds.** The original per-turn reading left the next
message plain, which silently stripped the Agent's tools, skills and model from a
conversation the user still believed was the Agent's. Measured in prod: of 247 mentions,
247 started the conversation and none was a mid-thread consult, so the per-turn borrow was
paying a real failure mode for a case nobody used.
"""

import pytest

from apis.inference_api.chat.agent_binding_policy import binds_conversation


@pytest.mark.parametrize(
    "is_agent_mention,is_preview,thread_is_empty,expected",
    [
        # An ordinary Agent conversation: validated and persisted, empty or not.
        (False, False, True, True),
        (False, False, False, True),
        # A mention that STARTS a thread is a launch — it binds.
        (True, False, True, True),
        # A mention into a thread that already has messages does not (legacy clients
        # only; the SPA opens a new conversation instead of sending this).
        (True, False, False, False),
        # An editor preview persists nothing by design — including on an empty thread,
        # which every preview session is.
        (False, True, True, False),
        (False, True, False, False),
        (True, True, True, False),
        (True, True, False, False),
    ],
)
def test_binding_matrix(is_agent_mention, is_preview, thread_is_empty, expected):
    assert (
        binds_conversation(
            is_agent_mention=is_agent_mention,
            is_preview=is_preview,
            thread_is_empty=thread_is_empty,
        )
        is expected
    )


def test_preview_never_binds_whatever_else_is_true():
    """Preview is the one unconditional no: it is a scratch session by construction."""
    for mention in (True, False):
        for empty in (True, False):
            assert not binds_conversation(
                is_agent_mention=mention, is_preview=True, thread_is_empty=empty
            )


def test_a_thread_starting_mention_binds():
    """The case the change exists for, stated positively.

    Before this, the turn ran as the Agent and the *next* one did not — with no signal
    to the user and none to the model, which then explained a vanished tool as something
    to fix in the tool picker.
    """
    assert binds_conversation(
        is_agent_mention=True, is_preview=False, thread_is_empty=True
    )


def test_thread_is_empty_defaults_to_the_conservative_answer():
    """Omitting the argument must not silently upgrade a mention into a binding.

    The default exists for callers that cannot cheaply answer it; it has to mean "assume
    there is history", because the opposite default would let a mid-thread mention annex
    a conversation whenever someone forgot to pass it.
    """
    assert not binds_conversation(is_agent_mention=True, is_preview=False)
    assert binds_conversation(is_agent_mention=False, is_preview=False)
