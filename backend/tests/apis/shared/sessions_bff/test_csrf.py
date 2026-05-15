"""Tests for CSRF token derivation and validation."""

from __future__ import annotations

from apis.shared.sessions_bff.csrf import CSRFHelper, derive_token, tokens_match


def test_derive_token_is_deterministic() -> None:
    secret = "csrf-secret"
    sid = "sess-001"
    assert derive_token(secret, sid) == derive_token(secret, sid)


def test_derive_token_changes_with_session_id() -> None:
    secret = "csrf-secret"
    assert derive_token(secret, "sess-A") != derive_token(secret, "sess-B")


def test_derive_token_changes_with_secret() -> None:
    sid = "sess-001"
    assert derive_token("secret-A", sid) != derive_token("secret-B", sid)


def test_derive_token_truncated_to_32_chars() -> None:
    assert len(derive_token("secret", "sid")) == 32


def test_tokens_match_constant_time_safe_paths() -> None:
    assert tokens_match("abc", "abc") is True
    assert tokens_match("abc", "abd") is False
    assert tokens_match("", "abc") is False
    assert tokens_match("abc", "") is False


def test_validate_happy_path() -> None:
    secret = "csrf-secret"
    sid = "sess-001"
    token = CSRFHelper.derive_token(secret, sid)
    assert CSRFHelper.validate(secret, sid, token, token) is True


def test_validate_rejects_header_cookie_mismatch() -> None:
    secret = "csrf-secret"
    sid = "sess-001"
    token = CSRFHelper.derive_token(secret, sid)
    assert CSRFHelper.validate(secret, sid, token, "different") is False


def test_validate_rejects_header_with_wrong_session_secret() -> None:
    """Even if header == cookie, an attacker-supplied token must not validate."""
    forged = "0" * 32
    assert CSRFHelper.validate("real-secret", "sess-001", forged, forged) is False


def test_validate_rejects_empty() -> None:
    assert CSRFHelper.validate("s", "sid", "", "") is False
