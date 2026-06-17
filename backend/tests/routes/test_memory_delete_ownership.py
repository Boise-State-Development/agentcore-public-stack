"""Tests for ``DELETE /memory/{record_id}`` ownership enforcement.

The memory store is keyed at the AWS API level by record id alone — the
namespace (which encodes the owning user id) is metadata on the record,
not part of the lookup. The route therefore has to assert ownership
before calling ``batch_delete_memory_records``; otherwise any user
holding any record id can delete any other user's record.

These tests pin that contract:

* The owner of a record can delete it (the underlying delete service is
  invoked exactly once, with the right id).
* A non-owner asking to delete the same record sees 404 (matching GET's
  behaviour for resources that aren't theirs) and the underlying delete
  service is *never* invoked.
* A record that doesn't exist at all also yields 404 with no delete.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.memory.routes import router
from tests.routes.conftest import mock_auth_user


ROUTES_MODULE = "apis.app_api.memory.routes"


@pytest.fixture
def app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(router)
    return _app


def _record_owned_by(user_id: str, *, record_id: str = "mem-abc") -> dict:
    """Shape that mirrors GetMemoryRecord's response for a record whose
    owning user id is *user_id*."""
    return {
        "memoryRecordId": record_id,
        "namespaces": [f"/strategies/STRAT/actors/{user_id}"],
        "content": {"text": "owned content"},
    }


# ---------------------------------------------------------------------------
# Owner can delete
# ---------------------------------------------------------------------------


def test_owner_can_delete_their_record(app, make_user) -> None:
    user = make_user(user_id="user-owner")
    mock_auth_user(app, user)

    fetch = AsyncMock(return_value=_record_owned_by("user-owner"))
    delete = AsyncMock(return_value=True)

    with patch(f"{ROUTES_MODULE}.is_memory_available", return_value=True), patch(
        f"{ROUTES_MODULE}.get_memory_record_owner", fetch
    ), patch(f"{ROUTES_MODULE}.delete_memory", delete):
        resp = TestClient(app).delete("/memory/mem-abc")

    assert resp.status_code == 200
    fetch.assert_awaited_once_with("mem-abc")
    delete.assert_awaited_once()


# ---------------------------------------------------------------------------
# Non-owner cannot delete and triggers no delete call
# ---------------------------------------------------------------------------


def test_non_owner_delete_returns_404_and_skips_delete(app, make_user) -> None:
    user = make_user(user_id="user-attacker")
    mock_auth_user(app, user)

    fetch = AsyncMock(return_value=_record_owned_by("user-victim"))
    delete = AsyncMock(return_value=True)

    with patch(f"{ROUTES_MODULE}.is_memory_available", return_value=True), patch(
        f"{ROUTES_MODULE}.get_memory_record_owner", fetch
    ), patch(f"{ROUTES_MODULE}.delete_memory", delete):
        resp = TestClient(app).delete("/memory/mem-victim")

    assert resp.status_code == 404
    delete.assert_not_called()


def test_record_with_no_namespaces_treated_as_not_yours(app, make_user) -> None:
    """Defensive: a record without any namespace is not attributable to
    a user, so a non-owner can't proceed."""
    user = make_user(user_id="user-attacker")
    mock_auth_user(app, user)

    record_no_ns = {"memoryRecordId": "mem-x", "namespaces": []}
    fetch = AsyncMock(return_value=record_no_ns)
    delete = AsyncMock(return_value=True)

    with patch(f"{ROUTES_MODULE}.is_memory_available", return_value=True), patch(
        f"{ROUTES_MODULE}.get_memory_record_owner", fetch
    ), patch(f"{ROUTES_MODULE}.delete_memory", delete):
        resp = TestClient(app).delete("/memory/mem-x")

    assert resp.status_code == 404
    delete.assert_not_called()


# ---------------------------------------------------------------------------
# Missing record → 404 and no delete
# ---------------------------------------------------------------------------


def test_missing_record_returns_404_without_delete(app, make_user) -> None:
    user = make_user(user_id="user-anyone")
    mock_auth_user(app, user)

    fetch = AsyncMock(return_value=None)
    delete = AsyncMock(return_value=True)

    with patch(f"{ROUTES_MODULE}.is_memory_available", return_value=True), patch(
        f"{ROUTES_MODULE}.get_memory_record_owner", fetch
    ), patch(f"{ROUTES_MODULE}.delete_memory", delete):
        resp = TestClient(app).delete("/memory/mem-nonexistent")

    assert resp.status_code == 404
    delete.assert_not_called()
