"""Agent Marketplace Phase 3 — the detail read, the instructions gate, and runnability.

The gate is a **behaviour change to an endpoint that already shipped**: ``GET /agents/{id}``
returned ``instructions`` to any PUBLIC viewer, which was tolerable while PUBLIC meant
"anyone with the link" and is not once a store puts the link in front of the institution.
These tests pin it per permission level, because the failure mode — a regression that
restores the field for viewers — is silent and invisible in the UI.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.agent_designer.routes import router
from apis.shared.assistants.models import (
    AgentBinding,
    AgentCapability,
    AgentModelConfig,
    AgentRunnabilityResponse,
    Assistant,
    MissingCapability,
)
from tests.routes.conftest import mock_auth_user

ROUTES_MODULE = "apis.app_api.agent_designer.routes"


def _make_assistant(**overrides) -> Assistant:
    defaults = dict(
        assistantId="ast-001",
        ownerId="user-author",
        ownerName="Ada Author",
        name="Policy Lookup",
        description="Finds and cites university policy",
        instructions="SECRET SYSTEM PROMPT",
        vectorIndexId="idx-001",
        visibility="PUBLIC",
        tags=["policy"],
        starters=["What is the travel reimbursement deadline?"],
        emoji="📋",
        usageCount=12,
        createdAt="2026-07-01T00:00:00Z",
        updatedAt="2026-07-20T00:00:00Z",
        status="COMPLETE",
        tagline="Find and cite university policy",
    )
    defaults.update(overrides)
    return Assistant.model_validate(defaults)


@pytest.fixture
def app():
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture
def _flags_on(monkeypatch):
    monkeypatch.setenv("AGENTS_API_ENABLED", "true")
    monkeypatch.setenv("AGENT_MARKETPLACE_ENABLED", "true")


def _get_agent(app, permission, *, capabilities=None, model_label=None, agent=None):
    """Drive ``GET /agents/{id}`` with the access check resolved to ``permission``."""
    with patch(f"{ROUTES_MODULE}.assistant_exists", new_callable=AsyncMock, return_value=True), patch(
        f"{ROUTES_MODULE}.get_assistant_with_access_check",
        new_callable=AsyncMock,
        return_value=(agent or _make_assistant(), permission),
    ), patch(
        f"{ROUTES_MODULE}.resolve_capabilities",
        new_callable=AsyncMock,
        return_value=(capabilities or [], model_label),
    ):
        return TestClient(app).get("/agents/ast-001")


# ── the instructions gate ────────────────────────────────────────────────────────────
class TestInstructionsGate:
    @pytest.mark.parametrize("permission", ["owner", "editor"])
    def test_owner_and_editor_still_read_instructions(self, app, make_user, _flags_on, permission):
        """The people who may *edit* the system prompt keep reading it — the Designer
        loads the Agent through this exact route to populate its form."""
        mock_auth_user(app, make_user())
        resp = _get_agent(app, permission)

        assert resp.status_code == 200
        assert resp.json()["instructions"] == "SECRET SYSTEM PROMPT"

    def test_a_viewer_does_not_receive_instructions(self, app, make_user, _flags_on):
        """The store case: a PUBLIC agent every browsing user can reach."""
        mock_auth_user(app, make_user())
        resp = _get_agent(app, "viewer")

        body = resp.json()
        assert resp.status_code == 200
        assert "instructions" not in body
        # The rest of the detail read is intact — this is a gate, not a truncation.
        assert body["description"] == "Finds and cites university policy"
        assert body["starters"] == ["What is the travel reimbursement deadline?"]
        assert body["updatedAt"] == "2026-07-20T00:00:00Z"

    def test_the_prompt_text_appears_nowhere_in_a_viewers_payload(self, app, make_user, _flags_on):
        """Guards the whole response, not one key: a future field that happens to echo
        the prompt (a summary, a preview) re-opens the exact hole this closed."""
        mock_auth_user(app, make_user())
        resp = _get_agent(app, "viewer")

        assert "SECRET SYSTEM PROMPT" not in resp.text

    def test_the_list_read_is_gated_the_same_way(self, app, make_user, _flags_on):
        """An agent shared read-only shows up in ``GET /agents`` too; one gate, both reads."""
        mock_auth_user(app, make_user())
        shared = _make_assistant()
        shared.user_permission = "viewer"
        with patch(
            f"{ROUTES_MODULE}.list_user_assistants", new_callable=AsyncMock, return_value=([], None)
        ), patch(f"{ROUTES_MODULE}.list_shared_with_user", new_callable=AsyncMock, return_value=[shared]):
            resp = TestClient(app).get("/agents")

        assert resp.status_code == 200
        assert "instructions" not in resp.json()["agents"][0]


# ── the detail shape ─────────────────────────────────────────────────────────────────
class TestDetailShape:
    def test_capabilities_and_model_label_are_layered_on(self, app, make_user, _flags_on):
        mock_auth_user(app, make_user())
        resp = _get_agent(
            app,
            "viewer",
            capabilities=[
                AgentCapability(label="Document Search", kind="tool"),
                AgentCapability(label="Policy Citation Format", kind="skill"),
            ],
            model_label="Claude Sonnet 4.5",
        )

        body = resp.json()
        assert body["capabilities"] == [
            {"label": "Document Search", "kind": "tool"},
            {"label": "Policy Citation Format", "kind": "skill"},
        ]
        assert body["modelLabel"] == "Claude Sonnet 4.5"

    def test_a_capability_lookup_failure_does_not_500_the_page(self, app, make_user, _flags_on):
        """Capabilities are presentation. A catalog hiccup degrades the panel; it must
        not turn a readable Agent into an error page."""
        mock_auth_user(app, make_user())
        with patch(f"{ROUTES_MODULE}.assistant_exists", new_callable=AsyncMock, return_value=True), patch(
            f"{ROUTES_MODULE}.get_assistant_with_access_check",
            new_callable=AsyncMock,
            return_value=(_make_assistant(), "viewer"),
        ), patch(
            f"{ROUTES_MODULE}.resolve_capabilities",
            new_callable=AsyncMock,
            side_effect=RuntimeError("catalog down"),
        ):
            resp = TestClient(app).get("/agents/ast-001")

        assert resp.status_code == 200
        assert "capabilities" not in resp.json()

    def test_the_publisher_renders_by_name_and_never_by_id(self, app, make_user, _flags_on):
        """D12: attribution is display-only, so the page gets label/kind/verified and the
        ``publisherId`` it was resolved from stays server-side."""
        from apis.shared.assistants.models import AgentListing, ListingPublisher

        mock_auth_user(app, make_user())
        listed = _make_assistant(
            listing=AgentListing(
                state="published", category="Administration", publisher_id="pub-registrar"
            )
        )
        with patch(f"{ROUTES_MODULE}.assistant_exists", new_callable=AsyncMock, return_value=True), patch(
            f"{ROUTES_MODULE}.get_assistant_with_access_check",
            new_callable=AsyncMock,
            return_value=(listed, "viewer"),
        ), patch(
            f"{ROUTES_MODULE}.resolve_capabilities", new_callable=AsyncMock, return_value=([], None)
        ), patch(
            f"{ROUTES_MODULE}.resolve_listing_display",
            new_callable=AsyncMock,
            return_value=(
                ListingPublisher(
                    label="Office of the Registrar", kind="department", verified=True
                ),
                "University Operations",
            ),
        ):
            resp = TestClient(app).get("/agents/ast-001")

        body = resp.json()
        assert body["publisher"] == {
            "label": "Office of the Registrar",
            "kind": "department",
            "verified": True,
        }
        assert body["categoryLabel"] == "University Operations"
        # The id is still on the listing block for owner-facing surfaces, but it is never
        # what the page renders — and it is not a permission.
        assert body["listing"]["publisherId"] == "pub-registrar"

    def test_an_unlisted_agent_carries_no_listing_block(self, app, make_user, _flags_on):
        """D3's backfill default: absent listing means never submitted, and stays absent."""
        mock_auth_user(app, make_user())
        resp = _get_agent(app, "owner")

        assert "listing" not in resp.json()


