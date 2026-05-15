"""Tests for BFFConfig env-driven loader and is_enabled gating."""

from __future__ import annotations

import pytest

from apis.shared.sessions_bff.config import BFFConfig


@pytest.fixture(autouse=True)
def _clear_bff_env(monkeypatch):
    for key in (
        "BFF_SESSIONS_TABLE_NAME",
        "BFF_COOKIE_SIGNING_KEY_ARN",
        "BFF_SESSION_TTL_SECONDS",
        "BFF_SESSION_REFRESH_LEEWAY_SECONDS",
        "COGNITO_BFF_APP_CLIENT_ID",
        "COGNITO_BFF_APP_CLIENT_SECRET_ARN",
        "INFERENCE_API_URL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_disabled_when_env_unset() -> None:
    cfg = BFFConfig.from_env()
    assert cfg.is_enabled() is False
    assert cfg.session_ttl_seconds == 28800
    assert cfg.refresh_leeway_seconds == 60


def test_enabled_requires_table_and_kms(monkeypatch) -> None:
    monkeypatch.setenv("BFF_SESSIONS_TABLE_NAME", "tbl")
    cfg_no_kms = BFFConfig.from_env()
    assert cfg_no_kms.is_enabled() is False

    monkeypatch.setenv("BFF_COOKIE_SIGNING_KEY_ARN", "arn:aws:kms:...")
    cfg_both = BFFConfig.from_env()
    assert cfg_both.is_enabled() is True


def test_overrides_ttl_and_leeway(monkeypatch) -> None:
    monkeypatch.setenv("BFF_SESSION_TTL_SECONDS", "1234")
    monkeypatch.setenv("BFF_SESSION_REFRESH_LEEWAY_SECONDS", "7")
    cfg = BFFConfig.from_env()
    assert cfg.session_ttl_seconds == 1234
    assert cfg.refresh_leeway_seconds == 7
