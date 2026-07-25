"""Agent Marketplace Phase 1 — the author's submit / withdraw routes (D2, D7, D12).

The load-bearing cases here are the D7 checks, because they are the only place the cost
of publishing is made visible to the person paying it: a memory-space binding blocks
submission outright, and skill exposure is enumerated before a reviewer's time is spent.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.agent_designer.routes import router
from apis.shared.assistants.models import AgentBinding, AgentListing, Assistant
from tests.routes.conftest import mock_auth_user

ROUTES_MODULE = "apis.app_api.agent_designer.routes"
SERVICE_MODULE = "apis.app_api.agent_designer.services.listing_service"


def _make_assistant(**overrides) -> Assistant:
    defaults = dict(
        assistantId="ast-001",
        ownerId="user-001",
        ownerName="Test User",
        name="Policy Lookup",
        description="Find and cite university policy",
        instructions="Answer from the policy manual.",
        vectorIndexId="idx-001",
        visibility="PRIVATE",
        usageCount=0,
        createdAt="2026-07-01T00:00:00Z",
        updatedAt="2026-07-01T00:00:00Z",
        status="COMPLETE",
    )
    defaults.update(overrides)
    return Assistant.model_validate(defaults)


@pytest.fixture
def app():
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture(autouse=True)
def _flags_on(monkeypatch):
    monkeypatch.setenv("AGENTS_API_ENABLED", "true")
    monkeypatch.setenv("AGENT_MARKETPLACE_ENABLED", "true")


@pytest.fixture
def _no_writes():
    """Stub the persistence + publisher resolution so tests exercise decisions, not I/O."""
    with patch(f"{SERVICE_MODULE}.write_listing", new_callable=AsyncMock) as write, patch(
        f"{SERVICE_MODULE}.ensure_individual_profile",
        new_callable=AsyncMock,
        return_value=SimpleNamespace(id="user-user-001"),
    ):
        yield write


def _owner(assistant):
    return patch(
        f"{SERVICE_MODULE}.resolve_assistant_permission",
        new_callable=AsyncMock,
        return_value=(assistant, "owner"),
    )


# ── the kill switch ──────────────────────────────────────────────────────────────────
class TestFeatureGate:
    def test_404_when_marketplace_flag_off(self, app, make_user, monkeypatch):
        monkeypatch.setenv("AGENT_MARKETPLACE_ENABLED", "false")
        mock_auth_user(app, make_user())
        resp = TestClient(app).post(
            "/agents/ast-001/listing/submit", json={"category": "Administration"}
        )
        assert resp.status_code == 404

    def test_404_when_the_agent_surface_itself_is_off(self, app, make_user, monkeypatch):
        """The marketplace is a surface over Agents; it cannot outlive them."""
        monkeypatch.setenv("AGENTS_API_ENABLED", "false")
        mock_auth_user(app, make_user())
        resp = TestClient(app).post(
            "/agents/ast-001/listing/submit", json={"category": "Administration"}
        )
        assert resp.status_code == 404


# ── D7.2 — memory spaces block submission ────────────────────────────────────────────
class TestMemorySpaceBlock:
    def test_memory_space_binding_blocks_submission_and_names_the_space(
        self, app, make_user, _no_writes
    ):
        """A memory space is personal data — a published agent bound to one fails for everyone."""
        assistant = _make_assistant(
            bindings=[AgentBinding(kind="memory_space", ref="mem-042", config={})]
        )
        space = SimpleNamespace(space_id="mem-042", name="Oliver", owner_id="user-001")

        mock_auth_user(app, make_user())
        with _owner(assistant), patch(f"{SERVICE_MODULE}.MemorySpaceService") as svc:
            svc.return_value.list_spaces_for_user.return_value = [(space, "owner")]
            resp = TestClient(app).post(
                "/agents/ast-001/listing/submit", json={"category": "Administration"}
            )

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "Oliver" in detail, "the message must name the space, not just its id"
        assert "memory space" in detail.lower()

    def test_block_falls_back_to_the_ref_when_the_name_cannot_be_resolved(
        self, app, make_user, _no_writes
    ):
        assistant = _make_assistant(
            bindings=[AgentBinding(kind="memory_space", ref="mem-042", config={})]
        )
        mock_auth_user(app, make_user())
        with _owner(assistant), patch(f"{SERVICE_MODULE}.MemorySpaceService") as svc:
            svc.return_value.list_spaces_for_user.side_effect = RuntimeError("boom")
            resp = TestClient(app).post(
                "/agents/ast-001/listing/submit", json={"category": "Administration"}
            )

        assert resp.status_code == 400
        assert "mem-042" in resp.json()["detail"]

    def test_blocked_submission_writes_nothing(self, app, make_user, _no_writes):
        """A blocked submission must not leave a half-built listing behind."""
        assistant = _make_assistant(
            bindings=[AgentBinding(kind="memory_space", ref="mem-042", config={})]
        )
        mock_auth_user(app, make_user())
        with _owner(assistant), patch(f"{SERVICE_MODULE}.MemorySpaceService") as svc:
            svc.return_value.list_spaces_for_user.return_value = []
            TestClient(app).post(
                "/agents/ast-001/listing/submit", json={"category": "Administration"}
            )

        _no_writes.assert_not_called()


# ── D7.1 — skill exposure is enumerated ──────────────────────────────────────────────
class TestSkillDisclosure:
    def test_submission_enumerates_the_authors_own_skills(self, app, make_user, _no_writes):
        """Publishing publishes the contents of every skill the author wrote and bound."""
        assistant = _make_assistant(
            bindings=[
                AgentBinding(kind="skill", ref="skill-a", config={}),
                AgentBinding(kind="skill", ref="skill-b", config={}),
            ]
        )
        skills = [
            SimpleNamespace(skill_id="skill-a", display_name="Policy Citation Format", owner_id="user-001"),
            SimpleNamespace(skill_id="skill-b", display_name="Someone Else's Skill", owner_id="user-999"),
        ]

        mock_auth_user(app, make_user())
        with _owner(assistant), patch(f"{SERVICE_MODULE}.skills_enabled", return_value=True), patch(
            f"{SERVICE_MODULE}.get_skill_catalog_repository"
        ) as repo:
            repo.return_value.batch_get_skills = AsyncMock(return_value=skills)
            resp = TestClient(app).post(
                "/agents/ast-001/listing/submit", json={"category": "Administration"}
            )

        assert resp.status_code == 200
        exposed = resp.json()["exposedSkills"]
        # Invoke-through resolves on skill.owner_id == agent.owner_id, so only the
        # author's own skills are theirs to disclose.
        assert [s["label"] for s in exposed] == ["Policy Citation Format"]

    def test_no_skill_bindings_discloses_nothing(self, app, make_user, _no_writes):
        mock_auth_user(app, make_user())
        with _owner(_make_assistant(bindings=[])):
            resp = TestClient(app).post(
                "/agents/ast-001/listing/submit", json={"category": "Administration"}
            )

        assert resp.status_code == 200
        assert resp.json()["exposedSkills"] == []


# ── submission mechanics ─────────────────────────────────────────────────────────────
class TestSubmit:
    def test_submission_moves_to_in_review_and_is_not_indexed(self, app, make_user, _no_writes):
        mock_auth_user(app, make_user())
        with _owner(_make_assistant(bindings=[])):
            resp = TestClient(app).post(
                "/agents/ast-001/listing/submit",
                json={"category": "Teaching", "note": "For the CTL team"},
            )

        assert resp.status_code == 200
        listing = resp.json()["listing"]
        assert listing["state"] == "in_review"
        assert listing["category"] == "Teaching"
        assert listing["reviewNote"] == "For the CTL team"

    def test_unknown_category_is_rejected(self, app, make_user, _no_writes):
        mock_auth_user(app, make_user())
        with _owner(_make_assistant(bindings=[])):
            resp = TestClient(app).post(
                "/agents/ast-001/listing/submit", json={"category": "Miscellaneous"}
            )

        assert resp.status_code == 400
        assert "Unknown category" in resp.json()["detail"]

    def test_defaults_to_the_authors_own_individual_publisher(self, app, make_user, _no_writes):
        """D12: an individual profile is auto-created from the display name on first submit."""
        mock_auth_user(app, make_user())
        with _owner(_make_assistant(bindings=[])):
            resp = TestClient(app).post(
                "/agents/ast-001/listing/submit", json={"category": "Research"}
            )

        assert resp.json()["listing"]["publisherId"] == "user-user-001"

    def test_editor_may_not_submit(self, app, make_user, _no_writes):
        """Putting the institution's name on an agent is the owner's act, not an editor's."""
        mock_auth_user(app, make_user())
        with patch(
            f"{SERVICE_MODULE}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=(_make_assistant(), "editor"),
        ):
            resp = TestClient(app).post(
                "/agents/ast-001/listing/submit", json={"category": "Research"}
            )

        assert resp.status_code == 403

    def test_missing_agent_is_404(self, app, make_user, _no_writes):
        mock_auth_user(app, make_user())
        with patch(
            f"{SERVICE_MODULE}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=(None, None),
        ):
            resp = TestClient(app).post(
                "/agents/ast-001/listing/submit", json={"category": "Research"}
            )

        assert resp.status_code == 404

    def test_resubmission_after_changes_requested_keeps_the_review_note(
        self, app, make_user, _no_writes
    ):
        """The author keeps the context they are acting on until a reviewer replaces it."""
        assistant = _make_assistant(
            bindings=[],
            listing=AgentListing(
                state="changes_requested",
                category="Teaching",
                publisherId="user-user-001",
                reviewNote="Please add a tagline.",
            ),
        )
        mock_auth_user(app, make_user())
        with _owner(assistant):
            resp = TestClient(app).post(
                "/agents/ast-001/listing/submit", json={"category": "Teaching"}
            )

        assert resp.status_code == 200
        assert resp.json()["listing"]["state"] == "in_review"
        assert resp.json()["listing"]["reviewNote"] == "Please add a tagline."

    def test_cannot_resubmit_an_agent_already_in_review(self, app, make_user, _no_writes):
        assistant = _make_assistant(
            bindings=[],
            listing=AgentListing(
                state="in_review", category="Teaching", publisherId="user-user-001"
            ),
        )
        mock_auth_user(app, make_user())
        with _owner(assistant):
            resp = TestClient(app).post(
                "/agents/ast-001/listing/submit", json={"category": "Teaching"}
            )

        assert resp.status_code == 400


# ── withdraw ─────────────────────────────────────────────────────────────────────────
class TestWithdraw:
    def test_owner_unpublishes_back_to_private(self, app, make_user, _no_writes):
        assistant = _make_assistant(
            listing=AgentListing(
                state="published", category="Teaching", publisherId="user-user-001"
            )
        )
        mock_auth_user(app, make_user())
        with _owner(assistant):
            resp = TestClient(app).delete("/agents/ast-001/listing")

        assert resp.status_code == 200
        assert resp.json()["state"] == "private"

    def test_withdrawing_an_unsubmitted_agent_is_404(self, app, make_user, _no_writes):
        mock_auth_user(app, make_user())
        with _owner(_make_assistant()):
            resp = TestClient(app).delete("/agents/ast-001/listing")

        assert resp.status_code == 404
