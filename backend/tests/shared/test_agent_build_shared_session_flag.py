"""`agent_build_shared_session_enabled`: default ON, `false` is the kill switch."""

import pytest

from apis.shared.feature_flags import agent_build_shared_session_enabled


@pytest.mark.parametrize("value", [None, "", "true", "on", "ab", "shared_clients", "nonsense"])
def test_everything_but_false_is_on(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("AGENT_BUILD_SHARED_SESSION_ENABLED", raising=False)
    else:
        monkeypatch.setenv("AGENT_BUILD_SHARED_SESSION_ENABLED", value)

    assert agent_build_shared_session_enabled()


@pytest.mark.parametrize("value", ["false", "FALSE", " False "])
def test_false_is_off(monkeypatch, value):
    monkeypatch.setenv("AGENT_BUILD_SHARED_SESSION_ENABLED", value)
    assert not agent_build_shared_session_enabled()


def test_the_retired_experiment_variable_is_ignored(monkeypatch):
    """`AGENT_BUILD_EXPERIMENT` drove the A/B that decided this; a Runtime
    still carrying it must not read as anything."""
    monkeypatch.setenv("AGENT_BUILD_EXPERIMENT", "ab")
    monkeypatch.delenv("AGENT_BUILD_SHARED_SESSION_ENABLED", raising=False)
    assert agent_build_shared_session_enabled()
