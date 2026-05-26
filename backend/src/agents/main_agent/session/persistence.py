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
"""

import logging
from typing import Any, List, Tuple

from strands.types.content import Message
from strands.types.session import SessionMessage

logger = logging.getLogger(__name__)


def persist_synthetic_messages(
    session_manager: Any,
    session_id: str,
    messages: List[Tuple[str, str]],
    *,
    agent_id: str = "default",
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

    Returns:
        ``True`` if all messages were written. ``False`` (with an ERROR
        log) if the session manager has no ``create_message`` method —
        the failure mode that previously went silent.

    Raises:
        Whatever ``create_message`` raises is propagated. Callers wrap
        with their own try/except so the failure appears in logs at
        the call site rather than being swallowed here.
    """
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
            f"Cannot persist messages to session {session_id}: "
            f"session manager {type(session_manager).__name__} has no create_message method"
        )
        return False

    for index, (role, text) in enumerate(messages):
        msg: Message = {"role": role, "content": [{"text": text}]}
        session_msg = SessionMessage.from_message(msg, index)
        target_manager.create_message(session_id, agent_id, session_msg)

    logger.info(
        f"💾 Persisted {len(messages)} synthetic message(s) to session {session_id}"
    )
    return True
