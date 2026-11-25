"""Quiet watcher routes and fields on `/schedules/*` (docs/specs/quiet-watcher.md, PR 1.1).

- ``mode`` / ``modelId`` on create and edit: refused (422) while
  PROACTIVE_RUNS_ENABLED is off, held to the model-access predicate while on,
  and always present on the response
- ``GET /schedules/{id}/runs``, ``GET`` / ``DELETE /schedules/{id}/state``:
  authentication first, then 404 while the flag is off; 404 for a schedule
  that isn't the caller's or that runs in a project

The service layer is mocked; storage is covered by
tests/shared/test_scheduled_prompt_watch_state.py.
"""

from typing import Optional, Set
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.schedules import routes as schedules_routes
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.scheduled_prompts.runs import ScheduleRun
from apis.shared.scheduled_prompts.watch_state import WatchState
from tests.routes.test_schedules_routes import SCHEDULE_ID, USER_ID, FakeRoleService, _make_schedule, _user

ALLOWED_MODEL = "us.anthropic.claude-haiku-4-5"

CREATE_BODY = {
    "label": "Inbox watcher",
    "promptText": "Tell me what needs me",
    "cadence": "daily",
    "hourLocal": 9,
    "timezone": "America/Boise",
}


class ModelRoleService(FakeRoleService):
    def __init__(self, models: Optional[Set[str]] = None):
        super().__init__()
        self.models = models if models is not None else {ALLOWED_MODEL}
        self.model_checks = []

    async def can_access_model(self, user, model_id, **kwargs):
        self.model_checks.append(model_id)
        return model_id in self.models


def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    proactive: Optional[str] = "true",
    authed: bool = True,
    roles: Optional[ModelRoleService] = None,
) -> TestClient:
    monkeypatch.delenv("SKIP_AUTH", raising=False)
    monkeypatch.delenv("SCHEDULED_RUNS_ENABLED", raising=False)
    if proactive is None:
        monkeypatch.delenv("PROACTIVE_RUNS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", proactive)
    role_service = roles or ModelRoleService()
    monkeypatch.setattr(schedules_routes, "get_app_role_service", lambda: role_service)
    monkeypatch.setattr(schedules_routes, "remember_owner_email", MagicMock(return_value=True))

    app = FastAPI()
    app.include_router(schedules_routes.router)
    if authed:
        app.dependency_overrides[get_current_user_from_session] = lambda: _user()
    return TestClient(app, raise_server_exceptions=False)


def _stub_read(monkeypatch, schedule):
    read = MagicMock(return_value=schedule)
    monkeypatch.setattr(schedules_routes, "read_scheduled_prompt", read)
    return read


ROUTES = [
    ("get", f"/schedules/{SCHEDULE_ID}/runs"),
    ("get", f"/schedules/{SCHEDULE_ID}/state"),
    ("delete", f"/schedules/{SCHEDULE_ID}/state"),
]


# ---------------------------------------------------------------------------
# mode and modelId on create / edit
# ---------------------------------------------------------------------------


