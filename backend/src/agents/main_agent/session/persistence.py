"""Helper for persisting synthetic conversational messages to AgentCore Memory.

Used by code paths that need to write a message to session history without
going through Strands' ``MessageAddedEvent`` hook — error handlers in the
streaming layer, quota-exceeded short-circuits, and similar.

WHY THIS HELPER EXISTS:
Three call sites had near-identical persistence code with the same broken
guard (``hasattr(session_manager, "base_manager")``). The guard was always
False against the current SDK — ``AgentCoreMemorySessionManager`` exposes
``create_message`` directly, with no nested ``.base_manager`` wrapper — so
every synthetic write was silently skipped. That was the root cause of
the "assistant error visible live, gone after refresh" bug. Centralizing
the contract here surfaces the failure mode loudly and prevents the same
shape of bug from drifting back into individual call sites.

CANONICAL REFERENCE for the "user turn already persisted" invariant:
The ``messages`` argument's docstring below is the single source of truth
for *which roles to pass* in each scenario (assistant-only for paths
inside the agent stream; user+assistant for paths that short-circuit
before the agent runs). Call sites in ``stream_coordinator`` and
``chat/routes.py`` repeat the high-level reasoning inline; if you need to
revisit the invariant, start here.

ROLE-ALTERNATION GUARD (also canonical here):
Passing ``last_persisted_role`` makes this helper drop any synthetic turn
that would create two consecutive same-role messages — the corruption that
bricks a session under Bedrock's strict alternation rule. Centralizing it
here protects every caller that writes an assistant turn after an error,
consistent with this module being the single source of truth.
"""

import logging
from typing import Any, List, Optional, Tuple

from strands.types.content import Message
from strands.types.session import SessionMessage

from apis.shared.security.log_sanitize import scrub_log

logger = logging.getLogger(__name__)


def persist_synthetic_messages(
    session_manager: Any,
    session_id: str,
    messages: List[Tuple[str, str]],
    *,
    agent_id: str = "default",
    last_persisted_role: Optional[str] = None,
) -> bool:
    """Write one or more synthetic ``(role, text)`` messages to a session.

    Args:
        session_manager: A session manager exposing ``create_message`` —
            typically the object returned by ``SessionFactory.create_session_manager``.
            A legacy nested ``.base_manager`` is also honored if the SDK
            ever reintroduces that indirection.
        session_id: AgentCore Memory session ID.
        messages: List of ``(role, text)`` tuples in order. Use
            ``[("assistant", ...)]`` for paths where the user turn was
            already persisted by Strands' ``MessageAddedEvent`` hook at
            turn start (any error fired from inside the agent stream).
            Use ``[("user", ...), ("assistant", ...)]`` for paths where the
            agent never ran (quota-exceeded short-circuit, etc.) and the
            user turn has not been written yet.
        agent_id: AgentCore Memory agent_id. Defaults to ``"default"`` to
            match read paths in ``apis.shared.sessions.messages``.
        last_persisted_role: Role of the message already at the tail of the
            session (``"user"`` / ``"assistant"``), typically
            ``agent.messages[-1]["role"]``. When provided, this helper drops
            any synthetic turn that would land adjacent to a same-role turn,
            preserving Bedrock's strict user/assistant alternation — see the
            ROLE-ALTERNATION GUARD below. Pass ``None`` (the default) to
            persist verbatim, e.g. the quota-exceeded path that writes a
            fresh ``user`` + ``assistant`` pair onto an empty session.

    Returns:
        ``True`` if every message that survived alternation filtering was
        written (including the case where the guard dropped all of them —
        that is a successful no-op, not a failure). ``False`` (with an ERROR
        log) if the session manager has no ``create_message`` method — the
        failure mode that previously went silent.

    Raises:
        Whatever ``create_message`` raises is propagated. Callers wrap
        with their own try/except so the failure appears in logs at
        the call site rather than being swallowed here.
    """
    # ROLE-ALTERNATION GUARD (single source of truth for all callers).
    #
    # Bedrock Converse requires strict user/assistant alternation. TWO
    # consecutive same-role turns ANYWHERE in stored history make EVERY
    # subsequent turn on that session fail with a ValidationException,
    # permanently bricking the conversation. The classic amplifier: a turn
    # ends with a dangling assistant toolUse (or a prior synthetic error
    # turn), an error fires, and the error handler appends ANOTHER assistant
    # turn — assistant, assistant — which then fails the next turn, which
    # persists yet another assistant error, and so on.
    #
    # When the caller knows the tail role (``last_persisted_role``), skip any
    # synthetic turn that would sit next to a same-role turn. In practice
    # this is the "error persisted after a dangling assistant turn" case: the
    # synthetic assistant message is dropped and the error stays a live-only
    # UI affordance for that turn — the same deliberate choice the max_tokens
    # path already makes (see ``stream_coordinator``). Only the FIRST tuple can
    # collide with the tail; the tuples within ``messages`` already alternate,
    # so we thread ``prev_role`` through the loop to stay correct for any
    # multi-message batch.
    if last_persisted_role is not None:
        filtered: List[Tuple[str, str]] = []
        prev_role = last_persisted_role
        for role, text in messages:
            if role == prev_role:
                logger.warning(
                    f"Skipping synthetic {role} message for session "
                    f"{scrub_log(session_id)} to preserve role alternation "
                    f"(tail role is already {role})"
                )
                continue
            filtered.append((role, text))
            prev_role = role
        messages = filtered

    if not messages:
        logger.info(
            f"No synthetic messages to persist to session {scrub_log(session_id)} "
            f"after role-alternation filtering"
        )
        return True

    target_manager = next(
        (
            m
            for m in (session_manager, getattr(session_manager, "base_manager", None))
            if m is not None and hasattr(m, "create_message")
        ),
        None,
    )
    if target_manager is None:
        logger.error(
            f"Cannot persist messages to session {scrub_log(session_id)}: "
            f"session manager {type(session_manager).__name__} has no create_message method"
        )
        return False

    for index, (role, text) in enumerate(messages):
        msg: Message = {"role": role, "content": [{"text": text}]}
        session_msg = SessionMessage.from_message(msg, index)
        target_manager.create_message(session_id, agent_id, session_msg)

    logger.info(
        f"💾 Persisted {len(messages)} synthetic message(s) to session {scrub_log(session_id)}"
    )
    return True
