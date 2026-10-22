"""Tests for FastAPI auth dependencies.

Covers:
- get_current_user_trusted: JWT decode without signature verification
- get_current_user_id: convenience wrapper returning user_id string

Requirements: 10.5, 10.6
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from apis.shared.auth import dependencies as deps
from apis.shared.auth.dependencies import (
    get_current_user_id,
    get_current_user_trusted,
)
from apis.shared.auth.models import User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bearer(token: str):
    """Create a mock HTTPAuthorizationCredentials with the given token."""
    creds = MagicMock()
    creds.credentials = token
    return creds


# ---------------------------------------------------------------------------
# get_current_user_trusted tests
# ---------------------------------------------------------------------------


class TestGetCurrentUserTrusted:
    """Tests for the get_current_user_trusted dependency."""

    @pytest.mark.asyncio
    async def test_trusted_decode_success(self, make_jwt):
        """Valid Bearer token decoded without signature verification, returns User."""
        token = make_jwt(
            claims={
                "sub": "trusted-user-001",
                "email": "trusted@example.com",
                "name": "Trusted User",
                "roles": ["Admin"],
            },
        )

        with patch(
            "apis.shared.auth.dependencies._get_user_sync_service",
            return_value=None,
        ):
            user = await get_current_user_trusted(credentials=_bearer(token))

        assert isinstance(user, User)
        assert user.user_id == "trusted-user-001"
        assert user.email == "trusted@example.com"
        assert user.name == "Trusted User"
        assert user.roles == ["Admin"]
        assert user.raw_token == token

    @pytest.mark.asyncio
    async def test_trusted_cognito_groups(self, make_jwt):
        """Trusted path extracts cognito:groups as roles."""
        token = make_jwt(
            claims={
                "sub": "cognito-user-001",
                "email": "cognito@example.com",
                "name": "Cognito User",
                "cognito:groups": ["system_admin", "developer"],
            },
        )

        with patch(
            "apis.shared.auth.dependencies._get_user_sync_service",
            return_value=None,
        ):
            user = await get_current_user_trusted(credentials=_bearer(token))

        assert user.roles == ["system_admin", "developer"]

    @pytest.mark.asyncio
    async def test_trusted_malformed_token(self):
        """Malformed token raises 401 with 'Malformed token.'."""
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user_trusted(credentials=_bearer("not.a.jwt"))

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Malformed token."

    @pytest.mark.asyncio
    async def test_trusted_fallback_claims(self, make_jwt):
        """Standard OIDC claims (sub, email, name, roles) are extracted correctly."""
        token = make_jwt(
            claims={
                "sub": "fallback-user",
                "email": "fallback@example.com",
                "name": "Fallback User",
                "roles": ["Reader"],
            },
        )

        with patch(
            "apis.shared.auth.dependencies._get_user_sync_service",
            return_value=None,
        ):
            user = await get_current_user_trusted(credentials=_bearer(token))

        assert user.user_id == "fallback-user"
        assert user.email == "fallback@example.com"
        assert user.name == "Fallback User"
        assert user.roles == ["Reader"]

    @pytest.mark.asyncio
    async def test_trusted_missing_user_id(self, make_jwt):
        """Missing sub claim raises 401 with 'Invalid user.'."""
        token = make_jwt(
            claims={
                "sub": None,
                "email": "nouser@example.com",
                "name": "No User",
            },
        )

        with patch(
            "apis.shared.auth.dependencies._get_user_sync_service",
            return_value=None,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user_trusted(credentials=_bearer(token))

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Invalid user."

    @pytest.mark.asyncio
    async def test_trusted_no_credentials_401(self):
        """Trusted path also rejects missing credentials with 401."""
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user_trusted(credentials=None)

        assert exc_info.value.status_code == 401
        assert "WWW-Authenticate" in (exc_info.value.headers or {})


# ---------------------------------------------------------------------------
# get_current_user_id tests
# ---------------------------------------------------------------------------


class TestGetCurrentUserId:
    """Tests for the get_current_user_id dependency."""

    @pytest.mark.asyncio
    async def test_returns_string(self, make_user):
        """get_current_user_id returns the resolved user's user_id."""
        expected_user = make_user(user_id="uid-42")

        user_id = await get_current_user_id(user=expected_user)

        assert user_id == "uid-42"
        assert isinstance(user_id, str)


# ---------------------------------------------------------------------------
# Background user-sync throttle tests
# ---------------------------------------------------------------------------


