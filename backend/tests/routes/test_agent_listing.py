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
        # PUBLIC by default because publication now requires it: the marketplace is
        # public-only, so an agent that cannot be published is the special case, not the
        # baseline. Tests that exercise the block pass ``visibility=`` explicitly.
        visibility="PUBLIC",
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


def _default_categories():
    """The seeded category set, as ``ensure_seeded`` would return it."""
    from apis.shared.assistants.listing import DEFAULT_CATEGORIES
    from apis.shared.assistants.models import AgentCategory

    return [
        AgentCategory(id=label, label=label, order=i * 10, enabled=True)
        for i, label in enumerate(DEFAULT_CATEGORIES)
    ]


@pytest.fixture(autouse=True)
def _categories():
    """Category validation reads admin-managed records (Phase 2); stub the store."""
    with patch(
        f"{SERVICE_MODULE}.ensure_seeded", new_callable=AsyncMock, side_effect=_default_categories
    ):
        yield


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


# ── the marketplace is public-only ───────────────────────────────────────────────────
class TestVisibilityBlock:
    """Publication requires PUBLIC.

    Sharing an agent with named coworkers is a *separate* mechanism, and a listing carries
    no audience of its own — so a published SHARED or PRIVATE agent is a tile everyone sees
    and nobody but the author can open. That was a live incident: two demo users tapped Add
    on a published-but-SHARED agent and got a bare 404.
    """

    @pytest.mark.parametrize(
        "visibility,expected",
        [("PRIVATE", "private"), ("SHARED", "shared with specific people")],
    )
    def test_a_non_public_agent_cannot_be_submitted(
        self, app, make_user, _no_writes, visibility, expected
    ):
        assistant = _make_assistant(visibility=visibility)
        mock_auth_user(app, make_user())
        with _owner(assistant):
            resp = TestClient(app).post(
                "/agents/ast-001/listing/submit", json={"category": "Administration"}
            )

        assert resp.status_code == 400
        assert expected in resp.json()["detail"]
        _no_writes.assert_not_called()

    def test_a_public_agent_submits_normally(self, app, make_user, _no_writes):
        """The gate must not be so eager it blocks the ordinary path."""
        mock_auth_user(app, make_user())
        with _owner(_make_assistant(visibility="PUBLIC")):
            resp = TestClient(app).post(
                "/agents/ast-001/listing/submit", json={"category": "Administration"}
            )

        assert resp.status_code == 200
        assert resp.json()["listing"]["state"] == "in_review"

    def test_preflight_shows_the_block_so_the_dialog_can_disable_submit(
        self, app, make_user, _no_writes
    ):
        """Shown and enforced by one function — the dialog and the transition cannot drift."""
        mock_auth_user(app, make_user())
        with _owner(_make_assistant(visibility="SHARED")):
            resp = TestClient(app).get("/agents/ast-001/listing/preflight")

        body = resp.json()
        assert resp.status_code == 200
        assert body["blockReason"] is not None
        assert "shared with specific people" in body["blockReason"]
        assert body["reachability"] == "shared_only"

    def test_the_memory_space_block_still_wins_when_both_apply(
        self, app, make_user, _no_writes
    ):
        """Ordering is deliberate: the harder problem is named first, not the cheaper one."""
        assistant = _make_assistant(
            visibility="PRIVATE",
            bindings=[AgentBinding(kind="memory_space", ref="mem-042", config={})],
        )
        mock_auth_user(app, make_user())
        with _owner(assistant), patch(f"{SERVICE_MODULE}.MemorySpaceService") as svc:
            svc.return_value.list_spaces_for_user.return_value = []
            resp = TestClient(app).get("/agents/ast-001/listing/preflight")

        assert "memory space" in resp.json()["blockReason"].lower()


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

    # ── #749 — the author sets the shelf subtitle at submission ──────────────────
    def test_a_supplied_tagline_is_written_with_the_listing(
        self, app, make_user, _no_writes
    ):
        """One write, not two — same reason the D13 patch path bundles presentation."""
        mock_auth_user(app, make_user())
        with _owner(_make_assistant(bindings=[])):
            resp = TestClient(app).post(
                "/agents/ast-001/listing/submit",
                json={"category": "Teaching", "tagline": "Cites policy, with sources"},
            )

        assert resp.status_code == 200
        assert _no_writes.call_args.kwargs["tagline"] == "Cites policy, with sources"

    def test_an_omitted_tagline_leaves_the_existing_one_alone(
        self, app, make_user, _no_writes
    ):
        """A resubmission that never touches the field must not blank the subtitle.

        ``write_listing`` treats ``None`` as "don't set", so the omission has to reach it
        as ``None`` rather than as an empty string.
        """
        mock_auth_user(app, make_user())
        with _owner(_make_assistant(bindings=[])):
            TestClient(app).post(
                "/agents/ast-001/listing/submit", json={"category": "Teaching"}
            )

        assert _no_writes.call_args.kwargs["tagline"] is None

    def test_a_whitespace_only_tagline_is_treated_as_omitted(
        self, app, make_user, _no_writes
    ):
        mock_auth_user(app, make_user())
        with _owner(_make_assistant(bindings=[])):
            TestClient(app).post(
                "/agents/ast-001/listing/submit",
                json={"category": "Teaching", "tagline": "   "},
            )

        assert _no_writes.call_args.kwargs["tagline"] is None

    def test_a_tagline_past_the_shelf_limit_is_rejected(self, app, make_user, _no_writes):
        """80 chars is a layout constraint, not a suggestion — the row is one line."""
        mock_auth_user(app, make_user())
        with _owner(_make_assistant(bindings=[])):
            resp = TestClient(app).post(
                "/agents/ast-001/listing/submit",
                json={"category": "Teaching", "tagline": "x" * 81},
            )

        assert resp.status_code == 422
        _no_writes.assert_not_called()

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


