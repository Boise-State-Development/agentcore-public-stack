"""Guard: the preview-session prefix stays in lockstep across its copies.

``apis.shared.sessions.preview`` deliberately re-declares the ``"preview-"``
literal (rather than importing it from ``agents``) so the lean scheduled-runs
Lambda image can detect preview sessions without pulling in agents/strands.
This test fails loudly if that copy ever drifts from the canonical
``agents.main_agent.config.constants.Prefixes.PREVIEW_SESSION``.
"""

from apis.shared.sessions.preview import PREVIEW_SESSION_PREFIX, is_preview_session


def test_shared_prefix_matches_agents_constant() -> None:
    from agents.main_agent.config.constants import Prefixes

    assert PREVIEW_SESSION_PREFIX == Prefixes.PREVIEW_SESSION


def test_is_preview_session_behavior() -> None:
    assert is_preview_session("preview-abc123")
    assert not is_preview_session("headless-075af61855bf4240")
    assert not is_preview_session("")
