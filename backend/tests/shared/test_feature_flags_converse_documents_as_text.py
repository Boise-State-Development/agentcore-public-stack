"""``CONVERSE_DOCUMENTS_AS_TEXT_ENABLED`` is in development: only "true" enables it."""

import pytest

from apis.shared.feature_flags import converse_documents_as_text_enabled


def test_off_when_unset(monkeypatch):
    monkeypatch.delenv("CONVERSE_DOCUMENTS_AS_TEXT_ENABLED", raising=False)
    assert converse_documents_as_text_enabled() is False


@pytest.mark.parametrize("value", ["", "false", "1", "yes", "on", "enabled"])
def test_only_true_enables_it(monkeypatch, value):
    monkeypatch.setenv("CONVERSE_DOCUMENTS_AS_TEXT_ENABLED", value)
    assert converse_documents_as_text_enabled() is False


@pytest.mark.parametrize("value", ["true", "True", " TRUE "])
def test_true_enables_it(monkeypatch, value):
    monkeypatch.setenv("CONVERSE_DOCUMENTS_AS_TEXT_ENABLED", value)
    assert converse_documents_as_text_enabled() is True