# ── D7 preflight — the same checks, without the transition ───────────────────────────
class TestPreflight:
    """The submit dialog's read.

    The point of this route is that the author sees the D7 answers *before* committing,
    and that what they see comes from the same helpers the transition enforces. Each
    test below therefore pairs with one in TestMemorySpaceBlock / TestSkillDisclosure.
    """

    def test_enumerates_the_authors_own_skills_without_submitting(
        self, app, make_user, _no_writes
    ):
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
            resp = TestClient(app).get("/agents/ast-001/listing/preflight")

        assert resp.status_code == 200
        body = resp.json()
        assert [s["label"] for s in body["exposedSkills"]] == ["Policy Citation Format"]
        assert body["blockReason"] is None
        # A preflight must never move the listing — it is a rehearsal, not the act.
        _no_writes.assert_not_called()

    def test_memory_space_returns_a_block_reason_rather_than_an_error(
        self, app, make_user, _no_writes
    ):
        """A disabled Submit with an explanation beats a 400 after the click."""
        assistant = _make_assistant(
            bindings=[AgentBinding(kind="memory_space", ref="mem-042", config={})]
        )
        space = SimpleNamespace(space_id="mem-042", name="Oliver", owner_id="user-001")

        mock_auth_user(app, make_user())
        with _owner(assistant), patch(f"{SERVICE_MODULE}.MemorySpaceService") as svc:
            svc.return_value.list_spaces_for_user.return_value = [(space, "owner")]
            resp = TestClient(app).get("/agents/ast-001/listing/preflight")

        assert resp.status_code == 200
        body = resp.json()
        assert "Oliver" in body["blockReason"]
        # Blocked agents disclose nothing: an author who cannot publish at all should not
        # first be walked through a skill-exposure confirmation. Mirrors submit_listing.
        assert body["exposedSkills"] == []

    def test_editor_may_not_preflight(self, app, make_user, _no_writes):
        """Skill exposure is a statement about the owner's publication, not an editor's."""
        mock_auth_user(app, make_user())
        with patch(
            f"{SERVICE_MODULE}.resolve_assistant_permission",
            new_callable=AsyncMock,
            return_value=(_make_assistant(), "editor"),
        ):
            resp = TestClient(app).get("/agents/ast-001/listing/preflight")

        assert resp.status_code == 403

    def test_404_when_marketplace_flag_off(self, app, make_user, monkeypatch):
        monkeypatch.setenv("AGENT_MARKETPLACE_ENABLED", "false")
        mock_auth_user(app, make_user())
        assert TestClient(app).get("/agents/ast-001/listing/preflight").status_code == 404

    def test_preflight_is_not_captured_by_the_agent_id_path_param(
        self, app, make_user, _no_writes
    ):
        """`/{agent_id}/listing/preflight` must not resolve as `GET /{agent_id}`."""
        mock_auth_user(app, make_user())
        with _owner(_make_assistant(bindings=[])):
            resp = TestClient(app).get("/agents/ast-001/listing/preflight")

        assert resp.status_code == 200
        assert "exposedSkills" in resp.json()
