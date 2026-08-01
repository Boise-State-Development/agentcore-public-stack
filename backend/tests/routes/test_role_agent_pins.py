"""Agent Marketplace Phase 6 — default pins by role (D9).

Two surfaces, one rule each that the feature turns on:

* **The user's shelf** — ``(⋃ role pins) − dismissed(unlocked only) ∪ own pins``, resolved
  live. Every case below is a way that formula can quietly stop holding: a dismissal that
  the resolver forgets (the seed comes back and can never be removed), a lock that a
  dismissal defeats, a role pin that outranks the user's own conversion of it, or a seed
  that reaches someone who could not otherwise open the Agent — which would make a pin a
  grant.

* **The admin console** — the D9.5 assignment-time diff, and the two labels that keep an
  admin from seeding nobody: ``default`` reaches only users who matched *zero* roles, and
  a role with no JWT mappings matches nobody at all.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.roles.agent_pins import router as role_pins_router
from apis.app_api.agent_designer.routes import router as agents_router
from apis.shared.assistants.models import (
    AgentListing,
    Assistant,
    PinnedAgentRef,
    RoleAgentPin,
    UserPinState,
)
from apis.shared.auth import require_admin
from tests.conftest import override_admin_auth
from apis.shared.rbac.models import AppRole, EffectivePermissions, UserEffectivePermissions
from tests.routes.conftest import mock_auth_user

PIN_SERVICE = "apis.app_api.agent_designer.services.pin_service"
ROLE_PIN_SERVICE = "apis.app_api.agent_designer.services.role_pin_service"
ADMIN_ROUTES = "apis.app_api.admin.roles.agent_pins"


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


def _role(role_id: str, priority: int = 0, **overrides) -> AppRole:
    return AppRole(
        role_id=role_id,
        display_name=overrides.pop("display_name", role_id.title()),
        description="",
        jwt_role_mappings=overrides.pop("jwt_role_mappings", [role_id.title()]),
        priority=priority,
        effective_permissions=overrides.pop(
            "effective_permissions", EffectivePermissions(tools=["*"], models=["*"], skills=["*"])
        ),
        **overrides,
    )


def _seed(agent_id: str, order: int = 0, locked: bool = False) -> RoleAgentPin:
    return RoleAgentPin(
        agent_id=agent_id, order=order, locked=locked, created_at="2026-07-20T00:00:00Z"
    )


def _role_service(roles):
    """A stand-in AppRoleService that reports ``roles`` as the caller's matched roles."""
    service = MagicMock()
    service.resolve_user_permissions = AsyncMock(
        return_value=UserEffectivePermissions(
            user_id="user-001",
            app_roles=[role.role_id for role in roles],
            tools=[],
            models=[],
            quota_tier=None,
            resolved_at="2026-07-25T00:00:00Z",
        )
    )
    by_id = {role.role_id: role for role in roles}
    service.get_role = AsyncMock(side_effect=lambda role_id: by_id.get(role_id))
    return service


def _shelf(client, *, state=None, seeds=None, roles=None, assistants=None):
    """Call ``GET /agents/pins`` with the role side stubbed at the service boundary."""
    roles = roles if roles is not None else [_role("faculty")]

    async def _resolve(agent_id, _user_id, _email=None):
        if assistants is not None and agent_id not in assistants:
            return None, None
        override = (assistants or {}).get(agent_id, {})
        return _make_assistant(assistantId=agent_id, **override), "viewer"

    with (
        patch(f"{PIN_SERVICE}.get_pin_state", AsyncMock(return_value=state or UserPinState())),
        patch(f"{PIN_SERVICE}.list_publishers", AsyncMock(return_value=[])),
        patch(f"{PIN_SERVICE}.get_assistant_with_access_check", AsyncMock(side_effect=_resolve)),
        patch(f"{PIN_SERVICE}.get_app_role_service", lambda: _role_service(roles)),
        patch(f"{PIN_SERVICE}.list_pins_for_roles", AsyncMock(return_value=seeds or {})),
    ):
        return client.get("/agents/pins")


