"""`history_count_prefetch_enabled`: default ON, `false` is the kill switch."""

import pytest

from apis.shared.feature_flags import history_count_prefetch_enabled


@pytest.mark.parametrize("value", [None, "", "true", "on", "nonsense"])
def test_everything_but_false_is_on(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("HISTORY_COUNT_PREFETCH_ENABLED", raising=False)
    else:
        monkeypatch.setenv("HISTORY_COUNT_PREFETCH_ENABLED", value)

    assert history_count_prefetch_enabled()


@pytest.mark.parametrize("value", ["false", "FALSE", " False "])
def test_false_is_off(monkeypatch, value):
    monkeypatch.setenv("HISTORY_COUNT_PREFETCH_ENABLED", value)
    assert not history_count_prefetch_enabled()
