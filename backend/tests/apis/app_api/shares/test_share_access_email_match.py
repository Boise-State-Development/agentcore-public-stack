"""Tests for the share-access invariant: ``_check_access`` evaluates the
``allowed_emails`` list against ``requester.email`` only.

The User object handed to the service is the one produced by
``get_current_user_from_session``, whose ``email`` field is hydrated
from the validated session (Cognito-issued ID-token claims persisted to
the Users table by the BFF callback). The persisted profile cannot be
spoofed via the profile-sync endpoint (see test_users_sync.py), so any
email landing in ``requester.email`` here is authoritative. These tests
just lock the comparison itself in place — same-string compare,
case-insensitive, with the ``allowed_emails`` list and the
``access_level`` semantics as the only inputs.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apis.app_api.shares.service import AccessDeniedError, ShareService
from apis.shared.auth.models import User


def _service() -> ShareService:
    """Build a ShareService without touching DynamoDB.

    We never call methods that hit the table — only ``_check_access``,
    which reads the share item dict directly.
    """
    svc = ShareService.__new__(ShareService)
    svc._enabled = True
    svc._table = MagicMock()
    return svc


def _user(email: str, user_id: str = "u-1") -> User:
    return User(user_id=user_id, email=email, name="N", roles=[])


def _share(
    *,
    owner_id: str = "owner-1",
    access_level: str = "specific",
    allowed_emails: list[str] | None = None,
) -> dict:
    item: dict = {
        "share_id": "s-1",
        "owner_id": owner_id,
        "access_level": access_level,
    }
    if allowed_emails is not None:
        item["allowed_emails"] = allowed_emails
    return item


# ---------------------------------------------------------------------------
# Allowed-email match
# ---------------------------------------------------------------------------


def test_specific_access_allows_listed_email() -> None:
    svc = _service()
    item = _share(allowed_emails=["alice@example.com"])
    svc._check_access(item, _user("alice@example.com"))


def test_specific_access_match_is_case_insensitive() -> None:
    svc = _service()
    item = _share(allowed_emails=["alice@example.com"])
    svc._check_access(item, _user("ALICE@Example.COM"))


def test_specific_access_denies_unlisted_email() -> None:
    svc = _service()
    item = _share(allowed_emails=["alice@example.com"])
    with pytest.raises(AccessDeniedError):
        svc._check_access(item, _user("eve@example.com"))


def test_specific_access_with_empty_allowlist_denies_everyone() -> None:
    svc = _service()
    item = _share(allowed_emails=[])
    with pytest.raises(AccessDeniedError):
        svc._check_access(item, _user("alice@example.com"))


def test_specific_access_with_no_allowlist_field_denies() -> None:
    svc = _service()
    item = _share()  # no allowed_emails key at all
    with pytest.raises(AccessDeniedError):
        svc._check_access(item, _user("alice@example.com"))


# ---------------------------------------------------------------------------
# Owner override
# ---------------------------------------------------------------------------


def test_owner_always_has_access_regardless_of_allowlist() -> None:
    svc = _service()
    item = _share(owner_id="owner-1", allowed_emails=["someone-else@example.com"])
    user = User(user_id="owner-1", email="any@whatever.com", name="N", roles=[])
    svc._check_access(item, user)


# ---------------------------------------------------------------------------
# Public access
# ---------------------------------------------------------------------------


def test_public_access_allows_everyone() -> None:
    svc = _service()
    item = _share(access_level="public", allowed_emails=None)
    svc._check_access(item, _user("randomstranger@example.com"))