@pytest.fixture
def app():
    _app = FastAPI()
    _app.include_router(agents_router)
    _app.include_router(role_pins_router, prefix="/admin")
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


# ── the resolver: (⋃ role pins) − dismissed(unlocked only) ∪ own pins ────────────────
def test_a_role_seed_lands_on_the_shelf(client):
    response = _shelf(client, seeds={"faculty": [_seed("ast-seeded")]})

    assert response.status_code == 200
    row = response.json()["pins"][0]
    assert row["agentId"] == "ast-seeded"
    assert row["source"] == "role"
    assert row["locked"] is False
    # Still the shelf projection — a seed is a pin, not a richer object.
    for leaked in ("instructions", "description", "bindings", "ownerId"):
        assert leaked not in row


def test_a_dismissed_seed_stays_gone(client):
    """The tombstone is the whole reason live resolution is safe (D9.3)."""
    response = _shelf(
        client,
        state=UserPinState(dismissed=["ast-seeded"]),
        seeds={"faculty": [_seed("ast-seeded")]},
    )

    assert response.json() == {"pins": []}


def test_a_locked_seed_ignores_the_dismissal(client):
    """D9.4: locked means the role's members keep it, tombstone or not."""
    response = _shelf(
        client,
        state=UserPinState(dismissed=["ast-locked"]),
        seeds={"faculty": [_seed("ast-locked", locked=True)]},
    )

    row = response.json()["pins"][0]
    assert row["agentId"] == "ast-locked"
    assert row["locked"] is True


def test_pinning_a_seeded_agent_converts_it_to_your_own(client):
    """The D9.1 escape hatch: this is what survives the role pin being removed."""
    response = _shelf(
        client,
        state=UserPinState(pinned=[PinnedAgentRef(agent_id="ast-seeded", order=0)]),
        seeds={"faculty": [_seed("ast-seeded")]},
    )

    rows = response.json()["pins"]
    assert len(rows) == 1
    assert rows[0]["source"] == "user"


def test_a_lock_survives_the_user_pinning_it_themselves(client):
    """Owning a copy of the pin is not a contradiction of the role's lock."""
    response = _shelf(
        client,
        state=UserPinState(pinned=[PinnedAgentRef(agent_id="ast-locked", order=0)]),
        seeds={"faculty": [_seed("ast-locked", locked=True)]},
    )

    assert response.json()["pins"][0]["locked"] is True


def test_pins_from_several_roles_merge(client):
    """D9.2: the union, with no precedence rules."""
    response = _shelf(
        client,
        seeds={"faculty": [_seed("ast-a")], "staff": [_seed("ast-b")]},
        roles=[_role("faculty"), _role("staff")],
    )

    assert {row["agentId"] for row in response.json()["pins"]} == {"ast-a", "ast-b"}


def test_a_lock_from_any_role_wins(client):
    """The strictest claim is the honest one — otherwise a lock is a coin toss."""
    response = _shelf(
        client,
        seeds={"faculty": [_seed("ast-a")], "staff": [_seed("ast-a", locked=True)]},
        roles=[_role("faculty"), _role("staff")],
    )

    rows = response.json()["pins"]
    assert len(rows) == 1
    assert rows[0]["locked"] is True


def test_shelf_order_is_locked_then_priority_then_own(client):
    response = _shelf(
        client,
        state=UserPinState(pinned=[PinnedAgentRef(agent_id="ast-own", order=0)]),
        seeds={
            "faculty": [_seed("ast-high")],
            "staff": [_seed("ast-low"), _seed("ast-locked", order=1, locked=True)],
        },
        roles=[_role("faculty", priority=50), _role("staff", priority=10)],
    )

    assert [row["agentId"] for row in response.json()["pins"]] == [
        "ast-locked",
        "ast-high",
        "ast-low",
        "ast-own",
    ]


