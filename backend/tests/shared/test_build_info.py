"""Which build is running (docs/specs/release-ledger.md D1, D2)."""

import importlib

import pytest


def _reload(monkeypatch: pytest.MonkeyPatch, **env: str):
    for key in ("APP_VERSION", "APP_BUILD"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import apis.shared.build_info as module

    return importlib.reload(module)


def test_reads_the_baked_values_once(monkeypatch):
    module = _reload(monkeypatch, APP_VERSION="1.33.0", APP_BUILD="8f676bb2e1a4c0d2")
    assert module.build_stamp() == {"appVersion": "1.33.0", "build": "8f676bb2e1a4c0d2"}
    assert module.health_fields() == {"version": "1.33.0", "build": "8f676bb2e1a4c0d2"}


def test_unset_and_blank_both_read_unknown(monkeypatch):
    """A workflow variable can arrive as ""; a local run has neither."""
    assert _reload(monkeypatch).build_stamp() == {"appVersion": "unknown", "build": "unknown"}
    assert _reload(monkeypatch, APP_VERSION="  ", APP_BUILD="").build_stamp() == {
        "appVersion": "unknown",
        "build": "unknown",
    }


def test_stamp_is_a_fresh_dict_each_call(monkeypatch):
    """Callers splat it into a row; one caller's mutation must not reach the next row."""
    module = _reload(monkeypatch, APP_VERSION="1.33.0", APP_BUILD="abc")
    first = module.build_stamp()
    first["appVersion"] = "tampered"
    assert module.build_stamp()["appVersion"] == "1.33.0"
