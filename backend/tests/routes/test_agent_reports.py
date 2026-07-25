"""Agent Marketplace Phase 8 — the report routes (D15).

Two surfaces, and the rules that make them different from every other pair of routes in
the marketplace:

* **The user's half** is gated on *publication*, not just access (D15.3) — you may report
  what the store offered you — and one open report per reporter (D15.4), which is what
  keeps the queue from being floodable and the nav count from becoming noise.

* **The admin's half** is the only place the reporter is ever visible (D15.2), and it must
  not be reachable from anywhere else. Several cases below exist purely to assert an
  *absence*: no report content on a user-facing read, no listing change from a resolution,
  no report count anywhere near the store.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.agents.routes import router as admin_router
from apis.app_api.agent_designer.routes import router as agents_router
from apis.shared.assistants.models import AgentListing, AgentReport, Assistant
from apis.shared.auth import require_admin
from tests.routes.conftest import mock_auth_user

REPORT_SERVICE = "apis.app_api.agent_designer.services.report_service"
AGENT_ROUTES = "apis.app_api.agent_designer.routes"

AGENT = "ast-001"


def _make_assistant(listing_state="published", **overrides) -> Assistant:
    defaults = dict(
        assistantId=AGENT,
        ownerId="user-author",
        ownerName="Ada Author",
        name="Policy Lookup",
        description="Find and cite university policy",
        instructions="SECRET SYSTEM PROMPT",
        vectorIndexId="idx-001",
        visibility="PUBLIC",
        usageCount=0,
        createdAt="2026-07-01T00:00:00Z",
        updatedAt="2026-07-01T00:00:00Z",
        status="COMPLETE",
        emoji="📋",
        tagline="Find and cite university policy",
    )
    if listing_state is not None:
        defaults["listing"] = AgentListing(
            state=listing_state, category="Administration", publisher_id="pub-registrar"
        ).model_dump(by_alias=True)
    defaults.update(overrides)
    return Assistant.model_validate(defaults)


def _report(**overrides) -> AgentReport:
    defaults = dict(
        reportId="rep-abc",
        agentId=AGENT,
        reporterId="user-001",
        reporterName="Pat Reporter",
        reason="broken",
        note="It errors out",
        state="open",
        createdAt="2026-07-24T00:00:00Z",
    )
    defaults.update(overrides)
    return AgentReport.model_validate(defaults)


@pytest.fixture
def app():
    _app = FastAPI()
    _app.include_router(agents_router)
    _app.include_router(admin_router, prefix="/admin")
    return _app


@pytest.fixture(autouse=True)
def _flags_on(monkeypatch):
    monkeypatch.setenv("AGENTS_API_ENABLED", "true")
    monkeypatch.setenv("AGENT_MARKETPLACE_ENABLED", "true")


@pytest.fixture
def client(app, make_user):
    mock_auth_user(app, make_user(user_id="user-001", email="pat@example.edu"))
    app.dependency_overrides[require_admin] = lambda: make_user(
        user_id="admin-1", email="admin@example.edu"
    )
    return TestClient(app)


_UNSET = object()


def _report_agent(client, *, assistant=_UNSET, body=None, submitted=None):
    """POST the report with the storage layer stubbed at the service's own boundary.

    ``assistant=None`` means "the access check denies", which is why the default is a
    sentinel rather than ``None``.
    """
    resolved = _make_assistant() if assistant is _UNSET else assistant

    with (
        patch(
            f"{REPORT_SERVICE}.get_assistant_with_access_check",
            AsyncMock(return_value=(resolved, "viewer") if resolved else (None, None)),
        ),
        patch(
            f"{REPORT_SERVICE}.submit_report",
            AsyncMock(return_value=submitted or (_report(), False)),
        ) as submit,
    ):
        response = client.post(
            f"/agents/{AGENT}/report", json=body or {"reason": "broken", "note": "It errors out"}
        )
    return response, submit


# ── reportable means published (D15.3) ───────────────────────────────────────────────
def test_a_published_agent_can_be_reported(client):
    response, submit = _report_agent(client)

    assert response.status_code == 201
    assert submit.await_count == 1
    assert response.json()["state"] == "open"


@pytest.mark.parametrize("state", ["private", "in_review", "changes_requested", "taken_down"])
def test_an_unpublished_agent_cannot_be_reported(client, state):
    """You may report what the store offered you — nothing else (D15.3).

    A takedown is not available as a remedy for something that was never listed, so there
    is no useful thing the queue could do with the report either.
    """
    response, submit = _report_agent(client, assistant=_make_assistant(listing_state=state))

    assert response.status_code == 400
    assert submit.await_count == 0, "nothing may reach storage for an unpublished agent"


def test_an_agent_that_was_never_submitted_cannot_be_reported(client):
    response, submit = _report_agent(client, assistant=_make_assistant(listing_state=None))

    assert response.status_code == 400
    assert submit.await_count == 0


def test_an_agent_the_caller_cannot_reach_is_a_404(client):
    """The access check runs first, so reporting is not a way to probe for agents."""
    response, submit = _report_agent(client, assistant=None)

    assert response.status_code == 404
    assert submit.await_count == 0


def test_an_unknown_reason_is_refused(client):
    """The fixed set is what lets the queue sort by severity without reading every note."""
    response, submit = _report_agent(client, body={"reason": "i-just-dont-like-it"})

    assert response.status_code == 422
    assert submit.await_count == 0


# ── one open report per reporter (D15.4) ─────────────────────────────────────────────
def test_a_replaced_report_says_so(client):
    """The UI has to be able to say "we updated your report" rather than imply a second."""
    response, _ = _report_agent(client, submitted=(_report(), True))

    assert response.json()["replacedExisting"] is True


def test_a_first_report_does_not_claim_to_have_replaced_anything(client):
    response, _ = _report_agent(client, submitted=(_report(), False))

    assert response.json()["replacedExisting"] is False


# ── ⚠️ a report is private, and never a ranking input ────────────────────────────────
def test_the_reporter_response_leaks_nothing_about_the_queue(client):
    """A report is a private message to the curator, so the reporter learns only that it
    landed. Queue position, other reports and every admin field would each be a way to
    read a surface the reporter has no business in.
    """
    response, _ = _report_agent(client)

    assert set(response.json()) == {
        "agentId",
        "reason",
        "state",
        "createdAt",
        "replacedExisting",
    }


def test_reporting_does_not_touch_the_listing_or_usage_count(client):
    """⚠️ The moment report volume influenced placement, reporting would become a way to
    bury a competitor's agent. Nothing on the write path may reach the listing.
    """
    with (
        patch(
            f"{REPORT_SERVICE}.get_assistant_with_access_check",
            AsyncMock(return_value=(_make_assistant(), "viewer")),
        ),
        patch(f"{REPORT_SERVICE}.submit_report", AsyncMock(return_value=(_report(), False))),
        patch(
            "apis.shared.assistants.listing_repository.write_listing", AsyncMock()
        ) as write_listing,
    ):
        client.post(f"/agents/{AGENT}/report", json={"reason": "inappropriate"})

    assert write_listing.await_count == 0


# ── the kill switch ──────────────────────────────────────────────────────────────────
def test_reporting_is_404_when_the_marketplace_is_off(client, monkeypatch):
    """404 rather than 403, so the surface reads as unmounted while the feature ships."""
    monkeypatch.setenv("AGENT_MARKETPLACE_ENABLED", "false")

    response, submit = _report_agent(client)

    assert response.status_code == 404
    assert submit.await_count == 0


def test_the_admin_queue_is_404_when_the_marketplace_is_off(client, monkeypatch):
    monkeypatch.setenv("AGENT_MARKETPLACE_ENABLED", "false")

    assert client.get("/admin/agents/reports").status_code == 404


# ── the admin queue (D10, D15.2) ─────────────────────────────────────────────────────
def _queue(client, reports):
    with (
        patch(
            f"{REPORT_SERVICE}.list_open_reports", AsyncMock(return_value=reports)
        ),
        patch(
            f"{REPORT_SERVICE}._get_assistant_cloud_without_ownership_check",
            AsyncMock(return_value=_make_assistant()),
        ),
    ):
        return client.get("/admin/agents/reports")


def test_the_queue_shows_the_reporter(client):
    """D15.2 — admins need identity to spot a brigade or a grudge."""
    response = _queue(client, [_report()])

    row = response.json()["reports"][0]
    assert row["reporterName"] == "Pat Reporter"
    assert row["reporterId"] == "user-001"
    assert row["note"] == "It errors out"


def test_the_queue_leads_with_severity(client):
    """`inappropriate` should page a human rather than wait behind a stale-link report."""
    response = _queue(
        client,
        [
            _report(reportId="rep-old", reason="other", createdAt="2026-07-01T00:00:00Z"),
            _report(reportId="rep-bad", reason="inappropriate", createdAt="2026-07-24T00:00:00Z"),
        ],
    )

    assert [r["reportId"] for r in response.json()["reports"]] == ["rep-bad", "rep-old"]


def test_the_open_count_badges_the_nav(client):
    response = _queue(client, [_report(reportId="a"), _report(reportId="b")])

    assert response.json()["openCount"] == 2


def test_a_report_whose_agent_is_gone_is_flagged_not_dropped(client):
    """A row that is dropped but still counted is one an admin can neither see nor clear."""
    with (
        patch(f"{REPORT_SERVICE}.list_open_reports", AsyncMock(return_value=[_report()])),
        patch(
            f"{REPORT_SERVICE}._get_assistant_cloud_without_ownership_check",
            AsyncMock(return_value=None),
        ),
    ):
        response = client.get("/admin/agents/reports")

    row = response.json()["reports"][0]
    assert row["agentMissing"] is True
    assert row["agentName"] == AGENT


def test_the_queue_requires_an_admin(app, make_user):
    """The reporter is visible here; there is no non-admin view of this surface."""
    mock_auth_user(app, make_user(user_id="user-001"))
    response = TestClient(app).get("/admin/agents/reports")

    assert response.status_code in (401, 403)


# ── triage never delists (D15.5) ─────────────────────────────────────────────────────
def _triage(client, decision="resolve", note="Asked the author for changes"):
    with (
        patch(
            f"{REPORT_SERVICE}.resolve_report",
            AsyncMock(
                return_value=_report(
                    state="resolved" if decision == "resolve" else "dismissed",
                    resolvedBy="admin@example.edu",
                    resolutionNote=note,
                )
            ),
        ) as resolve,
        patch(
            f"{REPORT_SERVICE}._get_assistant_cloud_without_ownership_check",
            AsyncMock(return_value=_make_assistant()),
        ),
        patch(
            "apis.app_api.agent_designer.services.listing_service.takedown_listing", AsyncMock()
        ) as takedown,
        patch(
            "apis.shared.assistants.listing_repository.write_listing", AsyncMock()
        ) as write_listing,
    ):
        response = client.post(
            f"/admin/agents/{AGENT}/reports/rep-abc/resolve",
            json={"decision": decision, "note": note},
        )
    return response, resolve, takedown, write_listing


@pytest.mark.parametrize("decision", ["resolve", "dismiss"])
def test_triage_records_the_decision(client, decision):
    response, resolve, _takedown, _write = _triage(client, decision=decision)

    assert response.status_code == 200
    assert resolve.await_args.kwargs["state"] == (
        "resolved" if decision == "resolve" else "dismissed"
    )


@pytest.mark.parametrize("decision", ["resolve", "dismiss"])
def test_triage_never_changes_the_listing(client, decision):
    """⚠️ D15.5. A report is a note *about* an agent, not a state *of* it.

    If this ever starts delisting, "Resolve" quietly becomes a takedown with no reason
    recorded for the author — and an admin tidying the queue would be removing agents.
    """
    _response, _resolve_mock, takedown, write_listing = _triage(client, decision=decision)

    assert takedown.await_count == 0
    assert write_listing.await_count == 0


def test_an_unknown_decision_is_refused(client):
    response = client.post(
        f"/admin/agents/{AGENT}/reports/rep-abc/resolve", json={"decision": "delist"}
    )

    assert response.status_code == 422


def test_resolving_a_missing_report_is_a_404(client):
    with patch(
        f"{REPORT_SERVICE}.resolve_report", AsyncMock(side_effect=ValueError("Report not found"))
    ):
        response = client.post(
            f"/admin/agents/{AGENT}/reports/nope/resolve", json={"decision": "resolve"}
        )

    assert response.status_code == 404


# ── the nav counts ───────────────────────────────────────────────────────────────────
def test_the_queue_counts_endpoint_returns_both(client):
    with (
        patch(
            "apis.app_api.admin.agents.routes.list_admin_listings", AsyncMock(return_value=([], 3))
        ),
        patch("apis.app_api.admin.agents.routes.open_report_count", AsyncMock(return_value=5)),
    ):
        response = client.get("/admin/agents/queues")

    assert response.json() == {"pendingCount": 3, "openReportCount": 5}


def test_one_unreachable_count_does_not_break_the_other(client):
    """A badge is orientation. An unreachable count shows zero rather than erroring the
    shell around a page that works.
    """
    with (
        patch(
            "apis.app_api.admin.agents.routes.list_admin_listings",
            AsyncMock(side_effect=RuntimeError("table gone")),
        ),
        patch("apis.app_api.admin.agents.routes.open_report_count", AsyncMock(return_value=5)),
    ):
        response = client.get("/admin/agents/queues")

    assert response.status_code == 200
    assert response.json() == {"pendingCount": 0, "openReportCount": 5}