class TestFieldsWhileOff:
    @pytest.mark.parametrize("value", [None, "", "false"])
    @pytest.mark.parametrize(
        "extra", [{"mode": "watch"}, {"modelId": ALLOWED_MODEL}, {"mode": "watch", "modelId": ALLOWED_MODEL}]
    )
    def test_create_refuses_them(self, monkeypatch, value, extra):
        client = _client(monkeypatch, proactive=value)
        create = AsyncMock(return_value=_make_schedule())
        monkeypatch.setattr(schedules_routes, "create_scheduled_prompt", create)

        response = client.post("/schedules", json={**CREATE_BODY, **extra})

        assert response.status_code == 422
        assert "not available" in response.json()["detail"]
        create.assert_not_called()

    def test_create_without_them_is_unchanged_and_answers_the_defaults(self, monkeypatch):
        client = _client(monkeypatch, proactive=None)
        create = AsyncMock(return_value=_make_schedule())
        monkeypatch.setattr(schedules_routes, "create_scheduled_prompt", create)

        response = client.post("/schedules", json={**CREATE_BODY, "mode": "standard"})

        assert response.status_code == 201
        body = response.json()
        assert body["mode"] == "standard"
        assert body["modelId"] is None
        assert create.call_args.kwargs["mode"] == "standard"
        assert create.call_args.kwargs["model_id"] is None

    @pytest.mark.parametrize("extra", [{"mode": "watch"}, {"modelId": ALLOWED_MODEL}, {"clearModel": True}])
    def test_edit_refuses_them(self, monkeypatch, extra):
        client = _client(monkeypatch, proactive="false")
        monkeypatch.setattr(schedules_routes, "get_scheduled_prompt", AsyncMock(return_value=_make_schedule()))
        update = AsyncMock(return_value=_make_schedule())
        monkeypatch.setattr(schedules_routes, "update_scheduled_prompt", update)

        response = client.patch(f"/schedules/{SCHEDULE_ID}", json=extra)

        assert response.status_code == 422
        update.assert_not_called()

    def test_an_edit_without_them_leaves_the_model_alone(self, monkeypatch):
        client = _client(monkeypatch, proactive=None)
        monkeypatch.setattr(schedules_routes, "get_scheduled_prompt", AsyncMock(return_value=_make_schedule()))
        update = AsyncMock(return_value=_make_schedule(label="Renamed"))
        monkeypatch.setattr(schedules_routes, "update_scheduled_prompt", update)

        response = client.patch(f"/schedules/{SCHEDULE_ID}", json={"label": "Renamed"})

        assert response.status_code == 200
        assert update.call_args.kwargs["mode"] is None
        assert update.call_args.kwargs["model_id"] is schedules_routes.UNSET


