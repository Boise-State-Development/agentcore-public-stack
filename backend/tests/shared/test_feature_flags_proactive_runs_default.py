"""The quiet watcher is opt-in while in development (CLAUDE.md "Feature flags")."""

import pytest

from apis.shared.feature_flags import proactive_runs_enabled


@pytest.mark.parametrize(
    "value,expected",
    [(None, False), ("", False), ("false", False), ("yes", False), ("1", False), ("true", True), (" TRUE ", True)],
)
def test_proactive_runs_is_on_only_when_explicitly_enabled(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("PROACTIVE_RUNS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", value)
    assert proactive_runs_enabled() is expected
