"""Handoffs: carrying an ``@``-mention's work into a new conversation.

``docs/specs/agent-handoffs-and-consults.md``. This package holds the parts both services need,
so ``app_api`` and ``inference_api`` import from here and never from each other:

- ``excerpt``: the deterministic, bounded conversation excerpt (§4.5)
- ``context``: how that excerpt is wrapped for the Agent, and joined to the request (§4.2)
- ``models`` / ``store``: the ``HANDOFF#`` row and its states (§4.1)
"""

from apis.shared.handoff.context import (
    build_handoff_message,
    context_hash,
    wrap_handoff_context,
)
from apis.shared.handoff.excerpt import HANDOFF_EXCERPT_MAX_TOKENS, Excerpt, render_excerpt
from apis.shared.handoff.models import Handoff, HandoffKind, HandoffStatus, TurnsCovered
from apis.shared.handoff.store import HandoffStore, HandoffStoreUnavailable, get_handoff_store

__all__ = [
    "Excerpt",
    "HANDOFF_EXCERPT_MAX_TOKENS",
    "Handoff",
    "HandoffKind",
    "HandoffStatus",
    "HandoffStore",
    "HandoffStoreUnavailable",
    "TurnsCovered",
    "build_handoff_message",
    "context_hash",
    "get_handoff_store",
    "render_excerpt",
    "wrap_handoff_context",
]
