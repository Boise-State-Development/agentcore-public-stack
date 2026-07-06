"""Tests for schedule routes (`/schedules/*` — scheduled-runs B1, inert CRUD).

Endpoints under test:
- POST   /schedules             -> create (201; 400 cap; 422 validation)
- GET    /schedules             -> list caller's own schedules (200)
- GET    /schedules/{id}        -> get (200; 404)
- PATCH  /schedules/{id}        -> edit fields / pause / resume (200; 404; 422)
- POST   /schedules/{id}/pause  -> pause (200; 404)
- POST   /schedules/{id}/resume -> resume (200; 404)
- DELETE /schedules/{id}        -> delete (204; 404)

Every route shares the three-layer gate (cookie auth -> SCHEDULED_RUNS_ENABLED
kill switch -> `scheduled-runs` RBAC capability), pinned once via `TestGating`
and assumed enabled/authorized everywhere else. The service layer is mocked
(DynamoDB semantics are covered by tests/shared/test_scheduled_prompts.py);
these tests pin the HTTP contract and ownership isolation.
"""

from dataclasses import dataclass
from typing import List, Optional
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.schedules import routes as schedules_routes
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.scheduled_prompts.models import ScheduledPrompt
from apis.shared.scheduled_prompts.service import ScheduledPromptLimitExceeded
from tests.routes.conftest import mock_no_auth

USER_ID = "user-001"
OTHER_USER_ID = "user-002"
SCHEDULE_ID = "sched-abc123def456"
NOW = "2026-07-03T00:00:00Z"
FUTURE = "2999-01-01T09:00:00Z"


def _user(user_id: str = USER_ID) -> User:
    return User(user_id=user_id, email=f"{user_id}@example.com", name="Test User", roles=["User"])


def _make_schedule(**overrides) -> ScheduledPrompt:
    defaults = dict(
        scheduleId=SCHEDULE_ID,
        userId=USER_ID,
        assistantId=None,
        label="Morning Briefing",
        promptText="Summarize my day",
        cadence="daily",
        hourLocal=9,
        weekday=None,
        timezone="America/Boise",
        state="active",
        nextRunAt=FUTURE,
        runsToday=0,
        maxRunsPerDay=24,
        enabledTools=["class_search"],
        deliverEmail=False,
        createdAt=NOW,
        updatedAt=NOW,
    )
    defaults.update(overrides)
    return ScheduledPrompt.model_validate(defaults)


@dataclass
class _FakePermissions:
    tools: List[str]


class FakeRoleService:
    def __init__(self, tools: Optional[List[str]] = None):
        self.tools = tools if tools is not None else ["class_search", "web_search"]

    async def resolve_user_permissions(self, user):
        return _FakePermissions(tools=self.tools)


