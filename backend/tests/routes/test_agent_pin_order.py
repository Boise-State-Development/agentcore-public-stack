"""``PUT /agents/pins/order`` — the user's arranged pin order (agent-project-organization D7).

The rules a reorder must keep: it writes only Agents on the caller's effective shelf, a
locked role pin keeps its place whatever the body says, an unlocked seed the user arranges
becomes their own pin, and once arranged the shelf follows the user's order rather than a
role's priority. A seed that arrives afterwards sits ahead of the arranged rows, as new.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.agent_designer.routes import router as agents_router
from apis.shared.assistants.models import PinnedAgentRef, UserPinState
from tests.routes.conftest import mock_auth_user
from tests.routes.test_role_agent_pins import PIN_SERVICE, _make_assistant, _role, _role_service, _seed

PINS = "apis.shared.assistants.pins"


@pytest.fixture(autouse=True)
def _flags_on(monkeypatch):
    monkeypatch.setenv("AGENTS_API_ENABLED", "true")
    monkeypatch.setenv("AGENT_MARKETPLACE_ENABLED", "true")


@pytest.fixture
def client(make_user):
    app = FastAPI()
    app.include_router(agents_router)
    mock_auth_user(app, make_user(user_id="user-001", email="pat@example.edu"))
    return TestClient(app)


class _Store:
    """One user's pin item, in memory, shared by the read and write paths."""

    def __init__(self, state: UserPinState):
        self.state = state
        self.writes = 0

    async def get(self, _user_id):
        return self.state.model_copy(deep=True)

    async def put(self, _user_id, state):
        self.writes += 1
        self.state = state.model_copy(deep=True)
        return state


def _own(*agent_ids: str) -> list:
    return [PinnedAgentRef(agent_id=agent_id, order=i, pinned_at="2026-11-01T00:00:00Z") for i, agent_id in enumerate(agent_ids)]


def _call(client, store, *, method="get", body=None, seeds=None, reachable=None):
    async def _resolve(agent_id, _user_id, _email=None):
        if reachable is not None and agent_id not in reachable:
            return None, None
        return _make_assistant(assistantId=agent_id, name=agent_id), "viewer"

    with (
        patch(f"{PIN_SERVICE}.get_pin_state", AsyncMock(side_effect=store.get)),
        patch(f"{PINS}.get_pin_state", AsyncMock(side_effect=store.get)),
        patch(f"{PINS}._put_pin_state", AsyncMock(side_effect=store.put)),
        patch(f"{PIN_SERVICE}.list_publishers", AsyncMock(return_value=[])),
        patch(f"{PIN_SERVICE}.get_assistant_with_access_check", AsyncMock(side_effect=_resolve)),
        patch(f"{PIN_SERVICE}.get_app_role_service", lambda: _role_service([_role("faculty")])),
        patch(f"{PIN_SERVICE}.list_pins_for_roles", AsyncMock(return_value=seeds or {})),
    ):
        if method == "put":
            return client.put("/agents/pins/order", json=body)
        return client.get("/agents/pins")


def _ids(response) -> list:
    assert response.status_code == 200, response.text
    return [row["agentId"] for row in response.json()["pins"]]


def test_arranges_own_pins_and_the_shelf_follows(client):
    store = _Store(UserPinState(pinned=_own("ast-a", "ast-b", "ast-c")))
    put = _call(client, store, method="put", body={"agentIds": ["ast-c", "ast-a", "ast-b"]})
    assert _ids(put) == ["ast-c", "ast-a", "ast-b"]
    assert store.state.ordered is True
    assert _ids(_call(client, store)) == ["ast-c", "ast-a", "ast-b"]


def test_a_locked_seed_keeps_its_place_whatever_the_body_says(client):
    store = _Store(UserPinState(pinned=_own("ast-a", "ast-b")))
    seeds = {"faculty": [_seed("ast-locked", locked=True)]}
    put = _call(client, store, method="put", body={"agentIds": ["ast-b", "ast-locked", "ast-a"]}, seeds=seeds)
    assert _ids(put) == ["ast-locked", "ast-b", "ast-a"]
    assert "ast-locked" not in {ref.agent_id for ref in store.state.pinned}


def test_an_arranged_seed_becomes_your_own_pin_and_follows_your_order(client):
    store = _Store(UserPinState(pinned=_own("ast-a"), dismissed=["ast-seeded"]))
    seeds = {"faculty": [_seed("ast-seeded"), _seed("ast-other", order=1)]}
    # Before arranging, the undismissed seed sits ahead of the user's own pin.
    assert _ids(_call(client, store, seeds=seeds)) == ["ast-other", "ast-a"]

    put = _call(client, store, method="put", body={"agentIds": ["ast-a", "ast-other"]}, seeds=seeds)
    assert _ids(put) == ["ast-a", "ast-other"]
    rows = {row["agentId"]: row for row in put.json()["pins"]}
    assert rows["ast-other"]["source"] == "user"
    assert "ast-other" in {ref.agent_id for ref in store.state.pinned}


def test_writes_only_what_is_on_the_shelf(client):
    store = _Store(UserPinState(pinned=_own("ast-a", "ast-gone")))
    put = _call(
        client,
        store,
        method="put",
        body={"agentIds": ["ast-stranger", "ast-a"]},
        reachable={"ast-a"},
    )
    assert _ids(put) == ["ast-a"]
    stored = [ref.agent_id for ref in sorted(store.state.pinned, key=lambda ref: ref.order)]
    # The unreachable own pin is kept after the arranged ones; the stranger is never pinned.
    assert stored == ["ast-a", "ast-gone"]


def test_a_seed_that_arrives_later_sits_ahead_as_new(client):
    store = _Store(UserPinState(pinned=_own("ast-a", "ast-b")))
    _call(client, store, method="put", body={"agentIds": ["ast-b", "ast-a"]})
    seeds = {"faculty": [_seed("ast-new")]}
    assert _ids(_call(client, store, seeds=seeds)) == ["ast-new", "ast-b", "ast-a"]


def test_an_unarranged_shelf_sorts_exactly_as_before(client):
    """Without the ``ordered`` mark a user who also pinned a seed keeps it among the seeds."""
    store = _Store(UserPinState(pinned=_own("ast-a", "ast-seeded")))
    seeds = {"faculty": [_seed("ast-seeded")]}
    assert _ids(_call(client, store, seeds=seeds)) == ["ast-seeded", "ast-a"]


@pytest.mark.parametrize("body", [{"agentIds": ["has space"]}, {"agentIds": ["a"] * 101}, {}])
def test_invalid_bodies_are_422(client, body):
    store = _Store(UserPinState())
    assert _call(client, store, method="put", body=body).status_code == 422
    assert store.writes == 0


def test_404_while_agents_are_off(client, monkeypatch):
    monkeypatch.setenv("AGENTS_API_ENABLED", "false")
    store = _Store(UserPinState())
    assert _call(client, store, method="put", body={"agentIds": []}).status_code == 404