def test_a_seed_the_user_cannot_reach_is_not_a_grant(client):
    """The access check runs on every row, seeded or not."""
    response = _shelf(
        client,
        seeds={"faculty": [_seed("ast-private"), _seed("ast-open", order=1)]},
        assistants={"ast-open": {}},
    )

    assert [row["agentId"] for row in response.json()["pins"]] == ["ast-open"]


def test_the_shelf_still_renders_when_roles_cannot_be_resolved(client):
    """A pin list is an enhancement to three surfaces; it must degrade, not fail."""
    service = MagicMock()
    service.resolve_user_permissions = AsyncMock(side_effect=RuntimeError("dynamo down"))

    with (
        patch(
            f"{PIN_SERVICE}.get_pin_state",
            AsyncMock(return_value=UserPinState(pinned=[PinnedAgentRef(agent_id="ast-own")])),
        ),
        patch(f"{PIN_SERVICE}.list_publishers", AsyncMock(return_value=[])),
        patch(
            f"{PIN_SERVICE}.get_assistant_with_access_check",
            AsyncMock(return_value=(_make_assistant(assistantId="ast-own"), "viewer")),
        ),
        patch(f"{PIN_SERVICE}.get_app_role_service", lambda: service),
    ):
        response = client.get("/agents/pins")

    assert [row["agentId"] for row in response.json()["pins"]] == ["ast-own"]


# ── POST /agents/{id}/pin against a seed ─────────────────────────────────────────────
def test_pinning_something_your_role_locked_returns_a_locked_row(client):
    """The SPA splices this row into its list — a `locked: false` here offers a remove
    control that the next full read would take away."""
    with (
        patch(
            f"{PIN_SERVICE}.get_assistant_with_access_check",
            AsyncMock(return_value=(_make_assistant(assistantId="ast-locked"), "viewer")),
        ),
        patch(f"{PIN_SERVICE}.list_publishers", AsyncMock(return_value=[])),
        patch(
            f"{PIN_SERVICE}.add_pin",
            AsyncMock(return_value=UserPinState(pinned=[PinnedAgentRef(agent_id="ast-locked")])),
        ),
        patch(f"{PIN_SERVICE}.get_pin_state", AsyncMock(return_value=UserPinState())),
        patch(f"{PIN_SERVICE}.get_app_role_service", lambda: _role_service([_role("faculty")])),
        patch(
            f"{PIN_SERVICE}.list_pins_for_roles",
            AsyncMock(return_value={"faculty": [_seed("ast-locked", locked=True)]}),
        ),
    ):
        response = client.post("/agents/ast-locked/pin")

    assert response.status_code == 201
    body = response.json()
    assert body["source"] == "user"
    assert body["locked"] is True


# ── DELETE /agents/{id}/pin against a locked seed ────────────────────────────────────
def test_unpinning_a_locked_seed_no_ops(client):
    """D9.4. Writing the tombstone anyway would arm a dismissal for the day it unlocks."""
    remove = AsyncMock()
    with (
        patch(f"{PIN_SERVICE}.get_pin_state", AsyncMock(return_value=UserPinState())),
        patch(f"{PIN_SERVICE}.get_app_role_service", lambda: _role_service([_role("faculty")])),
        patch(
            f"{PIN_SERVICE}.list_pins_for_roles",
            AsyncMock(return_value={"faculty": [_seed("ast-locked", locked=True)]}),
        ),
        patch(f"{PIN_SERVICE}.remove_pin", remove),
    ):
        response = client.delete("/agents/ast-locked/pin")

    assert response.status_code == 204
    remove.assert_not_awaited()


