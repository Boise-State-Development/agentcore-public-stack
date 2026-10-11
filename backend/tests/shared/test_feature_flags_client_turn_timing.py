"""`CLIENT_TURN_TIMING_ENABLED` is opt-in while in development (CLAUDE.md "Feature flags")."""

import pytest

from apis.shared.feature_flags import client_turn_timing_enabled


@pytest.mark.parametrize("value", ["true", "TRUE", " True "])
def test_only_true_enables(monkeypatch, value):
    monkeypatch.setenv("CLIENT_TURN_TIMING_ENABLED", value)
    assert client_turn_timing_enabled()


@pytest.mark.parametrize("value", ["", "false", "yes", "1"])
def test_anything_else_is_off(monkeypatch, value):
    monkeypatch.setenv("CLIENT_TURN_TIMING_ENABLED", value)
    assert not client_turn_timing_enabled()
