"""``maxCostPerDay`` on ``/schedules`` (docs/specs/proactive-trust.md §4.3–§4.4, PR 4.1).

- while PROACTIVE_RUNS_ENABLED is off, a budget is refused (422) and none is stored;
- while on, an interval under 60 minutes must have one: an explicit null is a 422
  naming the field, and a request that names none gets SCHEDULE_DEFAULT_MAX_COST_PER_DAY;
- an edit that leaves a short schedule with no budget gets the default, and one that
  clears it there is refused; at 60 minutes and over, null clears it;
- a resume never lands inside a budget hold;
- the response carries the budget and today's spend.

The service layer is mocked, as in test_schedules_watch_routes; the hold and release
storage rules are covered by tests/shared/test_schedule_budget.py.
"""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from apis.app_api.schedules import routes as schedules_routes
from tests.routes.test_schedules_routes import SCHEDULE_ID, _make_schedule
from tests.routes.test_schedules_watch_routes import _client

SHORT = {
    "label": "Inbox watcher",
    "promptText": "Tell me what needs me",
    "cadence": "interval",
    "intervalValue": 15,
    "intervalUnit": "minutes",
    "hourLocal": 0,
    "timezone": "America/Boise",
}
HOURLY = {**SHORT, "intervalValue": 1, "intervalUnit": "hours"}


def _stub_create(monkeypatch, **schedule):
    create = AsyncMock(return_value=_make_schedule(**schedule))
    monkeypatch.setattr(schedules_routes, "create_scheduled_prompt", create)
    return create


def _stub_update(monkeypatch, existing, updated=None):
    monkeypatch.setattr(schedules_routes, "get_scheduled_prompt", AsyncMock(return_value=existing))
    update = AsyncMock(return_value=updated or existing)
    monkeypatch.setattr(schedules_routes, "update_scheduled_prompt", update)
    return update


class TestCreate:
    def test_a_short_interval_with_no_budget_named_gets_the_default(self, monkeypatch):
        monkeypatch.delenv("SCHEDULE_DEFAULT_MAX_COST_PER_DAY", raising=False)
        create = _stub_create(monkeypatch)
        response = _client(monkeypatch).post("/schedules", json=SHORT)
        assert response.status_code == 201
        assert create.call_args.kwargs["max_cost_per_day"] == Decimal("0.50")

    def test_the_default_is_the_deployment_setting(self, monkeypatch):
        monkeypatch.setenv("SCHEDULE_DEFAULT_MAX_COST_PER_DAY", "0.75")
        create = _stub_create(monkeypatch)
        _client(monkeypatch).post("/schedules", json=SHORT)
        assert create.call_args.kwargs["max_cost_per_day"] == Decimal("0.75")

    def test_a_null_budget_under_60_minutes_is_refused(self, monkeypatch):
        create = _stub_create(monkeypatch)
        response = _client(monkeypatch).post("/schedules", json={**SHORT, "maxCostPerDay": None})
        assert response.status_code == 422
        assert "maxCostPerDay" in response.json()["detail"]
        create.assert_not_called()

    def test_a_null_budget_at_60_minutes_is_no_budget(self, monkeypatch):
        create = _stub_create(monkeypatch)
        response = _client(monkeypatch).post("/schedules", json={**HOURLY, "maxCostPerDay": None})
        assert response.status_code == 201
        assert create.call_args.kwargs["max_cost_per_day"] is None

    def test_a_daily_schedule_needs_none(self, monkeypatch):
        create = _stub_create(monkeypatch)
        body = {**SHORT, "cadence": "daily", "hourLocal": 9}
        body.pop("intervalValue")
        body.pop("intervalUnit")
        _client(monkeypatch).post("/schedules", json=body)
        assert create.call_args.kwargs["max_cost_per_day"] is None

    def test_an_explicit_budget_is_kept(self, monkeypatch):
        create = _stub_create(monkeypatch)
        _client(monkeypatch).post("/schedules", json={**SHORT, "maxCostPerDay": 0.2})
        assert create.call_args.kwargs["max_cost_per_day"] == Decimal("0.2")

    @pytest.mark.parametrize("bad", [0, -1, 5000])
    def test_a_budget_out_of_range_is_refused(self, monkeypatch, bad):
        create = _stub_create(monkeypatch)
        response = _client(monkeypatch).post("/schedules", json={**SHORT, "maxCostPerDay": bad})
        assert response.status_code == 422
        create.assert_not_called()

    @pytest.mark.parametrize("value", [None, "", "false"])
    def test_while_off_a_budget_is_refused_and_none_stored(self, monkeypatch, value):
        create = _stub_create(monkeypatch)
        client = _client(monkeypatch, proactive=value)
        refused = client.post("/schedules", json={**SHORT, "maxCostPerDay": 0.5})
        assert refused.status_code == 422
        assert "budget" in refused.json()["detail"]
        accepted = client.post("/schedules", json=SHORT)
        assert accepted.status_code == 201
        assert create.call_args.kwargs["max_cost_per_day"] is None

    def test_the_response_carries_the_budget_and_todays_spend(self, monkeypatch):
        _stub_create(
            monkeypatch,
            maxCostPerDay=Decimal("0.50"),
            spendToday=Decimal("0.04"),
            spendTodayDate="2026-12-02",
            budgetHoldUntil=None,
            budgetDaysAtLimit=0,
        )
        body = _client(monkeypatch).post("/schedules", json=SHORT).json()
        assert body["maxCostPerDay"] == 0.5
        assert body["spendToday"] == 0.04
        assert body["spendTodayDate"] == "2026-12-02"
        assert body["budgetHoldUntil"] is None
        assert body["budgetDaysAtLimit"] == 0


