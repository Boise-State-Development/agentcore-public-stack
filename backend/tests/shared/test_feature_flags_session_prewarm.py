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


# `SESSION_PREWARM_PREPARE_ENABLED`: in development, app-api only, needs session prewarm on.
from apis.shared.feature_flags import session_prewarm_prepare_enabled  # noqa: E402


@pytest.mark.parametrize("value", ["true", "TRUE", " True "])
def test_prepare_only_true_enables(monkeypatch, value):
    monkeypatch.setenv("SESSION_PREWARM_ENABLED", "true")
    monkeypatch.setenv("SESSION_PREWARM_PREPARE_ENABLED", value)
    assert session_prewarm_prepare_enabled()


@pytest.mark.parametrize("value", ["", "false", "yes", "1", "on"])
def test_prepare_anything_else_is_off(monkeypatch, value):
    monkeypatch.setenv("SESSION_PREWARM_ENABLED", "true")
    monkeypatch.setenv("SESSION_PREWARM_PREPARE_ENABLED", value)
    assert not session_prewarm_prepare_enabled()


def test_prepare_unset_is_off(monkeypatch):
    monkeypatch.setenv("SESSION_PREWARM_ENABLED", "true")
    monkeypatch.delenv("SESSION_PREWARM_PREPARE_ENABLED", raising=False)
    assert not session_prewarm_prepare_enabled()


@pytest.mark.parametrize("prewarm", ["", "false"])
def test_prepare_is_off_when_session_prewarm_is_off(monkeypatch, prewarm):
    monkeypatch.setenv("SESSION_PREWARM_ENABLED", prewarm)
    monkeypatch.setenv("SESSION_PREWARM_PREPARE_ENABLED", "true")
    assert not session_prewarm_prepare_enabled()
