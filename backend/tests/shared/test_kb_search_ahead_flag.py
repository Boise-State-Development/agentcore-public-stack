"""`kb_search_ahead_enabled`: default ON, `false` is the kill switch."""

import pytest

from apis.shared.feature_flags import kb_search_ahead_enabled


@pytest.mark.parametrize("value", [None, "", "true", "on", "nonsense"])
def test_everything_but_false_is_on(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("KB_SEARCH_AHEAD_ENABLED", raising=False)
    else:
        monkeypatch.setenv("KB_SEARCH_AHEAD_ENABLED", value)

    assert kb_search_ahead_enabled()


@pytest.mark.parametrize("value", ["false", "FALSE", " False "])
def test_false_is_off(monkeypatch, value):
    monkeypatch.setenv("KB_SEARCH_AHEAD_ENABLED", value)
    assert not kb_search_ahead_enabled()
