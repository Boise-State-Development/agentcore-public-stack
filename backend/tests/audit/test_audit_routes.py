"""The audit read API.

Two properties matter more than the payload shapes:

  * every route is **system_admin-only** — the trail records what admins do,
    including refused escalation attempts, so the people it records must not be
    able to be granted the ability to read it; and
  * the API is **read-only** — records age out via the table's TTL and by no
    other means, so a delete or edit route appearing here is a bug.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apis.app_api.admin.audit.routes import router as audit_router
from apis.shared.audit.models import TARGET_APP_ROLE, AuditAction, AuditRecord
from apis.shared.auth import require_admin


def _record(**kw) -> AuditRecord:
    defaults = dict(
        action=AuditAction.ROLE_UPDATED,
        actor_user_id="admin-1",
        actor_email="admin@example.com",
        target_type=TARGET_APP_ROLE,
        target_id="analyst",
    )
    defaults.update(kw)
    return AuditRecord(**defaults)


def _client(repository: MagicMock, *, allow: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(audit_router, prefix="/admin")

    def _admin():
        if not allow:
            raise HTTPException(status_code=403, detail="Access denied.")
        return MagicMock(user_id="admin-1", email="admin@example.com")

    app.dependency_overrides[require_admin] = _admin

    patcher = patch(
        "apis.app_api.admin.audit.routes.get_audit_repository",
        return_value=repository,
    )
    patcher.start()
    client = TestClient(app)
    client._patcher = patcher  # type: ignore[attr-defined]
    return client


@pytest.fixture
def repository():
    repo = MagicMock()
    repo.list_recent.return_value = ([_record()], None)
    repo.list_for_target.return_value = ([_record()], None)
    repo.list_for_actor.return_value = ([_record()], None)
    return repo


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/admin/audit/",
        "/admin/audit/actions",
        "/admin/audit/targets/analyst",
        "/admin/audit/actors/admin-1",
    ],
)
def test_every_route_requires_full_admin(repository, path: str) -> None:
    client = _client(repository, allow=False)
    try:
        assert client.get(path).status_code == 403
    finally:
        client._patcher.stop()


def test_the_api_exposes_no_mutating_routes() -> None:
    """Records age out via TTL and by no other means."""
    for route in audit_router.routes:
        assert set(route.methods) <= {"GET", "HEAD", "OPTIONS"}, route.path


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def test_recent_defaults_to_the_current_month(repository) -> None:
    client = _client(repository)
    try:
        body = client.get("/admin/audit/").json()
    finally:
        client._patcher.stop()

    assert repository.list_recent.call_args.args[0] == body["month"]
    assert len(body["records"]) == 1
    assert body["nextCursor"] is None


def test_recent_accepts_an_explicit_month(repository) -> None:
    client = _client(repository)
    try:
        body = client.get("/admin/audit/?month=2026-06").json()
    finally:
        client._patcher.stop()

    assert body["month"] == "2026-06"
    assert repository.list_recent.call_args.args[0] == "2026-06"


def test_a_malformed_month_is_rejected(repository) -> None:
    client = _client(repository)
    try:
        assert client.get("/admin/audit/?month=June").status_code == 422
    finally:
        client._patcher.stop()


def test_target_history_queries_the_named_role(repository) -> None:
    client = _client(repository)
    try:
        assert client.get("/admin/audit/targets/analyst").status_code == 200
    finally:
        client._patcher.stop()

    assert repository.list_for_target.call_args.args == (TARGET_APP_ROLE, "analyst")


def test_actor_history_queries_the_named_admin(repository) -> None:
    client = _client(repository)
    try:
        assert client.get("/admin/audit/actors/admin-1").status_code == 200
    finally:
        client._patcher.stop()

    assert repository.list_for_actor.call_args.args == ("admin-1",)


def test_records_are_camel_cased_for_the_console(repository) -> None:
    client = _client(repository)
    try:
        record = client.get("/admin/audit/").json()["records"][0]
    finally:
        client._patcher.stop()

    assert "actorEmail" in record
    assert "actor_email" not in record


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_a_cursor_round_trips_back_to_the_repository(repository) -> None:
    repository.list_recent.return_value = ([_record()], {"PK": "x", "SK": "y"})
    client = _client(repository)
    try:
        cursor = client.get("/admin/audit/").json()["nextCursor"]
        assert cursor
        client.get(f"/admin/audit/?cursor={cursor}")
    finally:
        client._patcher.stop()

    assert repository.list_recent.call_args.kwargs["cursor"] == {"PK": "x", "SK": "y"}


def test_a_corrupt_cursor_is_a_400_not_a_500(repository) -> None:
    client = _client(repository)
    try:
        assert client.get("/admin/audit/?cursor=not-base64!!").status_code == 400
    finally:
        client._patcher.stop()


def test_an_oversized_limit_is_rejected(repository) -> None:
    client = _client(repository)
    try:
        assert client.get("/admin/audit/?limit=5000").status_code == 422
    finally:
        client._patcher.stop()


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


def test_an_unavailable_table_is_a_503(repository) -> None:
    repository.list_recent.side_effect = RuntimeError("no table")
    client = _client(repository)
    try:
        assert client.get("/admin/audit/").status_code == 503
    finally:
        client._patcher.stop()


def test_actions_lists_the_closed_registry(repository) -> None:
    client = _client(repository)
    try:
        actions = client.get("/admin/audit/actions").json()["actions"]
    finally:
        client._patcher.stop()

    assert AuditAction.ROLE_MUTATION_DENIED in actions
    assert actions == sorted(actions)