class TestEdit:
    def _short(self, **overrides):
        return _make_schedule(
            cadence="interval", intervalValue=15, intervalUnit="minutes", hourLocal=0, **overrides
        )

    def test_clearing_the_budget_under_60_minutes_is_refused(self, monkeypatch):
        update = _stub_update(monkeypatch, self._short(maxCostPerDay=Decimal("0.5")))
        response = _client(monkeypatch).patch(f"/schedules/{SCHEDULE_ID}", json={"maxCostPerDay": None})
        assert response.status_code == 422
        assert "maxCostPerDay" in response.json()["detail"]
        update.assert_not_called()

    def test_clearing_it_at_an_hour_clears_it(self, monkeypatch):
        update = _stub_update(monkeypatch, _make_schedule(maxCostPerDay=Decimal("0.5")))
        response = _client(monkeypatch).patch(f"/schedules/{SCHEDULE_ID}", json={"maxCostPerDay": None})
        assert response.status_code == 200
        assert update.call_args.kwargs["max_cost_per_day"] is None

    def test_an_edit_to_a_short_interval_gets_the_default(self, monkeypatch):
        monkeypatch.delenv("SCHEDULE_DEFAULT_MAX_COST_PER_DAY", raising=False)
        update = _stub_update(monkeypatch, _make_schedule())
        response = _client(monkeypatch).patch(
            f"/schedules/{SCHEDULE_ID}", json={"cadence": "interval", "intervalValue": 30, "intervalUnit": "minutes"}
        )
        assert response.status_code == 200
        assert update.call_args.kwargs["max_cost_per_day"] == Decimal("0.50")

    def test_an_edit_that_names_no_budget_leaves_it(self, monkeypatch):
        update = _stub_update(monkeypatch, self._short(maxCostPerDay=Decimal("0.2")))
        _client(monkeypatch).patch(f"/schedules/{SCHEDULE_ID}", json={"label": "Renamed"})
        assert update.call_args.kwargs["max_cost_per_day"] is schedules_routes.UNSET

    def test_raising_the_budget_is_passed_through(self, monkeypatch):
        update = _stub_update(monkeypatch, self._short(maxCostPerDay=Decimal("0.2")))
        _client(monkeypatch).patch(f"/schedules/{SCHEDULE_ID}", json={"maxCostPerDay": 1})
        assert update.call_args.kwargs["max_cost_per_day"] == Decimal("1")

    def test_while_off_a_budget_is_refused(self, monkeypatch):
        update = _stub_update(monkeypatch, _make_schedule())
        response = _client(monkeypatch, proactive="false").patch(
            f"/schedules/{SCHEDULE_ID}", json={"maxCostPerDay": 1}
        )
        assert response.status_code == 422
        update.assert_not_called()


class TestResume:
    def test_a_resume_waits_out_a_budget_hold(self, monkeypatch):
        hold = "2999-01-01T07:00:00Z"
        paused = _make_schedule(state="paused", budgetHoldUntil=hold)
        active = _make_schedule(state="active", nextRunAt=hold)
        monkeypatch.setattr(schedules_routes, "get_scheduled_prompt", AsyncMock(side_effect=[paused, active]))
        set_state = AsyncMock(return_value=True)
        monkeypatch.setattr(schedules_routes, "set_schedule_state", set_state)

        response = _client(monkeypatch).post(f"/schedules/{SCHEDULE_ID}/resume")

        assert response.status_code == 200
        assert set_state.call_args.kwargs["next_run_at"] == hold

    def test_a_resume_with_no_hold_uses_its_cadence(self, monkeypatch):
        paused = _make_schedule(state="paused", budgetHoldUntil="2000-01-01T07:00:00Z")
        monkeypatch.setattr(schedules_routes, "get_scheduled_prompt", AsyncMock(side_effect=[paused, paused]))
        set_state = AsyncMock(return_value=True)
        monkeypatch.setattr(schedules_routes, "set_schedule_state", set_state)

        _client(monkeypatch).post(f"/schedules/{SCHEDULE_ID}/resume")

        assert set_state.call_args.kwargs["next_run_at"] > "2026-01-01"
        assert set_state.call_args.kwargs["next_run_at"] != "2000-01-01T07:00:00Z"
