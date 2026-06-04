"""Unit tests for ``apis.shared.security.ownership``.

Verifies the helpers raise :class:`OwnershipError` for non-owners (mapped
to HTTP 404 by the registered handler) and accept owner-matched records.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.shared.security.ownership import (
    OwnershipError,
    register_ownership_handler,
    require_file_owner,
    require_memory_owner,
    require_session_owner,
)

# ---- require_session_owner ------------------------------------------------


def test_session_owner_accepts_match_via_dict() -> None:
    record = {"sessionId": "s1", "userId": "u1"}
    require_session_owner("u1", record)


def test_session_owner_accepts_match_via_attr() -> None:
    class Rec:
        user_id = "u1"

    require_session_owner("u1", Rec())


def test_session_owner_rejects_mismatch() -> None:
    record = {"userId": "u2"}
    with pytest.raises(OwnershipError):
        require_session_owner("u1", record)


def test_session_owner_rejects_none_record() -> None:
    with pytest.raises(OwnershipError):
        require_session_owner("u1", None)


def test_session_owner_rejects_record_without_owner_field() -> None:
    with pytest.raises(OwnershipError):
        require_session_owner("u1", {"sessionId": "s1"})


def test_session_owner_rejects_empty_user_id() -> None:
    with pytest.raises(OwnershipError):
        require_session_owner("", {"userId": "u1"})


# ---- require_memory_owner -------------------------------------------------


def test_memory_owner_namespace_match() -> None:
    require_memory_owner("u1", "/preferences/u1")


def test_memory_owner_namespace_mismatch() -> None:
    with pytest.raises(OwnershipError):
        require_memory_owner("u1", "/preferences/u2")


def test_memory_owner_record_object_match() -> None:
    require_memory_owner("u1", {"recordId": "mem-x", "owner_id": "u1"})


def test_memory_owner_record_object_mismatch() -> None:
    with pytest.raises(OwnershipError):
        require_memory_owner("u1", {"recordId": "mem-x", "owner_id": "u2"})


# ---- require_file_owner ---------------------------------------------------


def test_file_owner_match() -> None:
    require_file_owner("u1", {"upload_id": "f1", "user_id": "u1"})


def test_file_owner_mismatch() -> None:
    with pytest.raises(OwnershipError):
        require_file_owner("u1", {"upload_id": "f1", "user_id": "u2"})


# ---- FastAPI handler mapping ----------------------------------------------


def _make_app() -> FastAPI:
    app = FastAPI()
    register_ownership_handler(app)

    @app.get("/test")
    async def _route(kind: str = "session") -> dict:
        raise OwnershipError(kind)

    return app


def test_ownership_error_maps_to_404() -> None:
    client = TestClient(_make_app())
    resp = client.get("/test")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Session not found."}


def test_ownership_error_does_not_disclose_resource_existence() -> None:
    """Response must read like a generic not-found, not a 403/forbidden."""
    client = TestClient(_make_app())
    resp = client.get("/test", params={"kind": "memory record"})
    assert resp.status_code == 404
    body = resp.json()
    assert "forbidden" not in body["detail"].lower()
    assert "denied" not in body["detail"].lower()
    assert "permission" not in body["detail"].lower()
