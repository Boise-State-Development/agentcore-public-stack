"""Shared Projects is opt-in while in development (CLAUDE.md "Feature flags")."""

import pytest

from apis.shared.feature_flags import projects_enabled


@pytest.mark.parametrize(
    "value,expected",
    [(None, False), ("", False), ("false", False), ("yes", False), ("true", True), (" TRUE ", True)],
)
def test_projects_are_on_only_when_explicitly_enabled(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("PROJECTS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("PROJECTS_ENABLED", value)
    assert projects_enabled() is expected