def test_unpinning_an_unlocked_seed_tombstones_it(client):
    remove = AsyncMock()
    with (
        patch(f"{PIN_SERVICE}.get_pin_state", AsyncMock(return_value=UserPinState())),
        patch(f"{PIN_SERVICE}.get_app_role_service", lambda: _role_service([_role("faculty")])),
        patch(
            f"{PIN_SERVICE}.list_pins_for_roles",
            AsyncMock(return_value={"faculty": [_seed("ast-seeded")]}),
        ),
        patch(f"{PIN_SERVICE}.remove_pin", remove),
    ):
        response = client.delete("/agents/ast-seeded/pin")

    assert response.status_code == 204
    remove.assert_awaited_once_with("user-001", "ast-seeded")


# ── the admin console ────────────────────────────────────────────────────────────────
def _admin_get(client, role, pins, agents):
    with (
        patch(f"{ADMIN_ROUTES}.get_app_role_admin_service", lambda: _admin_service(role)),
        patch(f"{ADMIN_ROUTES}.list_role_pins", AsyncMock(return_value=pins)),
        patch(f"{ROLE_PIN_SERVICE}.batch_get_agents", AsyncMock(return_value=agents)),
        patch(f"{ROLE_PIN_SERVICE}.list_publishers", AsyncMock(return_value=[])),
    ):
        return client.get(f"/admin/roles/{role.role_id if role else 'nope'}/agent-pins")


def _admin_service(role):
    service = MagicMock()
    service.get_role = AsyncMock(return_value=role)
    return service


def test_an_unknown_role_is_a_404(client):
    with patch(f"{ADMIN_ROUTES}.get_app_role_admin_service", lambda: _admin_service(None)):
        response = client.get("/admin/roles/ghost/agent-pins")

    assert response.status_code == 404


def test_the_default_role_is_labelled_as_a_substitute(client):
    """⚠️ D9.6 — seeding ``default`` reaches only users who matched zero roles."""
    response = _admin_get(client, _role("default", jwt_role_mappings=[]), [], {})

    body = response.json()
    assert body["fallbackOnly"] is True
    assert body["unmapped"] is True


def test_a_mapped_role_is_not_labelled_as_a_substitute(client):
    response = _admin_get(client, _role("faculty"), [], {})

    body = response.json()
    assert body["fallbackOnly"] is False
    assert body["unmapped"] is False


def test_a_row_names_what_the_role_cannot_reach(client):
    """D9.5: seeding 410 researchers an Agent that fails on their first message."""
    agent = _make_assistant(
        assistantId="ast-001",
        modelConfig={"modelId": "anthropic.claude-opus-5"},
        bindings=[{"kind": "tool", "ref": "web_search", "config": {}}],
    ).model_dump(by_alias=True)
    role = _role(
        "student",
        effective_permissions=EffectivePermissions(tools=[], models=[], skills=[]),
    )

    with patch(f"{ROLE_PIN_SERVICE}._model_label", AsyncMock(return_value="Claude Opus 5")):
        response = _admin_get(client, role, [_seed("ast-001")], {"ast-001": agent})

    row = response.json()["pins"][0]
    assert row["state"] == "blocked"
    assert {item["kind"] for item in row["missing"]} == {"model", "tool"}
    # Never the raw ref — a capability label is rendered content.
    assert any(item["label"] == "Claude Opus 5" for item in row["missing"])


def test_a_role_that_grants_everything_reads_as_ready(client):
    agent = _make_assistant(
        assistantId="ast-001",
        modelConfig={"modelId": "anthropic.claude-opus-5"},
        bindings=[{"kind": "tool", "ref": "web_search", "config": {}}],
    ).model_dump(by_alias=True)

    response = _admin_get(client, _role("faculty"), [_seed("ast-001")], {"ast-001": agent})

    row = response.json()["pins"][0]
    assert row["state"] == "ready"
    assert row["missing"] == []


