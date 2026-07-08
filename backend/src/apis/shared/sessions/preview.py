"""Preview-session detection — a dependency-free leaf helper.

Preview sessions are the in-memory, never-persisted sessions the assistant
form-builder uses for testing (they keep conversation context for the turn
but are deliberately excluded from a user's saved history). Detecting one is
a pure prefix check.

This lives in ``apis.shared`` — with **no imports** — on purpose: the
canonical implementation used to live in
``agents.main_agent.session.preview_session_manager``, whose module also
pulls in ``strands`` and ``agents.main_agent.config`` for the unrelated
``PreviewSessionManager`` class. That made ``apis.shared.sessions.metadata``
(which only needs the prefix check) transitively depend on the whole agent
stack, and the lean scheduled-runs Lambda image — which deliberately omits
``agents``/``strands`` — crashed at delivery time trying to import it.

The ``"preview-"`` literal is mirrored in
``agents.main_agent.config.constants.Prefixes.PREVIEW_SESSION`` and
``apis.inference_api.chat.routes``; ``tests`` assert they stay in lockstep
(``test_preview_prefix_consistency``).
"""

#: Session-id prefix marking an in-memory preview session (no persistence).
PREVIEW_SESSION_PREFIX = "preview-"


def is_preview_session(session_id: str) -> bool:
    """True for an in-memory preview session (excluded from persistence)."""
    return session_id.startswith(PREVIEW_SESSION_PREFIX)
