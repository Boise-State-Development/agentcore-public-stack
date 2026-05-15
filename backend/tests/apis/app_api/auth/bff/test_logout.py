"""Tests for `POST /auth/logout`."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from apis.shared.sessions_bff.cache import get_default_cache
from apis.shared.sessions_bff.config import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
)
from apis.shared.sessions_bff.models import CookiePayload, SessionRecord

from .conftest import BFF_CLIENT_ID, COGNITO_DOMAIN_URL, POST_LOGIN_URL


def _assert_post_logout_url(response_body: dict) -> None:
    """The Cognito Hosted UI logout URL must point at our domain with the
    BFF client_id and a logout_uri matching what CDK registers (no trailing
    slash). Cognito rejects mismatched URIs at runtime, so we pin it here."""
    url = response_body.get("post_logout_url")
    assert url, f"expected post_logout_url, got {response_body!r}"
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        f"{COGNITO_DOMAIN_URL}/logout"
    )
    qs = parse_qs(parsed.query)
    assert qs["client_id"] == [BFF_CLIENT_ID]
    assert qs["logout_uri"] == [POST_LOGIN_URL.rstrip("/")]


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

    assert response.status_code == 200
    _assert_post_logout_url(response.json())
    set_cookie_blob = " ".join(response.headers.get_list("set-cookie"))
    assert SESSION_COOKIE_NAME in set_cookie_blob
    assert CSRF_COOKIE_NAME in set_cookie_blob

    # Row should be gone.
    response_item = repository._table.get_item(
        Key={"PK": f"SESSION#{record.session_id}", "SK": "META"}
    )
    assert "Item" not in response_item


def test_logout_without_cookie_clears_cookies(app):
    response = TestClient(app).post("/auth/logout")
    assert response.status_code == 200
    _assert_post_logout_url(response.json())
    set_cookie_blob = " ".join(response.headers.get_list("set-cookie"))
    assert SESSION_COOKIE_NAME in set_cookie_blob
    assert CSRF_COOKIE_NAME in set_cookie_blob


def test_logout_with_unsealable_cookie_still_clears_and_returns_post_logout_url(
    app, repository
):
    """A garbage cookie should not crash logout — clear and exit cleanly."""
    response = TestClient(app).post(
        "/auth/logout", cookies={SESSION_COOKIE_NAME: "this-is-not-a-sealed-cookie"}
    )
    assert response.status_code == 200
    _assert_post_logout_url(response.json())
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
    assert response.status_code == 200
    _assert_post_logout_url(response.json())
    assert cache.get(record.session_id) is None


def test_logout_is_idempotent(app, codec, repository):
    """Logging out twice in a row must not error out on the second call."""
    record = _seed_record(repository, session_id="sess-twice")
    sealed = codec.seal(CookiePayload(session_id=record.session_id))

    client = TestClient(app)
    r1 = client.post("/auth/logout", cookies={SESSION_COOKIE_NAME: sealed})
    r2 = client.post("/auth/logout", cookies={SESSION_COOKIE_NAME: sealed})

    assert r1.status_code == 200
    assert r2.status_code == 200
    _assert_post_logout_url(r1.json())
    _assert_post_logout_url(r2.json())


def test_logout_returns_null_post_logout_url_when_cognito_unconfigured(
    app, monkeypatch
):
    """If COGNITO_DOMAIN_URL isn't set, the Cognito hop is impossible —
    fall back to a null URL and let the SPA navigate locally."""
    monkeypatch.delenv("COGNITO_DOMAIN_URL", raising=False)
    response = TestClient(app).post("/auth/logout")
    assert response.status_code == 200
    assert response.json() == {"post_logout_url": None}