# ── runnability (D6) ─────────────────────────────────────────────────────────────────
class TestRunnabilityRoute:
    def _call(self, app, *, result=None, permission="viewer", exists=True, access=True):
        agent = _make_assistant(
            bindings=[AgentBinding(kind="tool", ref="workday_query")],
            model_settings=AgentModelConfig(model_id="claude-opus"),
        )
        with patch(
            f"{ROUTES_MODULE}.assistant_exists", new_callable=AsyncMock, return_value=exists
        ), patch(
            f"{ROUTES_MODULE}.get_assistant_with_access_check",
            new_callable=AsyncMock,
            return_value=(agent, permission) if access else (None, None),
        ), patch(
            f"{ROUTES_MODULE}.resolve_runnability",
            new_callable=AsyncMock,
            return_value=result
            or AgentRunnabilityResponse(agent_id="ast-001", state="ready", missing=[]),
        ):
            return TestClient(app).get("/agents/ast-001/runnability")

    def test_ready(self, app, make_user, _flags_on):
        mock_auth_user(app, make_user())
        resp = self._call(app)

        assert resp.status_code == 200
        assert resp.json() == {"agentId": "ast-001", "state": "ready", "missing": []}

    def test_blocked_names_what_is_missing(self, app, make_user, _flags_on):
        mock_auth_user(app, make_user())
        resp = self._call(
            app,
            result=AgentRunnabilityResponse(
                agent_id="ast-001",
                state="blocked",
                missing=[MissingCapability(label="Workday Query", kind="tool")],
            ),
        )

        body = resp.json()
        assert body["state"] == "blocked"
        assert body["missing"][0]["label"] == "Workday Query"

    def test_runnability_never_leaks_instructions_or_refs(self, app, make_user, _flags_on):
        """It answers one question. The Agent's prompt and its binding refs are not part
        of the answer, and the agent record is right there in the handler."""
        mock_auth_user(app, make_user())
        resp = self._call(app)

        assert "SECRET SYSTEM PROMPT" not in resp.text
        assert "workday_query" not in resp.text

    def test_404_for_an_agent_that_does_not_exist(self, app, make_user, _flags_on):
        mock_auth_user(app, make_user())
        assert self._call(app, exists=False).status_code == 404

    def test_403_when_the_caller_cannot_see_the_agent(self, app, make_user, _flags_on):
        """You may only ask about runnability for an Agent you can already reach."""
        mock_auth_user(app, make_user())
        assert self._call(app, access=False).status_code == 403

    def test_404_when_the_marketplace_kill_switch_is_off(self, app, make_user, monkeypatch):
        monkeypatch.setenv("AGENTS_API_ENABLED", "true")
        monkeypatch.setenv("AGENT_MARKETPLACE_ENABLED", "false")
        mock_auth_user(app, make_user())

        assert self._call(app).status_code == 404


