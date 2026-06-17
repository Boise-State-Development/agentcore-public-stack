"""Tests for the ``GET /files`` cursor's PK-matches-caller invariant.

The pagination cursor is a base64-encoded JSON object containing the
DynamoDB ``LastEvaluatedKey`` (which includes the partition key the
record was queried under). When a client supplies a cursor whose PK
points at a different user's partition than the calling user's,
DynamoDB rejects the query with ``ValidationException`` — historically
surfaced to the caller as a 500. The repository now validates that the
cursor's PK matches the caller's expected PK before sending it to
DynamoDB; mismatched cursors yield a generic 400 with no echo.
"""

from __future__ import annotations

import base64
import json

import pytest

from apis.shared.files.repository import FileUploadRepository, InvalidCursorError


def _cursor(pk: str, sk: str = "FILE#abc") -> str:
    return base64.b64encode(json.dumps({"PK": pk, "SK": sk}).encode()).decode()


def _repo() -> FileUploadRepository:
    """Build a repository instance without touching DynamoDB."""
    repo = FileUploadRepository.__new__(FileUploadRepository)
    repo._table = None  # type: ignore[attr-defined]
    return repo


# ---------------------------------------------------------------------------
# Cursor PK must match the caller
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cursor_with_other_user_pk_is_rejected() -> None:
    repo = _repo()
    other_pk_cursor = _cursor("USER#some-other-user")

    with pytest.raises(InvalidCursorError):
        await repo.list_user_files(user_id="caller-id", cursor=other_pk_cursor)


@pytest.mark.asyncio
async def test_cursor_with_garbage_payload_is_rejected() -> None:
    repo = _repo()
    bogus = base64.b64encode(b"not-json-at-all").decode()

    with pytest.raises(InvalidCursorError):
        await repo.list_user_files(user_id="caller-id", cursor=bogus)


@pytest.mark.asyncio
async def test_cursor_without_pk_field_is_rejected() -> None:
    repo = _repo()
    no_pk = base64.b64encode(json.dumps({"SK": "FILE#abc"}).encode()).decode()

    with pytest.raises(InvalidCursorError):
        await repo.list_user_files(user_id="caller-id", cursor=no_pk)


@pytest.mark.asyncio
async def test_invalid_cursor_returns_400_at_route(tmp_path) -> None:
    """End-to-end: a malformed cursor surfaces as a 400, not a 500.

    The InvalidCursorError → 400 mapping has to be wired by whichever
    layer registers the handler. We verify the contract at the
    repository level (above) and the route's HTTPException-on-error
    behaviour here, with the repository mocked to raise
    InvalidCursorError.
    """
    from unittest.mock import AsyncMock, patch
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apis.app_api.files.routes import router
    from tests.routes.conftest import mock_auth_user

    app = FastAPI()
    app.include_router(router)

    user_id = "caller-id"
    from apis.shared.auth.models import User

    mock_auth_user(app, User(user_id=user_id, email="c@x", name="C", roles=[]))

    fake_service = AsyncMock()
    fake_service.list_user_files = AsyncMock(side_effect=InvalidCursorError())

    with patch(
        "apis.app_api.files.routes.get_file_upload_service",
        return_value=fake_service,
    ):
        client = TestClient(app)
        resp = client.get("/files", params={"cursor": _cursor("USER#someone-else")})

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Owner's own cursor still works
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cursor_with_caller_pk_is_accepted(monkeypatch) -> None:
    """A cursor whose PK matches the caller is unwrapped into
    ExclusiveStartKey and forwarded to DynamoDB unchanged."""
    repo = _repo()

    captured: dict = {}

    class FakeTable:
        def query(self, **kwargs):
            captured.update(kwargs)
            return {"Items": [], "LastEvaluatedKey": None}

    repo._table = FakeTable()  # type: ignore[attr-defined]

    own_cursor = _cursor("USER#caller-id")
    files, next_cursor = await repo.list_user_files(
        user_id="caller-id", cursor=own_cursor
    )

    assert files == []
    assert "ExclusiveStartKey" in captured
    assert captured["ExclusiveStartKey"]["PK"] == "USER#caller-id"
