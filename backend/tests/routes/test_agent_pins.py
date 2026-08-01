"""Agent Marketplace Phase 5 — the pin routes and the store-front admin (D8, D9, D10).

The cases that matter here are the ones where a pin could become something it is not:

* a **grant** — a pin must never let a user reach an Agent they could not navigate to,
  so the read is access-checked on every request and the write refuses what it cannot see;
* a **fork** — the response is the shelf projection, carrying no ``instructions``;
* a **ranking input** — only published agents can be featured, and the order is the
  admin's array rather than anything derived.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.agents.routes import router as admin_router
from apis.app_api.agent_designer.routes import router as agents_router
from apis.shared.assistants.models import (
    AgentListing,
    AgentListingResponse,
    Assistant,
    PinnedAgentRef,
    UserPinState,
)
from apis.shared.auth import require_admin
from tests.conftest import override_admin_auth
from tests.routes.conftest import mock_auth_user

PIN_SERVICE = "apis.app_api.agent_designer.services.pin_service"
ADMIN_ROUTES = "apis.app_api.admin.agents.routes"


def _make_assistant(**overrides) -> Assistant:
    defaults = dict(
        assistantId="ast-001",
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
        listing=AgentListing(
            state="published", category="Administration", publisher_id="pub-registrar"
        ).model_dump(by_alias=True),
    )
    defaults.update(overrides)
    return Assistant.model_validate(defaults)


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
    override_admin_auth(app, lambda: make_user(user_id="admin-1"))
    return TestClient(app)


# ── GET /agents/pins ─────────────────────────────────────────────────────────────────
def test_pins_are_empty_for_a_user_who_has_never_pinned(client):
    with patch(f"{PIN_SERVICE}.get_pin_state", AsyncMock(return_value=UserPinState())):
        response = client.get("/agents/pins")

    assert response.status_code == 200
    assert response.json() == {"pins": []}


def test_a_pin_row_is_the_shelf_projection(client):
    state = UserPinState(pinned=[PinnedAgentRef(agent_id="ast-001", order=0, pinned_at="2026-07-25T00:00:00Z")])
    with (
        patch(f"{PIN_SERVICE}.get_pin_state", AsyncMock(return_value=state)),
        patch(f"{PIN_SERVICE}.list_publishers", AsyncMock(return_value=[])),
        patch(
            f"{PIN_SERVICE}.get_assistant_with_access_check",
            AsyncMock(return_value=(_make_assistant(), "viewer")),
        ),
    ):
        response = client.get("/agents/pins")

    assert response.status_code == 200
    row = response.json()["pins"][0]
    assert row["agentId"] == "ast-001"
    assert row["name"] == "Policy Lookup"
    assert row["source"] == "user"
    assert row["locked"] is False
    # A pin is a pointer, not a copy — the row carries nothing about behavior.
    for leaked in ("instructions", "description", "bindings", "ownerId"):
        assert leaked not in row


def test_pins_render_in_the_users_order(client):
    state = UserPinState(
        pinned=[
            PinnedAgentRef(agent_id="ast-002", order=1),
            PinnedAgentRef(agent_id="ast-001", order=0),
        ]
    )

    async def _resolve(agent_id, _user_id, _email=None):
        return _make_assistant(assistantId=agent_id, name=agent_id.upper()), "viewer"

    with (
        patch(f"{PIN_SERVICE}.get_pin_state", AsyncMock(return_value=state)),
        patch(f"{PIN_SERVICE}.list_publishers", AsyncMock(return_value=[])),
        patch(f"{PIN_SERVICE}.get_assistant_with_access_check", AsyncMock(side_effect=_resolve)),
    ):
        response = client.get("/agents/pins")

    assert [row["agentId"] for row in response.json()["pins"]] == ["ast-001", "ast-002"]


def test_an_unreachable_pin_is_omitted_but_not_deleted(client):
    """A pin is a bookmark, not a grant — and visibility changes are reversible."""
    state = UserPinState(pinned=[PinnedAgentRef(agent_id="ast-001", order=0)])
    remove = AsyncMock()

    with (
        patch(f"{PIN_SERVICE}.get_pin_state", AsyncMock(return_value=state)),
        patch(f"{PIN_SERVICE}.list_publishers", AsyncMock(return_value=[])),
        patch(
            f"{PIN_SERVICE}.get_assistant_with_access_check", AsyncMock(return_value=(None, None))
        ),
        patch(f"{PIN_SERVICE}.remove_pin", remove),
    ):
        response = client.get("/agents/pins")

    assert response.json() == {"pins": []}
    remove.assert_not_awaited()


def test_a_pinned_agent_survives_its_takedown(client):
    """D2: delisting is not revocation. A pin with no listing block still renders."""
    state = UserPinState(pinned=[PinnedAgentRef(agent_id="ast-001", order=0)])
    with (
        patch(f"{PIN_SERVICE}.get_pin_state", AsyncMock(return_value=state)),
        patch(f"{PIN_SERVICE}.list_publishers", AsyncMock(return_value=[])),
        patch(
            f"{PIN_SERVICE}.get_assistant_with_access_check",
            AsyncMock(return_value=(_make_assistant(listing=None), "viewer")),
        ),
    ):
        response = client.get("/agents/pins")

    row = response.json()["pins"][0]
    assert row["agentId"] == "ast-001"
    assert row["category"] == ""


# ── POST / DELETE /agents/{id}/pin ───────────────────────────────────────────────────
def test_pinning_returns_the_new_row(client):
    add = AsyncMock(
        return_value=UserPinState(pinned=[PinnedAgentRef(agent_id="ast-001", order=0)])
    )
    with (
        patch(
            f"{PIN_SERVICE}.get_assistant_with_access_check",
            AsyncMock(return_value=(_make_assistant(), "viewer")),
        ),
        patch(f"{PIN_SERVICE}.list_publishers", AsyncMock(return_value=[])),
        patch(f"{PIN_SERVICE}.add_pin", add),
    ):
        response = client.post("/agents/ast-001/pin")

    assert response.status_code == 201
    assert response.json()["agentId"] == "ast-001"
    add.assert_awaited_once_with("user-001", "ast-001")


def test_pinning_an_agent_you_cannot_reach_is_a_404(client):
    """Not-found and access-denied collapse: the store does not confirm ids exist."""
    add = AsyncMock()
    with (
        patch(
            f"{PIN_SERVICE}.get_assistant_with_access_check", AsyncMock(return_value=(None, None))
        ),
        patch(
            f"{PIN_SERVICE}.resolve_assistant_permission", AsyncMock(return_value=(None, None))
        ),
        patch(f"{PIN_SERVICE}.add_pin", add),
    ):
        response = client.post("/agents/ast-secret/pin")

    assert response.status_code == 404
    add.assert_not_awaited()


def test_pinning_a_published_agent_you_cannot_open_is_a_legible_403(client):
    """A tile the store already advertised is not a secret — say what went wrong.

    This is the shape that hit two users during the marketplace demo: a published agent
    whose visibility denies them. Publication now requires PUBLIC, so it should only be
    reachable via a listing narrowed after approval — the case no submit gate can catch.
    """
    unreachable = _make_assistant(
        visibility="SHARED",
        listing={"state": "published", "category": "Administration", "publisherId": "pub-1"},
    )
    add = AsyncMock()
    with (
        patch(
            f"{PIN_SERVICE}.get_assistant_with_access_check", AsyncMock(return_value=(None, None))
        ),
        patch(
            f"{PIN_SERVICE}.resolve_assistant_permission",
            AsyncMock(return_value=(unreachable, None)),
        ),
        patch(f"{PIN_SERVICE}.add_pin", add),
    ):
        response = client.post("/agents/ast-001/pin")

    assert response.status_code == 403
    assert "restricted who can" in response.json()["detail"]
    add.assert_not_awaited()


def test_an_unpublished_agent_still_collapses_to_404(client):
    """The disclosure rule only relaxes for ids the store itself handed out."""
    private = _make_assistant(visibility="PRIVATE", listing=None)
    with (
        patch(
            f"{PIN_SERVICE}.get_assistant_with_access_check", AsyncMock(return_value=(None, None))
        ),
        patch(
            f"{PIN_SERVICE}.resolve_assistant_permission",
            AsyncMock(return_value=(private, None)),
        ),
        patch(f"{PIN_SERVICE}.add_pin", AsyncMock()),
    ):
        response = client.post("/agents/ast-001/pin")

    assert response.status_code == 404


def test_a_failure_classifying_the_denial_falls_back_to_404(client):
    """The nicety must never escalate a clean 404 into a 500."""
    with (
        patch(
            f"{PIN_SERVICE}.get_assistant_with_access_check", AsyncMock(return_value=(None, None))
        ),
        patch(
            f"{PIN_SERVICE}.resolve_assistant_permission",
            AsyncMock(side_effect=RuntimeError("dynamo is having a day")),
        ),
        patch(f"{PIN_SERVICE}.add_pin", AsyncMock()),
    ):
        response = client.post("/agents/ast-001/pin")

    assert response.status_code == 404


def test_pinning_past_the_ceiling_is_a_409(client):
    from apis.shared.assistants.pins import PinLimitError

    with (
        patch(
            f"{PIN_SERVICE}.get_assistant_with_access_check",
            AsyncMock(return_value=(_make_assistant(), "viewer")),
        ),
        patch(f"{PIN_SERVICE}.add_pin", AsyncMock(side_effect=PinLimitError("Too many pins."))),
    ):
        response = client.post("/agents/ast-001/pin")

    assert response.status_code == 409


def test_unpinning_needs_no_existence_check(client):
    """Otherwise a user whose pinned agent was deleted cannot clear the row."""
    remove = AsyncMock(return_value=UserPinState(dismissed=["ast-gone"]))
    with patch(f"{PIN_SERVICE}.remove_pin", remove):
        response = client.delete("/agents/ast-gone/pin")

    assert response.status_code == 204
    remove.assert_awaited_once_with("user-001", "ast-gone")


# ── the kill switch (D14) ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "method,path",
    [("get", "/agents/pins"), ("post", "/agents/ast-001/pin"), ("delete", "/agents/ast-001/pin")],
)
def test_pin_routes_404_when_the_marketplace_is_off(client, monkeypatch, method, path):
    monkeypatch.setenv("AGENT_MARKETPLACE_ENABLED", "false")

    assert getattr(client, method)(path).status_code == 404


# ── admin store front (D10) ──────────────────────────────────────────────────────────
def _listing_row(agent_id: str, name: str) -> AgentListingResponse:
    return AgentListingResponse(
        agent_id=agent_id, name=name, category="Administration", tagline="One line"
    )


def test_admin_store_front_reports_ids_that_no_longer_publish(client):
    with (
        patch(f"{ADMIN_ROUTES}.get_featured_ids", AsyncMock(return_value=["ast-a", "ast-gone"])),
        patch(
            f"{ADMIN_ROUTES}.resolve_featured",
            AsyncMock(return_value=([_listing_row("ast-a", "Alpha")], ["ast-gone"])),
        ),
    ):
        response = client.get("/admin/agents/storefront")

    body = response.json()
    assert [row["agentId"] for row in body["featured"]] == ["ast-a"]
    assert body["unavailable"] == ["ast-gone"]


def test_saving_the_store_front_keeps_the_submitted_order(client):
    put = AsyncMock(side_effect=lambda ids, updated_by: ids)
    rows = [_listing_row("ast-b", "Beta"), _listing_row("ast-a", "Alpha")]
    with (
        patch(f"{ADMIN_ROUTES}.resolve_featured", AsyncMock(return_value=(rows, []))),
        patch(f"{ADMIN_ROUTES}.put_featured_ids", put),
    ):
        response = client.put("/admin/agents/storefront", json={"agentIds": ["ast-b", "ast-a"]})

    assert response.status_code == 200
    assert [row["agentId"] for row in response.json()["featured"]] == ["ast-b", "ast-a"]
    put.assert_awaited_once_with(["ast-b", "ast-a"], updated_by="admin-1")


def test_only_published_agents_can_be_featured(client):
    """A featured tile nobody can open is worse than a short row."""
    put = AsyncMock()
    with (
        patch(f"{ADMIN_ROUTES}.resolve_featured", AsyncMock(return_value=([], ["ast-draft"]))),
        patch(f"{ADMIN_ROUTES}.put_featured_ids", put),
    ):
        response = client.put("/admin/agents/storefront", json={"agentIds": ["ast-draft"]})

    assert response.status_code == 400
    assert "ast-draft" in response.json()["detail"]
    put.assert_not_awaited()


def test_the_featured_row_has_a_ceiling(client):
    from apis.shared.assistants.storefront import MAX_FEATURED

    put = AsyncMock()
    with patch(f"{ADMIN_ROUTES}.put_featured_ids", put):
        response = client.put(
            "/admin/agents/storefront",
            json={"agentIds": [f"ast-{index}" for index in range(MAX_FEATURED + 1)]},
        )

    assert response.status_code == 400
    put.assert_not_awaited()


def test_clearing_the_store_front_is_allowed(client):
    put = AsyncMock(return_value=[])
    with (
        patch(f"{ADMIN_ROUTES}.resolve_featured", AsyncMock(return_value=([], []))),
        patch(f"{ADMIN_ROUTES}.put_featured_ids", put),
    ):
        response = client.put("/admin/agents/storefront", json={"agentIds": []})

    assert response.status_code == 200
    put.assert_awaited_once()