# ── #744 — the drift baseline rides the instructions gate ────────────────────────────
class TestApprovedInstructionsHashIsGated:
    """A hash *of* the instructions is subject to the same gate as the instructions.

    It is not reversible on its own, but it would confirm a guessed prompt for anyone who
    could produce one — which is exactly what dropping ``instructions`` for a viewer
    exists to prevent.

    ⚠️ **The field is gone; this guard is not.** Version snapshots replaced drift detection
    and nothing writes ``approvedInstructionsHash`` any more — but ``AgentListing`` is
    ``extra="allow"``, so a listing approved *before* the removal still carries the
    attribute in storage and would round-trip through the read path untouched. The strip is
    transitional and these tests are what say so; both can go once no stored listing has it.
    """

    def _published(self):
        from apis.shared.assistants.models import AgentListing

        # Written the only way it can be now: as a leftover extra attribute on a listing
        # from before the field was removed.
        listing = AgentListing.model_validate(
            {
                "state": "published",
                "category": "Administration",
                "publisherId": "pub-registrar",
                "approvedInstructionsHash": "deadbeef",
            }
        )
        return _make_assistant(listing=listing)

    def test_a_viewer_never_sees_the_baseline(self, app, make_user, _flags_on):
        mock_auth_user(app, make_user())
        body = _get_agent(app, "viewer", agent=self._published()).json()

        # Guard premise: if this ever stops holding, the test below proves nothing.
        assert "instructions" not in body
        assert "approvedInstructionsHash" not in body["listing"]
        # A gate, not a truncation — the rest of the listing is public shelf data.
        assert body["listing"]["state"] == "published"
        assert body["listing"]["category"] == "Administration"

    def test_the_hash_appears_nowhere_in_a_viewers_payload(self, app, make_user, _flags_on):
        """Guards the whole response, not one key — the same shape as the prompt guard."""
        mock_auth_user(app, make_user())
        resp = _get_agent(app, "viewer", agent=self._published())

        assert "deadbeef" not in resp.text


