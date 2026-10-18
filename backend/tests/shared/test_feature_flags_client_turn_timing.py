"""`CLIENT_TURN_TIMING_ENABLED` is default on with a kill switch (CLAUDE.md "Feature flags")."""

import pytest

from apis.shared.feature_flags import client_turn_timing_enabled


@pytest.mark.parametrize("value", ["false", "FALSE", " False "])
def test_only_false_disables(monkeypatch, value):
    monkeypatch.setenv("CLIENT_TURN_TIMING_ENABLED", value)
    assert not client_turn_timing_enabled()


@pytest.mark.parametrize("value", ["", "true", "yes", "1"])
def test_anything_else_is_on(monkeypatch, value):
    monkeypatch.setenv("CLIENT_TURN_TIMING_ENABLED", value)
    assert client_turn_timing_enabled()


def test_unset_is_on(monkeypatch):
    monkeypatch.delenv("CLIENT_TURN_TIMING_ENABLED", raising=False)
    assert client_turn_timing_enabled()
