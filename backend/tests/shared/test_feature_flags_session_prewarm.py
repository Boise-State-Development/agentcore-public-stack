"""`SESSION_PREWARM_ENABLED` is opt-in while in development (CLAUDE.md "Feature flags")."""

import pytest

from apis.shared.feature_flags import session_prewarm_enabled


@pytest.mark.parametrize("value", ["true", "TRUE", " True "])
def test_only_true_enables(monkeypatch, value):
    monkeypatch.setenv("SESSION_PREWARM_ENABLED", value)
    assert session_prewarm_enabled()


@pytest.mark.parametrize("value", ["", "false", "yes", "1", "on"])
def test_anything_else_is_off(monkeypatch, value):
    monkeypatch.setenv("SESSION_PREWARM_ENABLED", value)
    assert not session_prewarm_enabled()


def test_unset_is_off(monkeypatch):
    monkeypatch.delenv("SESSION_PREWARM_ENABLED", raising=False)
    assert not session_prewarm_enabled()