# ── the detail read serves the approved snapshot (§4) ────────────────────────────────
class TestDetailReadServesTheSnapshot:
    """The store detail page must describe the Agent that will actually run.

    Before this, ``GET /agents/{id}`` served the live record to everyone. A published Agent
    whose author kept editing therefore had a store tile rendered from the approved snapshot
    and a detail page rendered from the unreviewed draft — different name, different summary,
    and ``capabilities`` resolved from the draft's bindings, so the page could advertise a
    tool the published version does not have. Invocation ran the snapshot regardless, which
    is what made it a lie rather than a preview.
    """

    def _published(self, **overrides):
        listing = {
            "state": "published",
            "category": "Administration",
            "publisherId": "pub-registrar",
            "submittedVersion": 1,
            "publishedVersion": 1,
        }
        listing.update(overrides.pop("listing", {}))
        return _make_assistant(listing=listing, **overrides)

    def _version(self):
        from apis.shared.assistants.models import AgentVersion

        return AgentVersion.model_validate(
            {
                "agentId": "ast-001",
                "version": 1,
                "name": "Approved Name",
                "description": "Approved summary",
                "instructions": "APPROVED PROMPT",
                "tagline": "Approved tagline",
                "bindings": [{"kind": "tool", "ref": "calculator"}],
                "category": "Administration",
                "publisherId": "pub-registrar",
            }
        )

    def _get(self, app, permission, version):
        with patch(
            f"{ROUTES_MODULE}.assistant_exists", new_callable=AsyncMock, return_value=True
        ), patch(
            f"{ROUTES_MODULE}.get_assistant_with_access_check",
            new_callable=AsyncMock,
            return_value=(self._published(name="DRAFT NAME"), permission),
        ), patch(
            "apis.shared.assistants.version_resolution.get_version",
            new_callable=AsyncMock,
            return_value=version,
        ), patch(
            f"{ROUTES_MODULE}.resolve_capabilities",
            new_callable=AsyncMock,
            return_value=([], None),
        ):
            return TestClient(app).get("/agents/ast-001")

    def test_a_viewer_sees_the_approved_snapshot_not_the_draft(self, app, make_user, _flags_on):
        mock_auth_user(app, make_user())
        resp = self._get(app, "viewer", self._version())

        body = resp.json()
        assert resp.status_code == 200
        assert body["name"] == "Approved Name"
        assert body["description"] == "Approved summary"
        assert body["tagline"] == "Approved tagline"
        # The snapshot's bindings, which is what `capabilities` is resolved from.
        assert body["bindings"] == [{"kind": "tool", "ref": "calculator", "config": {}}]

    @pytest.mark.parametrize("permission", ["owner", "editor"])
    def test_whoever_may_edit_still_gets_the_draft(self, app, make_user, _flags_on, permission):
        """⚠️ Editors too, unlike invocation.

        The Agent Designer loads its form from this endpoint. Serving an editor the approved
        snapshot would populate the form with someone else's published copy, and their next
        save would silently revert the owner's draft. Invocation is owner-only for the
        opposite reason — an editor *running* the draft would bypass review.
        """
        mock_auth_user(app, make_user())
        resp = self._get(app, permission, self._version())

        body = resp.json()
        assert resp.status_code == 200
        assert body["name"] == "DRAFT NAME"
        assert body["instructions"] == "SECRET SYSTEM PROMPT"

    def test_an_unpublished_agent_is_unaffected(self, app, make_user, _flags_on):
        """The common case, and the one that must not change."""
        mock_auth_user(app, make_user())
        with patch(
            f"{ROUTES_MODULE}.assistant_exists", new_callable=AsyncMock, return_value=True
        ), patch(
            f"{ROUTES_MODULE}.get_assistant_with_access_check",
            new_callable=AsyncMock,
            return_value=(_make_assistant(name="DRAFT NAME"), "viewer"),
        ), patch(
            f"{ROUTES_MODULE}.resolve_capabilities",
            new_callable=AsyncMock,
            return_value=([], None),
        ):
            resp = TestClient(app).get("/agents/ast-001")

        assert resp.status_code == 200
        assert resp.json()["name"] == "DRAFT NAME"

    def test_a_missing_snapshot_refuses_rather_than_falling_back(self, app, make_user, _flags_on):
        """Same refusal as invocation, for the same reason.

        Degrading to the draft would show unreviewed content precisely when something is
        already wrong — and such an Agent has no store tile either, since the index is
        sparse on the version row, so this is only reachable by direct link.
        """
        mock_auth_user(app, make_user())
        resp = self._get(app, "viewer", None)

        assert resp.status_code == 503
        assert "published version could not be loaded" in resp.json()["detail"]
