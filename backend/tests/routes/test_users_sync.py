"""Tests for the ``POST /users/me/sync`` endpoint.

Asserts the positive invariant that the persisted ``UserProfile.roles``
field is sourced from the validated JWT (i.e. ``current_user.roles``)
and never from the request body. Legacy clients that still send a
``roles`` field are accepted (no 4xx) but the field is dropped silently
and a warning is logged.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, PropertyMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.users.routes import router, get_user_repository
from tests.routes.conftest import mock_auth_user, mock_no_auth, mock_service


def _make_repo() -> AsyncMock:
    repo = AsyncMock()
    type(repo).enabled = PropertyMock(return_value=True)
    repo.upsert_user = AsyncMock()
    return repo


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.include_router(router)
    return a


# ---------------------------------------------------------------------------
# JWT roles are the source of truth
# ---------------------------------------------------------------------------


def test_sync_persists_jwt_roles_not_body_roles(app, make_user) -> None:
    """A client supplying ``roles: ["system_admin"]`` must not influence the
    persisted profile — JWT-derived roles win regardless of the body."""
    user = make_user(user_id="u-1", email="alice@example.com", name="Alice", roles=["default"])
    mock_auth_user(app, user)

    repo = _make_repo()
    mock_service(app, get_user_repository, repo)

    client = TestClient(app)
    resp = client.post(
        "/users/me/sync",
        json={
            "email": "alice@example.com",
            "name": "Alice",
            "roles": ["system_admin", "platform_admin"],  # malicious
        },
    )

    assert resp.status_code == 204
    repo.upsert_user.assert_called_once()
    persisted = repo.upsert_user.call_args.args[0]
    assert persisted.roles == ["default"]
    assert "system_admin" not in persisted.roles


def test_sync_with_no_jwt_roles_persists_empty_list(app, make_user) -> None:
    """If the JWT has no roles, the persisted profile gets an empty list —
    body ``roles`` cannot fill the gap."""
    user = make_user(user_id="u-2", email="bob@example.com", name="Bob", roles=[])
    mock_auth_user(app, user)

    repo = _make_repo()
    mock_service(app, get_user_repository, repo)

    client = TestClient(app)
    resp = client.post(
        "/users/me/sync",
        json={"email": "bob@example.com", "name": "Bob", "roles": ["system_admin"]},
    )

    assert resp.status_code == 204
    persisted = repo.upsert_user.call_args.args[0]
    assert persisted.roles == []


def test_sync_warns_when_legacy_roles_field_present(app, make_user, caplog) -> None:
    """The handler should log a WARNING when a request still carries the
    legacy ``roles`` key, so operators can chase down stale clients."""
    user = make_user(user_id="u-3", email="c@example.com", name="C", roles=["default"])
    mock_auth_user(app, user)
    repo = _make_repo()
    mock_service(app, get_user_repository, repo)

    client = TestClient(app)
    with caplog.at_level(logging.WARNING, logger="apis.app_api.users.routes"):
        resp = client.post(
            "/users/me/sync",
            json={"email": "c@example.com", "roles": ["system_admin"]},
        )

    assert resp.status_code == 204
    assert any("'roles' field" in rec.message and rec.levelno == logging.WARNING for rec in caplog.records)


def test_sync_without_legacy_roles_field_no_warning(app, make_user, caplog) -> None:
    """A clean request (no ``roles`` key at all) should not emit the warning."""
    user = make_user(user_id="u-4", email="d@example.com", name="D", roles=["default"])
    mock_auth_user(app, user)
    repo = _make_repo()
    mock_service(app, get_user_repository, repo)

    client = TestClient(app)
    with caplog.at_level(logging.WARNING, logger="apis.app_api.users.routes"):
        resp = client.post("/users/me/sync", json={"email": "d@example.com", "name": "D"})

    assert resp.status_code == 204
    assert not any("'roles' field" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Other identity fields still work
# ---------------------------------------------------------------------------


def test_sync_persists_email_name_picture_from_body(app, make_user) -> None:
    """Identity-display fields (email, name, picture) still come from the
    body — the security boundary is roles only."""
    user = make_user(user_id="u-5", email="e@example.com", name="E", roles=["default"])
    mock_auth_user(app, user)
    repo = _make_repo()
    mock_service(app, get_user_repository, repo)

    client = TestClient(app)
    resp = client.post(
        "/users/me/sync",
        json={
            "email": "e@example.com",
            "name": "Eddie",
            "picture": "https://cdn.example.com/e.png",
        },
    )

    assert resp.status_code == 204
    persisted = repo.upsert_user.call_args.args[0]
    assert persisted.email == "e@example.com"
    assert persisted.name == "Eddie"
    assert persisted.picture == "https://cdn.example.com/e.png"


def test_sync_email_normalized_to_lowercase(app, make_user) -> None:
    user = make_user(user_id="u-6", email="f@example.com", name="F", roles=["default"])
    mock_auth_user(app, user)
    repo = _make_repo()
    mock_service(app, get_user_repository, repo)

    client = TestClient(app)
    resp = client.post(
        "/users/me/sync",
        json={"email": "  F@Example.COM  ", "name": "F"},
    )

    assert resp.status_code == 204
    persisted = repo.upsert_user.call_args.args[0]
    assert persisted.email == "f@example.com"


def test_sync_missing_email_returns_422(app, make_user) -> None:
    user = make_user(user_id="u-7", email="g@example.com", name="G", roles=["default"])
    mock_auth_user(app, user)
    repo = _make_repo()
    mock_service(app, get_user_repository, repo)

    client = TestClient(app)
    resp = client.post("/users/me/sync", json={"name": "G"})

    # FastAPI returns 422 for missing required body fields.
    assert resp.status_code == 422


def test_sync_blank_email_returns_422(app, make_user) -> None:
    user = make_user(user_id="u-8", email="h@example.com", name="H", roles=["default"])
    mock_auth_user(app, user)
    repo = _make_repo()
    mock_service(app, get_user_repository, repo)

    client = TestClient(app)
    resp = client.post("/users/me/sync", json={"email": "   ", "name": "H"})

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Unauthenticated
# ---------------------------------------------------------------------------


def test_sync_requires_authentication(app) -> None:
    mock_no_auth(app)

    client = TestClient(app)
    resp = client.post(
        "/users/me/sync",
        json={"email": "x@example.com", "name": "X"},
    )

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Repo disabled (graceful no-op)
# ---------------------------------------------------------------------------


def test_sync_returns_204_when_repo_disabled(app, make_user) -> None:
    user = make_user(user_id="u-9", email="i@example.com", name="I", roles=["default"])
    mock_auth_user(app, user)

    repo = AsyncMock()
    type(repo).enabled = PropertyMock(return_value=False)
    repo.upsert_user = AsyncMock()
    mock_service(app, get_user_repository, repo)

    client = TestClient(app)
    resp = client.post("/users/me/sync", json={"email": "i@example.com", "name": "I"})

    assert resp.status_code == 204
    repo.upsert_user.assert_not_called()
