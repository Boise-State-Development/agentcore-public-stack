"""Tests for `POST /auth/logout`."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from apis.shared.sessions_bff.cache import get_default_cache
from apis.shared.sessions_bff.config import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
)
from apis.shared.sessions_bff.models import CookiePayload, SessionRecord


def _seed_record(repository, session_id: str = "sess-x") -> SessionRecord:
    now = int(time.time())
    record = SessionRecord(
        session_id=session_id,
        user_id="user-sub",
        username="alice",
        cognito_access_token="a.t",
        cognito_refresh_token="r.t",
        id_token="i.t",
        access_token_exp=now + 3600,
        csrf_secret="csrf-secret",
        created_at=now,
        last_seen_at=now,
        ttl=now + 28800,
    )

    # Use the sync table directly to avoid having to await in fixture code.
    repository._table.put_item(Item=repository._record_to_item(record))
    return record


def test_logout_with_valid_cookie_deletes_row_and_clears_cookies(
    app, codec, repository
):
    record = _seed_record(repository)
    sealed = codec.seal(CookiePayload(session_id=record.session_id))

    client = TestClient(app)
    response = client.post(
        "/auth/logout", cookies={SESSION_COOKIE_NAME: sealed}
    )

    assert response.status_code == 204
    set_cookie_blob = " ".join(response.headers.get_list("set-cookie"))
    assert SESSION_COOKIE_NAME in set_cookie_blob
    assert CSRF_COOKIE_NAME in set_cookie_blob

    # Row should be gone.
    response_item = repository._table.get_item(
        Key={"PK": f"SESSION#{record.session_id}", "SK": "META"}
    )
    assert "Item" not in response_item


def test_logout_without_cookie_clears_cookies_204(app):
    response = TestClient(app).post("/auth/logout")
    assert response.status_code == 204
    set_cookie_blob = " ".join(response.headers.get_list("set-cookie"))
    assert SESSION_COOKIE_NAME in set_cookie_blob
    assert CSRF_COOKIE_NAME in set_cookie_blob


def test_logout_with_unsealable_cookie_still_clears_and_returns_204(app, repository):
    """A garbage cookie should not crash logout — clear and exit cleanly."""
    response = TestClient(app).post(
        "/auth/logout", cookies={SESSION_COOKIE_NAME: "this-is-not-a-sealed-cookie"}
    )
    assert response.status_code == 204
    set_cookie_blob = " ".join(response.headers.get_list("set-cookie"))
    assert SESSION_COOKIE_NAME in set_cookie_blob


def test_logout_invalidates_local_cache(app, codec, repository):
    record = _seed_record(repository, session_id="sess-cached")
    cache = get_default_cache()
    cache.set(record)
    assert cache.get(record.session_id) is not None

    sealed = codec.seal(CookiePayload(session_id=record.session_id))
    response = TestClient(app).post(
        "/auth/logout", cookies={SESSION_COOKIE_NAME: sealed}
    )
    assert response.status_code == 204
    assert cache.get(record.session_id) is None


def test_logout_is_idempotent(app, codec, repository):
    """Logging out twice in a row must not error out on the second call."""
    record = _seed_record(repository, session_id="sess-twice")
    sealed = codec.seal(CookiePayload(session_id=record.session_id))

    client = TestClient(app)
    r1 = client.post("/auth/logout", cookies={SESSION_COOKIE_NAME: sealed})
    r2 = client.post("/auth/logout", cookies={SESSION_COOKIE_NAME: sealed})

    assert r1.status_code == 204
    assert r2.status_code == 204