class TestFieldsWhileOn:
    def test_create_a_watcher_on_a_model_the_caller_can_use(self, monkeypatch):
        roles = ModelRoleService()
        client = _client(monkeypatch, roles=roles)
        created = _make_schedule(mode="watch", modelId=ALLOWED_MODEL)
        create = AsyncMock(return_value=created)
        monkeypatch.setattr(schedules_routes, "create_scheduled_prompt", create)

        response = client.post("/schedules", json={**CREATE_BODY, "mode": "watch", "modelId": ALLOWED_MODEL})

        assert response.status_code == 201
        assert response.json()["mode"] == "watch"
        assert response.json()["modelId"] == ALLOWED_MODEL
        assert create.call_args.kwargs["mode"] == "watch"
        assert create.call_args.kwargs["model_id"] == ALLOWED_MODEL
        assert roles.model_checks == [ALLOWED_MODEL]

    def test_create_refuses_a_model_the_caller_cannot_use(self, monkeypatch):
        client = _client(monkeypatch)
        create = AsyncMock()
        monkeypatch.setattr(schedules_routes, "create_scheduled_prompt", create)

        response = client.post("/schedules", json={**CREATE_BODY, "modelId": "us.anthropic.claude-opus-5-5"})

        assert response.status_code == 422
        assert "us.anthropic.claude-opus-5-5" in response.json()["detail"]
        create.assert_not_called()

    def test_watch_mode_alone_needs_no_model_check(self, monkeypatch):
        roles = ModelRoleService()
        client = _client(monkeypatch, roles=roles)
        monkeypatch.setattr(schedules_routes, "create_scheduled_prompt", AsyncMock(return_value=_make_schedule()))

        assert client.post("/schedules", json={**CREATE_BODY, "mode": "watch"}).status_code == 201
        assert roles.model_checks == []

    def test_edit_sets_a_usable_model(self, monkeypatch):
        client = _client(monkeypatch)
        monkeypatch.setattr(schedules_routes, "get_scheduled_prompt", AsyncMock(return_value=_make_schedule()))
        update = AsyncMock(return_value=_make_schedule(modelId=ALLOWED_MODEL))
        monkeypatch.setattr(schedules_routes, "update_scheduled_prompt", update)

        response = client.patch(f"/schedules/{SCHEDULE_ID}", json={"modelId": ALLOWED_MODEL, "mode": "watch"})

        assert response.status_code == 200
        assert update.call_args.kwargs["model_id"] == ALLOWED_MODEL
        assert update.call_args.kwargs["mode"] == "watch"

    def test_edit_refuses_an_unusable_model(self, monkeypatch):
        client = _client(monkeypatch)
        monkeypatch.setattr(schedules_routes, "get_scheduled_prompt", AsyncMock(return_value=_make_schedule()))
        update = AsyncMock()
        monkeypatch.setattr(schedules_routes, "update_scheduled_prompt", update)

        response = client.patch(f"/schedules/{SCHEDULE_ID}", json={"modelId": "not-in-catalog"})

        assert response.status_code == 422
        update.assert_not_called()

    def test_clear_model_reverts_to_the_default(self, monkeypatch):
        client = _client(monkeypatch)
        monkeypatch.setattr(
            schedules_routes, "get_scheduled_prompt", AsyncMock(return_value=_make_schedule(modelId=ALLOWED_MODEL))
        )
        update = AsyncMock(return_value=_make_schedule())
        monkeypatch.setattr(schedules_routes, "update_scheduled_prompt", update)

        response = client.patch(f"/schedules/{SCHEDULE_ID}", json={"clearModel": True})

        assert response.status_code == 200
        assert update.call_args.kwargs["model_id"] is None

    def test_clear_model_with_a_model_is_invalid(self, monkeypatch):
        client = _client(monkeypatch)
        response = client.patch(f"/schedules/{SCHEDULE_ID}", json={"clearModel": True, "modelId": ALLOWED_MODEL})
        assert response.status_code == 422

    def test_a_project_schedule_is_still_changed_from_its_project(self, monkeypatch):
        client = _client(monkeypatch)
        monkeypatch.setattr(
            schedules_routes, "get_scheduled_prompt", AsyncMock(return_value=_make_schedule(projectId="proj-1"))
        )
        update = AsyncMock()
        monkeypatch.setattr(schedules_routes, "update_scheduled_prompt", update)

        response = client.patch(f"/schedules/{SCHEDULE_ID}", json={"mode": "watch", "modelId": ALLOWED_MODEL})

        assert response.status_code == 409
        update.assert_not_called()


# ---------------------------------------------------------------------------
# /runs and /state
# ---------------------------------------------------------------------------


class TestWatchRoutesGating:
    @pytest.mark.parametrize("method,path", ROUTES)
    def test_authentication_comes_first(self, monkeypatch, method, path):
        for flag in ("true", None):
            client = _client(monkeypatch, proactive=flag, authed=False)
            assert getattr(client, method)(path).status_code == 401

    @pytest.mark.parametrize("value", [None, "", "false"])
    @pytest.mark.parametrize("method,path", ROUTES)
    def test_404_while_the_flag_is_off(self, monkeypatch, method, path, value):
        client = _client(monkeypatch, proactive=value)
        read = _stub_read(monkeypatch, _make_schedule())
        assert getattr(client, method)(path).status_code == 404
        read.assert_not_called()

    @pytest.mark.parametrize("method,path", ROUTES)
    def test_404_while_scheduled_runs_is_off(self, monkeypatch, method, path):
        client = _client(monkeypatch)
        monkeypatch.setenv("SCHEDULED_RUNS_ENABLED", "false")
        assert getattr(client, method)(path).status_code == 404

    @pytest.mark.parametrize("method,path", ROUTES)
    def test_404_for_a_schedule_that_is_not_the_callers(self, monkeypatch, method, path):
        client = _client(monkeypatch)
        read = _stub_read(monkeypatch, None)
        clear = MagicMock()
        monkeypatch.setattr(schedules_routes, "clear_watch_state", clear)

        assert getattr(client, method)(path).status_code == 404
        # Looked up in the caller's own partition, so another user's id finds nothing.
        assert read.call_args.args == (USER_ID, SCHEDULE_ID)
        clear.assert_not_called()

    @pytest.mark.parametrize("method,path", ROUTES)
    def test_404_for_a_project_schedule(self, monkeypatch, method, path):
        client = _client(monkeypatch)
        _stub_read(monkeypatch, _make_schedule(projectId="proj-1"))
        clear = MagicMock()
        monkeypatch.setattr(schedules_routes, "clear_watch_state", clear)

        assert getattr(client, method)(path).status_code == 404
        clear.assert_not_called()


