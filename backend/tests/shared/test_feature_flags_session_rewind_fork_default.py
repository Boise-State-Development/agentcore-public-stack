"""Conversation rewind and fork are default on with a kill switch (CLAUDE.md "Feature flags")."""

import pytest

from apis.shared.feature_flags import session_fork_enabled, session_rewind_enabled


@pytest.mark.parametrize("variable,reader", [
    ("SESSION_REWIND_ENABLED", session_rewind_enabled),
    ("SESSION_FORK_ENABLED", session_fork_enabled),
])
@pytest.mark.parametrize(
    "value,expected",
    [(None, True), ("", True), ("true", True), ("yes", True), ("false", False), (" FALSE ", False)],
)
def test_off_only_when_explicitly_disabled(monkeypatch, variable, reader, value, expected):
    if value is None:
        monkeypatch.delenv(variable, raising=False)
    else:
        monkeypatch.setenv(variable, value)
    assert reader() is expected