def _make_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    authed: bool = True,
    capability: bool = True,
    flag: Optional[str] = None,
    user_id: str = USER_ID,
) -> TestClient:
    monkeypatch.delenv("SKIP_AUTH", raising=False)
    if flag is None:
        monkeypatch.delenv("SCHEDULED_RUNS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("SCHEDULED_RUNS_ENABLED", flag)

    async def fake_capability(user, capability_id):
        assert capability_id == "scheduled-runs"
        return capability

    monkeypatch.setattr(schedules_routes, "user_has_capability", fake_capability)
    monkeypatch.setattr(schedules_routes, "get_app_role_service", lambda: FakeRoleService())

    app = FastAPI()
    app.include_router(schedules_routes.router)
    if authed:
        app.dependency_overrides[get_current_user_from_session] = lambda: _user(user_id)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


class TestGating:
    def test_unauthenticated_request_is_401(self, monkeypatch):
        client = _make_client(monkeypatch, authed=False)
        mock_no_auth  # (unused import kept for parity with sync_policies test style)
        assert client.get("/schedules").status_code == 401

    def test_kill_switch_off_hides_the_surface_as_404(self, monkeypatch):
        client = _make_client(monkeypatch, flag="false")
        assert client.get("/schedules").status_code == 404
        assert client.post("/schedules", json={}).status_code == 404
        assert client.get(f"/schedules/{SCHEDULE_ID}").status_code == 404
        assert client.delete(f"/schedules/{SCHEDULE_ID}").status_code == 404

    def test_flag_defaults_on_when_unset(self, monkeypatch):
        client = _make_client(monkeypatch)
        monkeypatch.setattr(schedules_routes, "list_scheduled_prompts", AsyncMock(return_value=[]))
        assert client.get("/schedules").status_code == 200

    def test_empty_flag_value_stays_on(self, monkeypatch):
        client = _make_client(monkeypatch, flag="")
        monkeypatch.setattr(schedules_routes, "list_scheduled_prompts", AsyncMock(return_value=[]))
        assert client.get("/schedules").status_code == 200

    def test_missing_capability_is_403(self, monkeypatch):
        client = _make_client(monkeypatch, capability=False)
        assert client.get("/schedules").status_code == 403
        assert client.post("/schedules", json={}).status_code == 403


# ---------------------------------------------------------------------------
# POST /schedules — create
# ---------------------------------------------------------------------------


class TestCreateSchedule:
    def test_happy_path(self, monkeypatch):
        client = _make_client(monkeypatch)
        created = _make_schedule()
        create_mock = AsyncMock(return_value=created)
        monkeypatch.setattr(schedules_routes, "create_scheduled_prompt", create_mock)

        response = client.post(
            "/schedules",
            json={
                "label": "Morning Briefing",
                "promptText": "Summarize my day",
                "cadence": "daily",
                "hourLocal": 9,
                "timezone": "America/Boise",
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["scheduleId"] == SCHEDULE_ID
        assert body["cadence"] == "daily"
        assert body["state"] == "active"

        (call,) = create_mock.call_args_list
        assert call.kwargs["user_id"] == USER_ID
        assert call.kwargs["label"] == "Morning Briefing"

    def test_none_enabled_tools_resolves_rbac_snapshot_at_creation(self, monkeypatch):
        client = _make_client(monkeypatch)
        create_mock = AsyncMock(return_value=_make_schedule())
        monkeypatch.setattr(schedules_routes, "create_scheduled_prompt", create_mock)

        client.post(
            "/schedules",
            json={
                "label": "Briefing",
                "promptText": "Go",
                "cadence": "daily",
                "hourLocal": 9,
                "timezone": "America/Boise",
            },
        )

        (call,) = create_mock.call_args_list
        assert call.kwargs["enabled_tools"] == ["class_search", "web_search"]

    def test_explicit_enabled_tools_bypasses_rbac_resolution(self, monkeypatch):
        client = _make_client(monkeypatch)
        create_mock = AsyncMock(return_value=_make_schedule())
        monkeypatch.setattr(schedules_routes, "create_scheduled_prompt", create_mock)

        client.post(
            "/schedules",
            json={
                "label": "Briefing",
                "promptText": "Go",
                "cadence": "daily",
                "hourLocal": 9,
                "timezone": "America/Boise",
                "enabledTools": ["gmail_search"],
            },
        )

        (call,) = create_mock.call_args_list
        assert call.kwargs["enabled_tools"] == ["gmail_search"]

    def test_weekly_without_weekday_is_422(self, monkeypatch):
        client = _make_client(monkeypatch)
        response = client.post(
            "/schedules",
            json={
                "label": "Weekly",
                "promptText": "Go",
                "cadence": "weekly",
                "hourLocal": 9,
                "timezone": "America/Boise",
            },
        )
        assert response.status_code == 422

    def test_over_cap_is_400(self, monkeypatch):
        client = _make_client(monkeypatch)
        monkeypatch.setattr(
            schedules_routes,
            "create_scheduled_prompt",
            AsyncMock(side_effect=ScheduledPromptLimitExceeded("too many")),
        )

        response = client.post(
            "/schedules",
            json={
                "label": "One too many",
                "promptText": "Go",
                "cadence": "daily",
                "hourLocal": 9,
                "timezone": "America/Boise",
            },
        )
        assert response.status_code == 400

    def test_empty_prompt_is_422(self, monkeypatch):
        client = _make_client(monkeypatch)
        response = client.post(
            "/schedules",
            json={
                "label": "Briefing",
                "promptText": "",
                "cadence": "daily",
                "hourLocal": 9,
                "timezone": "America/Boise",
            },
        )
        assert response.status_code == 422

    def test_hour_out_of_range_is_422(self, monkeypatch):
        client = _make_client(monkeypatch)
        response = client.post(
            "/schedules",
            json={
                "label": "Briefing",
                "promptText": "Go",
                "cadence": "daily",
                "hourLocal": 24,
                "timezone": "America/Boise",
            },
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /schedules, /schedules/{id}
# ---------------------------------------------------------------------------


class TestListAndGet:
    def test_list_returns_only_the_caller_schedules(self, monkeypatch):
        client = _make_client(monkeypatch, user_id=USER_ID)
        list_mock = AsyncMock(return_value=[_make_schedule()])
        monkeypatch.setattr(schedules_routes, "list_scheduled_prompts", list_mock)

        response = client.get("/schedules")

        assert response.status_code == 200
        assert len(response.json()["schedules"]) == 1
        list_mock.assert_awaited_once_with(USER_ID)

    def test_get_by_id(self, monkeypatch):
        client = _make_client(monkeypatch)
        monkeypatch.setattr(schedules_routes, "get_scheduled_prompt", AsyncMock(return_value=_make_schedule()))

        response = client.get(f"/schedules/{SCHEDULE_ID}")

        assert response.status_code == 200
        assert response.json()["scheduleId"] == SCHEDULE_ID

    def test_get_missing_is_404(self, monkeypatch):
        client = _make_client(monkeypatch)
        monkeypatch.setattr(schedules_routes, "get_scheduled_prompt", AsyncMock(return_value=None))

        assert client.get(f"/schedules/{SCHEDULE_ID}").status_code == 404

    def test_get_another_users_schedule_is_404_not_leaked(self, monkeypatch):
        """Ownership isolation: the route always queries by the caller's own
        user_id, so another user's schedule_id simply resolves to nothing —
        it can never be fetched cross-account regardless of guessability."""
        client = _make_client(monkeypatch, user_id=OTHER_USER_ID)
        get_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(schedules_routes, "get_scheduled_prompt", get_mock)

        response = client.get(f"/schedules/{SCHEDULE_ID}")

        assert response.status_code == 404
        get_mock.assert_awaited_once_with(OTHER_USER_ID, SCHEDULE_ID)


# ---------------------------------------------------------------------------
# PATCH /schedules/{id}
# ---------------------------------------------------------------------------


class TestUpdateSchedule:
    def test_edit_label(self, monkeypatch):
        client = _make_client(monkeypatch)
        monkeypatch.setattr(schedules_routes, "get_scheduled_prompt", AsyncMock(return_value=_make_schedule()))
        update_mock = AsyncMock(return_value=_make_schedule(label="Renamed"))
        monkeypatch.setattr(schedules_routes, "update_scheduled_prompt", update_mock)

        response = client.patch(f"/schedules/{SCHEDULE_ID}", json={"label": "Renamed"})

        assert response.status_code == 200
        assert response.json()["label"] == "Renamed"

    def test_missing_schedule_is_404(self, monkeypatch):
        client = _make_client(monkeypatch)
        monkeypatch.setattr(schedules_routes, "get_scheduled_prompt", AsyncMock(return_value=None))

        response = client.patch(f"/schedules/{SCHEDULE_ID}", json={"label": "Renamed"})
        assert response.status_code == 404

    def test_switching_to_weekly_without_weekday_is_422(self, monkeypatch):
        client = _make_client(monkeypatch)
        monkeypatch.setattr(
            schedules_routes, "get_scheduled_prompt", AsyncMock(return_value=_make_schedule(cadence="daily", weekday=None))
        )

        response = client.patch(f"/schedules/{SCHEDULE_ID}", json={"cadence": "weekly"})
        assert response.status_code == 422

    def test_state_transition_to_paused(self, monkeypatch):
        client = _make_client(monkeypatch)
        active = _make_schedule(state="active")
        paused = _make_schedule(state="paused", stateReason="Paused by user")
        monkeypatch.setattr(schedules_routes, "get_scheduled_prompt", AsyncMock(side_effect=[active, paused]))
        monkeypatch.setattr(schedules_routes, "update_scheduled_prompt", AsyncMock(return_value=active))
        set_state_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(schedules_routes, "set_schedule_state", set_state_mock)

        response = client.patch(f"/schedules/{SCHEDULE_ID}", json={"state": "paused"})

        assert response.status_code == 200
        assert response.json()["state"] == "paused"
        set_state_mock.assert_awaited_once()
        assert set_state_mock.call_args.args[2] == "paused"

    def test_state_transition_to_active_recomputes_next_run_at(self, monkeypatch):
        client = _make_client(monkeypatch)
        paused = _make_schedule(state="paused", nextRunAt=None)
        active = _make_schedule(state="active")
        monkeypatch.setattr(schedules_routes, "get_scheduled_prompt", AsyncMock(side_effect=[paused, paused, active]))
        monkeypatch.setattr(schedules_routes, "update_scheduled_prompt", AsyncMock(return_value=paused))
        set_state_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(schedules_routes, "set_schedule_state", set_state_mock)

        response = client.patch(f"/schedules/{SCHEDULE_ID}", json={"state": "active"})

        assert response.status_code == 200
        set_state_mock.assert_awaited_once()
        assert set_state_mock.call_args.args[2] == "active"
        assert set_state_mock.call_args.kwargs["next_run_at"] is not None


# ---------------------------------------------------------------------------
# POST /schedules/{id}/pause, /resume
# ---------------------------------------------------------------------------


class TestPauseResume:
    def test_pause(self, monkeypatch):
        client = _make_client(monkeypatch)
        active = _make_schedule(state="active")
        paused = _make_schedule(state="paused")
        monkeypatch.setattr(schedules_routes, "get_scheduled_prompt", AsyncMock(side_effect=[active, paused]))
        set_state_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(schedules_routes, "set_schedule_state", set_state_mock)

        response = client.post(f"/schedules/{SCHEDULE_ID}/pause")

        assert response.status_code == 200
        assert response.json()["state"] == "paused"

    def test_pause_missing_is_404(self, monkeypatch):
        client = _make_client(monkeypatch)
        monkeypatch.setattr(schedules_routes, "get_scheduled_prompt", AsyncMock(return_value=None))

        assert client.post(f"/schedules/{SCHEDULE_ID}/pause").status_code == 404

    def test_pause_already_paused_is_idempotent(self, monkeypatch):
        client = _make_client(monkeypatch)
        paused = _make_schedule(state="paused")
        monkeypatch.setattr(schedules_routes, "get_scheduled_prompt", AsyncMock(return_value=paused))
        set_state_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(schedules_routes, "set_schedule_state", set_state_mock)

        response = client.post(f"/schedules/{SCHEDULE_ID}/pause")

        assert response.status_code == 200
        set_state_mock.assert_not_awaited()

    def test_resume(self, monkeypatch):
        client = _make_client(monkeypatch)
        paused = _make_schedule(state="paused")
        active = _make_schedule(state="active")
        monkeypatch.setattr(schedules_routes, "get_scheduled_prompt", AsyncMock(side_effect=[paused, active]))
        set_state_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(schedules_routes, "set_schedule_state", set_state_mock)

        response = client.post(f"/schedules/{SCHEDULE_ID}/resume")

        assert response.status_code == 200
        assert response.json()["state"] == "active"
        set_state_mock.assert_awaited_once()
        assert set_state_mock.call_args.args[2] == "active"

    def test_resume_from_paused_error(self, monkeypatch):
        client = _make_client(monkeypatch)
        errored = _make_schedule(state="paused_error", stateReason="Too many failures")
        active = _make_schedule(state="active")
        monkeypatch.setattr(schedules_routes, "get_scheduled_prompt", AsyncMock(side_effect=[errored, active]))
        monkeypatch.setattr(schedules_routes, "set_schedule_state", AsyncMock(return_value=True))

        response = client.post(f"/schedules/{SCHEDULE_ID}/resume")
        assert response.status_code == 200

    def test_resume_missing_is_404(self, monkeypatch):
        client = _make_client(monkeypatch)
        monkeypatch.setattr(schedules_routes, "get_scheduled_prompt", AsyncMock(return_value=None))

        assert client.post(f"/schedules/{SCHEDULE_ID}/resume").status_code == 404


# ---------------------------------------------------------------------------
# DELETE /schedules/{id}
# ---------------------------------------------------------------------------


class TestDeleteSchedule:
    def test_delete(self, monkeypatch):
        client = _make_client(monkeypatch)
        monkeypatch.setattr(schedules_routes, "get_scheduled_prompt", AsyncMock(return_value=_make_schedule()))
        delete_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(schedules_routes, "delete_scheduled_prompt", delete_mock)

        response = client.delete(f"/schedules/{SCHEDULE_ID}")

        assert response.status_code == 204
        delete_mock.assert_awaited_once_with(USER_ID, SCHEDULE_ID)

    def test_delete_missing_is_404(self, monkeypatch):
        client = _make_client(monkeypatch)
        monkeypatch.setattr(schedules_routes, "get_scheduled_prompt", AsyncMock(return_value=None))

        assert client.delete(f"/schedules/{SCHEDULE_ID}").status_code == 404