class TestWatchRoutes:
    def test_runs_newest_first(self, monkeypatch):
        client = _client(monkeypatch)
        _stub_read(monkeypatch, _make_schedule())
        runs = [
            ScheduleRun(
                run_id="run-2", schedule_id=SCHEDULE_ID, status="completed", finished_at="2026-11-25T02:00:00Z",
                session_id="headless-2", input_tokens=2000, output_tokens=40, cache_read_tokens=1200,
            ),
            ScheduleRun(run_id="run-1", schedule_id=SCHEDULE_ID, status="error", finished_at="2026-11-25T01:00:00Z"),
        ]
        listing = MagicMock(return_value=runs)
        monkeypatch.setattr(schedules_routes, "list_schedule_runs", listing)

        response = client.get(f"/schedules/{SCHEDULE_ID}/runs")

        assert response.status_code == 200
        body = response.json()["runs"]
        assert [r["runId"] for r in body] == ["run-2", "run-1"]
        assert body[0] == {
            "runId": "run-2",
            "scheduleId": SCHEDULE_ID,
            "status": "completed",
            "itemCount": 0,
            "sessionId": "headless-2",
            "finishedAt": "2026-11-25T02:00:00Z",
            "inputTokens": 2000,
            "outputTokens": 40,
            "cacheReadTokens": 1200,
        }
        assert listing.call_args.args == (USER_ID, SCHEDULE_ID)

    def test_state_is_null_before_a_watcher_has_run(self, monkeypatch):
        client = _client(monkeypatch)
        _stub_read(monkeypatch, _make_schedule(mode="watch"))
        monkeypatch.setattr(schedules_routes, "get_watch_state", MagicMock(return_value=None))

        response = client.get(f"/schedules/{SCHEDULE_ID}/state")

        assert response.status_code == 200
        assert response.json() == {"state": None}

    def test_state(self, monkeypatch):
        client = _client(monkeypatch)
        _stub_read(monkeypatch, _make_schedule(mode="watch"))
        state = WatchState(
            cursors={"since": "2026-11-25T00:00:00Z"}, tracking=["PR 12"], seen=["a"], version=3,
            last_run_at="2026-11-25T01:00:00Z", updated_at="2026-11-25T01:00:01Z",
        )
        monkeypatch.setattr(schedules_routes, "get_watch_state", MagicMock(return_value=state))

        response = client.get(f"/schedules/{SCHEDULE_ID}/state")

        assert response.status_code == 200
        assert response.json()["state"] == {
            "cursors": {"since": "2026-11-25T00:00:00Z"},
            "tracking": ["PR 12"],
            "seen": ["a"],
            "version": 3,
            "lastRunAt": "2026-11-25T01:00:00Z",
            "updatedAt": "2026-11-25T01:00:01Z",
        }

    def test_start_over_clears_the_state(self, monkeypatch):
        client = _client(monkeypatch)
        _stub_read(monkeypatch, _make_schedule(mode="watch"))
        clear = MagicMock(return_value=True)
        monkeypatch.setattr(schedules_routes, "clear_watch_state", clear)

        response = client.delete(f"/schedules/{SCHEDULE_ID}/state")

        assert response.status_code == 204
        clear.assert_called_once_with(USER_ID, SCHEDULE_ID)
