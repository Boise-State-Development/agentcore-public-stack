"""Tests for the ``POST /users/me/sync`` endpoint.

Asserts the positive invariant that the persisted ``UserProfile.roles``
and ``UserProfile.email`` fields are sourced from the validated JWT
(i.e. ``current_user.roles`` / ``current_user.email``) and never from
the request body. Legacy clients that still send those fields are
accepted (no 4xx) but the values are dropped silently and a warning
is logged.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, PropertyMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.users.routes import get_user_repository, router
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
# Roles: JWT is the source of truth
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
            "name": "Alice",
            "roles": ["system_admin", "platform_admin"],
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
        json={"name": "Bob", "roles": ["system_admin"]},
    )

    assert resp.status_code == 204
    persisted = repo.upsert_user.call_args.args[0]
    assert persisted.roles == []


# ---------------------------------------------------------------------------
# Email: JWT is the source of truth
# ---------------------------------------------------------------------------


def test_sync_persists_jwt_email_not_body_email(app, make_user) -> None:
    """A client supplying ``email`` must not influence the persisted email —
    the JWT-derived email wins regardless of the body."""
    user = make_user(
        user_id="u-e1",
        email="real@example.com",
        name="Real",
        roles=["default"],
    )
    mock_auth_user(app, user)
    repo = _make_repo()
    mock_service(app, get_user_repository, repo)

    client = TestClient(app)
    resp = client.post(
        "/users/me/sync",
        json={
            "name": "Real",
            "email": "victim@somewhere-else.com",
        },
    )

    assert resp.status_code == 204
    persisted = repo.upsert_user.call_args.args[0]
    assert persisted.email == "real@example.com"


def test_sync_email_domain_derived_from_jwt(app, make_user) -> None:
    user = make_user(
        user_id="u-e2",
        email="user@university.edu",
        name="U",
        roles=["default"],
    )
    mock_auth_user(app, user)
    repo = _make_repo()
    mock_service(app, get_user_repository, repo)

    client = TestClient(app)
    resp = client.post(
        "/users/me/sync",
        json={"name": "U", "email": "spoofed@elsewhere.com"},
    )

    assert resp.status_code == 204
    persisted = repo.upsert_user.call_args.args[0]
    assert persisted.email == "user@university.edu"
    assert persisted.email_domain == "university.edu"


def test_sync_jwt_email_normalized_to_lowercase(app, make_user) -> None:
    user = make_user(
        user_id="u-e3",
        email="MixedCase@Example.COM",
        name="M",
        roles=["default"],
    )
    mock_auth_user(app, user)
    repo = _make_repo()
    mock_service(app, get_user_repository, repo)

    client = TestClient(app)
    resp = client.post("/users/me/sync", json={"name": "M"})

    assert resp.status_code == 204
    persisted = repo.upsert_user.call_args.args[0]
    assert persisted.email == "mixedcase@example.com"


def test_sync_missing_jwt_email_returns_422(app, make_user) -> None:
    """A session whose JWT has no email cannot produce a valid persisted
    profile — better to refuse than to write an empty row."""
    user = make_user(user_id="u-e4", email="", name="X", roles=["default"])
    mock_auth_user(app, user)
    repo = _make_repo()
    mock_service(app, get_user_repository, repo)

    client = TestClient(app)
    resp = client.post("/users/me/sync", json={"name": "X"})

    assert resp.status_code == 422
    repo.upsert_user.assert_not_called()


# ---------------------------------------------------------------------------
# Legacy fields warn but don't break
# ---------------------------------------------------------------------------


def test_sync_warns_when_legacy_fields_present(app, make_user, caplog) -> None:
    """The handler should log a WARNING when a request still carries the
    legacy ``roles`` or ``email`` keys, so operators can chase down stale
    clients."""
    user = make_user(user_id="u-3", email="c@example.com", name="C", roles=["default"])
    mock_auth_user(app, user)
    repo = _make_repo()
    mock_service(app, get_user_repository, repo)

    client = TestClient(app)
    with caplog.at_level(logging.WARNING, logger="apis.app_api.users.routes"):
        resp = client.post(
            "/users/me/sync",
            json={"name": "C", "email": "x@y.com", "roles": ["system_admin"]},
        )

    assert resp.status_code == 204
    assert any("legacy fields" in rec.message and rec.levelno == logging.WARNING for rec in caplog.records)


def test_sync_without_legacy_fields_no_warning(app, make_user, caplog) -> None:
    """A clean request (no ``roles`` / ``email`` keys) should not emit the
    warning."""
    user = make_user(user_id="u-4", email="d@example.com", name="D", roles=["default"])
    mock_auth_user(app, user)
    repo = _make_repo()
    mock_service(app, get_user_repository, repo)

    client = TestClient(app)
    with caplog.at_level(logging.WARNING, logger="apis.app_api.users.routes"):
        resp = client.post("/users/me/sync", json={"name": "D"})

    assert resp.status_code == 204
    assert not any("legacy fields" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Other identity fields still work
# ---------------------------------------------------------------------------


def test_sync_persists_name_picture_from_body(app, make_user) -> None:
    """Display fields (name, picture) still come from the body — the
    security boundary is roles/email only."""
    user = make_user(user_id="u-5", email="e@example.com", name="E", roles=["default"])
    mock_auth_user(app, user)
    repo = _make_repo()
    mock_service(app, get_user_repository, repo)

    client = TestClient(app)
    resp = client.post(
        "/users/me/sync",
        json={
            "name": "Eddie",
            "picture": "https://cdn.example.com/e.png",
        },
    )

    assert resp.status_code == 204
    persisted = repo.upsert_user.call_args.args[0]
    assert persisted.name == "Eddie"
    assert persisted.picture == "https://cdn.example.com/e.png"
    # Email still comes from the JWT.
    assert persisted.email == "e@example.com"


def test_sync_falls_back_to_jwt_name_when_body_name_blank(app, make_user) -> None:
    user = make_user(
        user_id="u-6",
        email="f@example.com",
        name="JWT Name",
        roles=["default"],
    )
    mock_auth_user(app, user)
    repo = _make_repo()
    mock_service(app, get_user_repository, repo)

    client = TestClient(app)
    resp = client.post("/users/me/sync", json={"name": ""})

    assert resp.status_code == 204
    persisted = repo.upsert_user.call_args.args[0]
    assert persisted.name == "JWT Name"


# ---------------------------------------------------------------------------
# Auth + repository disabled
# ---------------------------------------------------------------------------


def test_sync_requires_authentication(app) -> None:
    mock_no_auth(app)

    client = TestClient(app)
    resp = client.post("/users/me/sync", json={"name": "X"})

    assert resp.status_code == 401


def test_sync_returns_204_when_repo_disabled(app, make_user) -> None:
    user = make_user(user_id="u-9", email="i@example.com", name="I", roles=["default"])
    mock_auth_user(app, user)

    repo = AsyncMock()
    type(repo).enabled = PropertyMock(return_value=False)
    repo.upsert_user = AsyncMock()
    mock_service(app, get_user_repository, repo)

    client = TestClient(app)
    resp = client.post("/users/me/sync", json={"name": "I"})

    assert resp.status_code == 204
    repo.upsert_user.assert_not_called()
