"""Watch triage is opt-in while in development, and needs the quiet watcher (triage spec §8)."""

import pytest

from apis.shared.feature_flags import watch_triage_enabled


@pytest.mark.parametrize(
    "proactive,triage,expected",
    [
        ("true", None, False),
        ("true", "", False),
        ("true", "false", False),
        ("true", "yes", False),
        ("true", "1", False),
        ("true", "true", True),
        ("true", " TRUE ", True),
        (None, "true", False),
        ("", "true", False),
        ("false", "true", False),
    ],
)
def test_on_only_when_both_are_explicitly_enabled(monkeypatch, proactive, triage, expected):
    for name, value in (("PROACTIVE_RUNS_ENABLED", proactive), ("WATCH_TRIAGE_ENABLED", triage)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert watch_triage_enabled() is expected
