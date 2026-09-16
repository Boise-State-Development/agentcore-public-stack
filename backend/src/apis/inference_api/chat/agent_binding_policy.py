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

⚠️ **Persisting is necessary but not sufficient, and that trips people up.** Every turn's
Agent is resolved from the *request*; the SPA's only carrier is the ``assistantId`` query
param, and the self-heal effect that refills it from preferences runs on session *load*.
So a turn that binds must also leave the param on the URL, or the very next message in the
same live page arrives with no Agent at all.

**A mention on an empty thread binds** (changed 2026-09-14). D11 originally read the
`@`-mention as borrowing the Agent for one turn and nothing more, which left the next
message plain. Measured against real traffic that reading was backwards: of 247 mentions in
prod, **247 began the conversation** and none was a mid-thread consult. The per-turn
borrow's failure mode is also invisible — the turn after a mention silently loses the
Agent's tools, skills and model, and the model, having no idea its toolset changed,
explains a missing tool as something the user should fix in the tool picker. So a mention
that *starts* a thread is a launch and binds like one.

The SPA no longer sends ``agent_mention`` at all: a mention sets the ``assistantId`` param,
and mentioning inside a thread that already has messages opens a **new** conversation with
that Agent rather than borrowing it for a turn. The flag is still honoured here for clients
that predate that change — and a stale tab mentioning into a fresh thread now gets a bound
conversation like everyone else, which is the case that actually happens.

The one remaining non-binding Agent turn is **preview** — the Agent/assistant editor's own
scratch session, which persists nothing by design.

⚠️ This decides *binding*, never *authorization*. The Agent itself is still resolved
through ``get_assistant_with_access_check`` on every turn, so a client that lies about
``agent_mention`` gains nothing: the worst it can do is decline to save a binding it would
otherwise have saved.
"""

from __future__ import annotations


def binds_conversation(
    *,
    is_agent_mention: bool,
    is_preview: bool,
    thread_is_empty: bool = False,
) -> bool:
    """True when this turn's Agent should be validated against, and written to, the session.

    Args:
        is_agent_mention: The Agent was ``@``-mentioned for this turn only (D11). Only
            legacy clients still send this; the current SPA turns a mention into a bound
            conversation before the request is built.
        is_preview: The session is an editor preview, which persists nothing.
        thread_is_empty: The session has no messages yet, so binding an Agent to it is the
            same act as launching one — there is no history produced under other
            instructions for the binding to contradict. Defaults False so a caller that
            cannot cheaply answer it keeps the conservative pre-existing behaviour.
    """
    if is_preview:
        return False
    return not is_agent_mention or thread_is_empty
