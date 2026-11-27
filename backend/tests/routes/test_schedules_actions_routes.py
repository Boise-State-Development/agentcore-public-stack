"""``actWithoutAsking`` and ``GET /schedules/{id}/actions`` (docs/specs/proactive-trust.md §7.4–§7.5, PR 4.2).

- ``actWithoutAsking`` is refused while PROACTIVE_RUNS_ENABLED is off; while on, each
  entry is one tool on one server the caller may use by RBAC, at most 20;
- ``GET …/actions``: authentication first, 404 while the flag is off, 404 for a
  schedule that isn't the caller's or that runs in a project, and the rows newest first.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from apis.app_api.schedules import routes as schedules_routes
from apis.shared.scheduled_prompts.actions import ScheduleAction
from tests.routes.test_schedules_routes import SCHEDULE_ID, USER_ID, _make_schedule
from tests.routes.test_schedules_watch_routes import CREATE_BODY, ModelRoleService, _client, _stub_read


class GrantingRoles(ModelRoleService):
    def __init__(self, tools):
        super().__init__()
        self.tools = tools


def _create(monkeypatch, **schedule):
    create = AsyncMock(return_value=_make_schedule(**schedule))
    monkeypatch.setattr(schedules_routes, "create_scheduled_prompt", create)
    return create


class TestActWithoutAsking:
    @pytest.mark.parametrize("value", [None, "false"])
    def test_refused_while_off(self, monkeypatch, value):
        create = _create(monkeypatch)
        response = _client(monkeypatch, proactive=value).post(
            "/schedules", json={**CREATE_BODY, "actWithoutAsking": ["mail-mcp/create_draft"]}
        )
        assert response.status_code == 422
        create.assert_not_called()

    def test_an_entry_the_caller_may_use_is_kept(self, monkeypatch):
        create = _create(monkeypatch, actWithoutAsking=["mail-mcp/create_draft"])
        client = _client(monkeypatch, roles=GrantingRoles(["mail-mcp", "class_search"]))
        response = client.post("/schedules", json={**CREATE_BODY, "actWithoutAsking": ["mail-mcp/create_draft", "mail-mcp/create_draft"]})
        assert response.status_code == 201
        assert create.call_args.kwargs["act_without_asking"] == ["mail-mcp/create_draft"]
        assert response.json()["actWithoutAsking"] == ["mail-mcp/create_draft"]

    def test_a_scoped_grant_allows_its_one_tool(self, monkeypatch):
        create = _create(monkeypatch)
        client = _client(monkeypatch, roles=GrantingRoles(["mail-mcp::create_draft"]))
        assert client.post("/schedules", json={**CREATE_BODY, "actWithoutAsking": ["mail-mcp/create_draft"]}).status_code == 201
        refused = client.post("/schedules", json={**CREATE_BODY, "actWithoutAsking": ["mail-mcp/send"]})
        assert refused.status_code == 422 and "mail-mcp/send" in refused.json()["detail"]
        assert create.call_count == 1

    def test_a_tool_the_caller_cannot_use_is_refused(self, monkeypatch):
        create = _create(monkeypatch)
        client = _client(monkeypatch, roles=GrantingRoles(["class_search"]))
        response = client.post("/schedules", json={**CREATE_BODY, "actWithoutAsking": ["mail-mcp/create_draft"]})
        assert response.status_code == 422
        create.assert_not_called()

    @pytest.mark.parametrize("entry", ["mail-mcp", "mail-mcp/", "/send", "mail mcp/send", "a/b/c"])
    def test_an_entry_names_one_tool_on_one_server(self, monkeypatch, entry):
        create = _create(monkeypatch)
        client = _client(monkeypatch, roles=GrantingRoles(["*"]))
        assert client.post("/schedules", json={**CREATE_BODY, "actWithoutAsking": [entry]}).status_code == 422
        create.assert_not_called()

    def test_at_most_twenty(self, monkeypatch):
        _create(monkeypatch)
        client = _client(monkeypatch, roles=GrantingRoles(["*"]))
        entries = [f"mail-mcp/t{i}" for i in range(21)]
        assert client.post("/schedules", json={**CREATE_BODY, "actWithoutAsking": entries}).status_code == 422

    def test_edit_replaces_and_an_empty_list_clears(self, monkeypatch):
        monkeypatch.setattr(schedules_routes, "get_scheduled_prompt", AsyncMock(return_value=_make_schedule()))
        update = AsyncMock(return_value=_make_schedule())
        monkeypatch.setattr(schedules_routes, "update_scheduled_prompt", update)
        client = _client(monkeypatch, roles=GrantingRoles(["*"]))
        client.patch(f"/schedules/{SCHEDULE_ID}", json={"actWithoutAsking": ["mail-mcp/create_draft"]})
        assert update.call_args.kwargs["act_without_asking"] == ["mail-mcp/create_draft"]
        client.patch(f"/schedules/{SCHEDULE_ID}", json={"actWithoutAsking": []})
        assert update.call_args.kwargs["act_without_asking"] is None
        client.patch(f"/schedules/{SCHEDULE_ID}", json={"label": "Renamed"})
        assert update.call_args.kwargs["act_without_asking"] is schedules_routes.UNSET


class TestActionsRoute:
    def test_authentication_first(self, monkeypatch):
        response = _client(monkeypatch, authed=False).get(f"/schedules/{SCHEDULE_ID}/actions")
        assert response.status_code == 401

    @pytest.mark.parametrize("value", [None, "", "false"])
    def test_404_while_off(self, monkeypatch, value):
        assert _client(monkeypatch, proactive=value).get(f"/schedules/{SCHEDULE_ID}/actions").status_code == 404

    def test_404_for_a_project_schedule_or_none(self, monkeypatch):
        client = _client(monkeypatch)
        _stub_read(monkeypatch, None)
        assert client.get(f"/schedules/{SCHEDULE_ID}/actions").status_code == 404
        _stub_read(monkeypatch, _make_schedule(projectId="prj_1"))
        assert client.get(f"/schedules/{SCHEDULE_ID}/actions").status_code == 404

    def test_lists_the_rows(self, monkeypatch):
        client = _client(monkeypatch)
        _stub_read(monkeypatch, _make_schedule(mode="watch"))
        action = ScheduleAction.model_validate({
            "runId": "run-1", "sessionId": "headless-1", "toolUseId": "tu-1", "trigger": "schedule",
            "toolName": "send", "toolId": "mail-mcp", "serverName": "Mail", "class": "write",
            "why": "destructive_hint", "approval": "approved", "approvedVia": "inbox", "outcome": "ok",
            "startedAt": "2026-11-27T10:00:00Z", "finishedAt": "2026-11-27T10:00:01Z", "rounds": 1,
        })
        listing = MagicMock(return_value=[action])
        monkeypatch.setattr(schedules_routes, "list_schedule_actions", listing)

        response = client.get(f"/schedules/{SCHEDULE_ID}/actions")

        assert response.status_code == 200
        [row] = response.json()["actions"]
        assert (row["toolName"], row["class"], row["approvedVia"], row["outcome"]) == ("send", "write", "inbox", "ok")
        assert listing.call_args.args == (USER_ID, SCHEDULE_ID)