def test_a_memory_space_binding_is_a_note_not_a_verdict(client):
    """A role does not grant memory spaces, so 'ready' must not claim to have checked one."""
    agent = _make_assistant(
        assistantId="ast-001",
        bindings=[{"kind": "memory_space", "ref": "space-1", "config": {}}],
    ).model_dump(by_alias=True)

    response = _admin_get(client, _role("faculty"), [_seed("ast-001")], {"ast-001": agent})

    row = response.json()["pins"][0]
    assert row["notes"]
    assert all(item["kind"] != "memory_space" for item in row["missing"])


def test_a_private_agent_is_flagged_as_unreachable(client):
    """The seed would resolve to nothing for every member: the read access-checks each row."""
    agent = _make_assistant(assistantId="ast-001", visibility="PRIVATE").model_dump(by_alias=True)

    response = _admin_get(client, _role("faculty"), [_seed("ast-001")], {"ast-001": agent})

    row = response.json()["pins"][0]
    assert row["reachable"] is False
    assert row["visibility"] == "PRIVATE"


def test_a_deleted_agent_is_reported_rather_than_pruned(client):
    """A GET that rewrote the seed list would disguise an accidental delete as an edit."""
    response = _admin_get(client, _role("faculty"), [_seed("ast-gone")], {})

    body = response.json()
    assert body["pins"] == []
    assert body["unavailable"] == ["ast-gone"]


def test_saving_replaces_the_list_in_order(client):
    save = AsyncMock()
    with (
        patch(f"{ADMIN_ROUTES}.get_app_role_admin_service", lambda: _admin_service(_role("faculty"))),
        patch(f"{ADMIN_ROUTES}.put_role_pins", save),
        patch(f"{ADMIN_ROUTES}.list_role_pins", AsyncMock(return_value=[])),
        patch(f"{ROLE_PIN_SERVICE}.batch_get_agents", AsyncMock(return_value={})),
        patch(f"{ROLE_PIN_SERVICE}.list_publishers", AsyncMock(return_value=[])),
    ):
        response = client.put(
            "/admin/roles/faculty/agent-pins",
            json={"pins": [{"agentId": "ast-002"}, {"agentId": "ast-001", "locked": True}]},
        )

    assert response.status_code == 200
    saved = save.await_args.args[1]
    assert [pin.agent_id for pin in saved] == ["ast-002", "ast-001"]
    assert [pin.locked for pin in saved] == [False, True]


def test_a_refused_list_is_a_400_with_the_reason(client):
    with (
        patch(f"{ADMIN_ROUTES}.get_app_role_admin_service", lambda: _admin_service(_role("faculty"))),
        patch(
            f"{ADMIN_ROUTES}.put_role_pins",
            AsyncMock(side_effect=ValueError("The same agent cannot be pinned to a role twice.")),
        ),
    ):
        response = client.put(
            "/admin/roles/faculty/agent-pins",
            json={"pins": [{"agentId": "ast-001"}, {"agentId": "ast-001"}]},
        )

    assert response.status_code == 400
    assert "twice" in response.json()["detail"]


def test_a_warning_does_not_block_the_save(client):
    """An admin may seed something whose author is about to publish it."""
    agent = _make_assistant(assistantId="ast-001", visibility="PRIVATE").model_dump(by_alias=True)
    save = AsyncMock()

    with (
        patch(f"{ADMIN_ROUTES}.get_app_role_admin_service", lambda: _admin_service(_role("faculty"))),
        patch(f"{ADMIN_ROUTES}.put_role_pins", save),
        patch(f"{ADMIN_ROUTES}.list_role_pins", AsyncMock(return_value=[_seed("ast-001")])),
        patch(f"{ROLE_PIN_SERVICE}.batch_get_agents", AsyncMock(return_value={"ast-001": agent})),
        patch(f"{ROLE_PIN_SERVICE}.list_publishers", AsyncMock(return_value=[])),
    ):
        response = client.put(
            "/admin/roles/faculty/agent-pins", json={"pins": [{"agentId": "ast-001"}]}
        )

    assert response.status_code == 200
    save.assert_awaited_once()
    assert response.json()["pins"][0]["reachable"] is False
