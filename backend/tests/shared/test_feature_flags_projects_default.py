"""Shared Projects is default on with a kill switch (CLAUDE.md "Feature flags")."""

import pytest

from apis.shared.feature_flags import projects_enabled


@pytest.mark.parametrize(
    "value,expected",
    [(None, True), ("", True), ("true", True), ("yes", True), ("false", False), (" FALSE ", False)],
)
def test_projects_are_off_only_when_explicitly_disabled(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("PROJECTS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("PROJECTS_ENABLED", value)
    assert projects_enabled() is expected