class TestUserSyncThrottle:
    """The per-request profile upsert must collapse to one write per window.

    Regression cover for the first-load fan-out: 12 concurrent SPA calls each
    fired `sync_user_from_jwt`, and that upsert is a GetItem + PutItem, so a
    single page load issued 24 DynamoDB operations against one item.
    """

    def setup_method(self):
        deps.reset_user_sync_throttle()

    def teardown_method(self):
        deps.reset_user_sync_throttle()

    def test_first_claim_wins_rest_are_throttled(self):
        """Only the first of a burst claims the sync."""
        claims = [deps._claim_user_sync("uid-1") for _ in range(12)]

        assert claims[0] is True
        assert not any(claims[1:])

    def test_throttle_is_per_user(self):
        """One user's claim must not suppress another's — a classroom of
        students signing in together each need their own row created."""
        assert deps._claim_user_sync("uid-a") is True
        assert deps._claim_user_sync("uid-b") is True

    def test_claim_allowed_again_after_window(self, monkeypatch):
        """Past the window the sync resumes, so the refresh still happens."""
        monkeypatch.setattr(deps, "_USER_SYNC_THROTTLE_SECONDS", 300)

        clock = {"now": 1_000.0}
        monkeypatch.setattr(deps.time, "monotonic", lambda: clock["now"])

        assert deps._claim_user_sync("uid-1") is True
        clock["now"] += 299
        assert deps._claim_user_sync("uid-1") is False
        clock["now"] += 2  # now 301s past the first claim
        assert deps._claim_user_sync("uid-1") is True

    def test_reset_forces_immediate_resync(self):
        """An explicit reset lets the next request through without waiting."""
        assert deps._claim_user_sync("uid-1") is True
        assert deps._claim_user_sync("uid-1") is False

        deps.reset_user_sync_throttle("uid-1")

        assert deps._claim_user_sync("uid-1") is True

    def test_tracker_is_pruned_when_it_grows(self, monkeypatch):
        """A long-lived container that has served many users stays bounded."""
        monkeypatch.setattr(deps, "_USER_SYNC_THROTTLE_SECONDS", 300)
        monkeypatch.setattr(deps, "_USER_SYNC_TRACKER_MAX", 10)

        clock = {"now": 1_000.0}
        monkeypatch.setattr(deps.time, "monotonic", lambda: clock["now"])

        for i in range(10):
            deps._claim_user_sync(f"old-{i}")

        # Move past the window so every recorded entry is now stale, then add
        # one more to trip the prune.
        clock["now"] += 301
        deps._claim_user_sync("fresh")

        assert list(deps._user_sync_last_run) == ["fresh"]

    def test_schedule_is_noop_when_sync_disabled(self, make_user, monkeypatch):
        """No sync service configured means no task and no claim consumed."""
        monkeypatch.setattr(deps, "_get_user_sync_service", lambda: None)

        deps._schedule_user_sync(make_user(user_id="uid-1"))

        assert deps._user_sync_last_run == {}
        assert deps._user_sync_tasks == set()

    @pytest.mark.asyncio
    async def test_schedule_dispatches_once_per_window(self, make_user):
        """End to end: a 12-call page load produces exactly one sync."""
        service = MagicMock()
        service.enabled = True
        calls = []

        async def _record(user):
            calls.append(user.user_id)

        service.sync_user_from_jwt = _record
        user = make_user(user_id="uid-1")

        with patch.object(deps, "_get_user_sync_service", return_value=service):
            for _ in range(12):
                deps._schedule_user_sync(user)
            # Let the dispatched task run to completion.
            await asyncio.gather(*list(deps._user_sync_tasks))

        assert calls == ["uid-1"]

    @pytest.mark.asyncio
    async def test_task_reference_is_held_then_released(self, make_user):
        """The task is strongly referenced while in flight (so the GC cannot
        collect it mid-await) and discarded once it finishes."""
        service = MagicMock()
        service.enabled = True
        started = asyncio.Event()
        release = asyncio.Event()

        async def _block(user):
            started.set()
            await release.wait()

        service.sync_user_from_jwt = _block

        with patch.object(deps, "_get_user_sync_service", return_value=service):
            deps._schedule_user_sync(make_user(user_id="uid-1"))
            await started.wait()
            assert len(deps._user_sync_tasks) == 1

            release.set()
            await asyncio.gather(*list(deps._user_sync_tasks))

        assert deps._user_sync_tasks == set()
