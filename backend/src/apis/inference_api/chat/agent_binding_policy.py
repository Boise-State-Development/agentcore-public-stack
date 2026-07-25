"""Whether the Agent running a turn *binds* the conversation to itself.

This sits in its own module for the same reason ``system_prompt_resolver`` does: the rule
is three lines and the route that applies it is a thousand, so the only way it gets tested
is if it lives somewhere a test can reach without an agent + invocation stack.

Binding is what makes a conversation "an Agent conversation":

* it is **validated** — a bound conversation refuses a second Agent, and refuses to be
  bound at all once it has messages, because its history was produced under different
  instructions, tools and skills;
* it is **persisted** — ``preferences.assistant_id`` is what the SPA self-heals its
  ``assistantId`` query param from, so a written binding survives reload forever.

Two kinds of turn run an Agent without binding, and they must skip *both* halves together
— validating without persisting would refuse the second mention in a thread, and
persisting without validating would let a mention quietly annex the conversation:

* **`@`-mention** (Marketplace D11) — the user handed this one turn to an Agent from the
  composer. Mentioning inside a thread that already has messages is the normal case, not
  the error case, and the next unmentioned message is plain chat again.
* **preview** — the Agent/assistant editor's own scratch session, which persists nothing
  by design.

⚠️ This decides *binding*, never *authorization*. The Agent itself is still resolved
through ``get_assistant_with_access_check`` on every turn, so a client that lies about
``agent_mention`` gains nothing: the worst it can do is decline to save a binding it would
otherwise have saved.
"""

from __future__ import annotations


def binds_conversation(*, is_agent_mention: bool, is_preview: bool) -> bool:
    """True when this turn's Agent should be validated against, and written to, the session.

    Args:
        is_agent_mention: The Agent was ``@``-mentioned for this turn only (D11).
        is_preview: The session is an editor preview, which persists nothing.
    """
    return not is_agent_mention and not is_preview
