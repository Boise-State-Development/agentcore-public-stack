"""The conversation index write path is opt-in while in development (CLAUDE.md "Feature flags")."""

import pytest

from apis.shared.feature_flags import conversation_index_enabled


@pytest.mark.parametrize(
    "value,expected",
    [(None, False), ("", False), ("false", False), ("yes", False), ("true", True), (" TRUE ", True)],
)
def test_conversation_index_is_on_only_when_explicitly_enabled(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("CONVERSATION_INDEX_ENABLED", raising=False)
    else:
        monkeypatch.setenv("CONVERSATION_INDEX_ENABLED", value)
    assert conversation_index_enabled() is expected
