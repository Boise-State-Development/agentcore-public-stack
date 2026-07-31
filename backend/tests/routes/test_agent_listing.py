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
    """Stub the persistence + publisher resolution so tests exercise decisions, not I/O.

    Submitting cuts a snapshot as well as writing the listing, so both have to be stubbed
    or the version write reaches a real table. The version mocks hang off the yielded write
    mock, leaving every existing ``_no_writes.call_args`` assertion untouched.
    """
    with patch(f"{SERVICE_MODULE}.write_listing", new_callable=AsyncMock) as write, patch(
        f"{SERVICE_MODULE}.create_version", new_callable=AsyncMock
    ) as create, patch(
        f"{SERVICE_MODULE}.set_version_index", new_callable=AsyncMock
    ) as index, patch(
        f"{SERVICE_MODULE}.ensure_individual_profile",
        new_callable=AsyncMock,
        return_value=SimpleNamespace(id="user-user-001"),
    ):
        create.return_value = SimpleNamespace(version=1)
        write.create_version = create
        write.set_version_index = index
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

    def test_preflight_asks_for_consent_rather_than_blocking(
        self, app, make_user, _no_writes
    ):
        """Needing to go public is fixable *in the dialog*, so it is not a block.

        Folding it into ``blockReason`` sent the author to another screen on their very
        first submission — every agent starts PRIVATE.
        """
        mock_auth_user(app, make_user())
        with _owner(_make_assistant(visibility="SHARED")):
            resp = TestClient(app).get("/agents/ast-001/listing/preflight")

        body = resp.json()
        assert resp.status_code == 200
        assert body["requiresPublic"] is True
        assert body["blockReason"] is None, "not a block — the checkbox resolves it"
        assert body["reachability"] == "shared_only"

    def test_preflight_asks_nothing_of_an_already_public_agent(
        self, app, make_user, _no_writes
    ):
        mock_auth_user(app, make_user())
        with _owner(_make_assistant(visibility="PUBLIC")):
            body = TestClient(app).get("/agents/ast-001/listing/preflight").json()

        assert body["requiresPublic"] is False
        assert body["blockReason"] is None

    def test_the_memory_space_block_is_still_a_block(self, app, make_user, _no_writes):
        """A real dead end stays one — nothing in the dialog can resolve it."""
        assistant = _make_assistant(
            visibility="PRIVATE",
            bindings=[AgentBinding(kind="memory_space", ref="mem-042", config={})],
        )
        mock_auth_user(app, make_user())
        with _owner(assistant), patch(f"{SERVICE_MODULE}.MemorySpaceService") as svc:
            svc.return_value.list_spaces_for_user.return_value = []
            body = TestClient(app).get("/agents/ast-001/listing/preflight").json()

        assert "memory space" in body["blockReason"].lower()
        assert body["requiresPublic"] is True, "still true — it just is not what stops them"


class TestGoingPublicOnSubmission:
    """The consent checkbox: the author widens visibility from the submit dialog."""

    @pytest.mark.parametrize("visibility", ["PRIVATE", "SHARED"])
    def test_consent_widens_visibility_in_the_same_write(
        self, app, make_user, _no_writes, visibility
    ):
        """One write, or an agent could end up listed but unreachable — the whole bug."""
        mock_auth_user(app, make_user())
        with _owner(_make_assistant(visibility=visibility)):
            resp = TestClient(app).post(
                "/agents/ast-001/listing/submit",
                json={"category": "Administration", "makePublic": True},
            )

        assert resp.status_code == 200
        assert _no_writes.call_args.kwargs["visibility"] == "PUBLIC"

    def test_an_already_public_agent_is_not_rewritten(self, app, make_user, _no_writes):
        """A no-op write would be a lie in the audit trail."""
        mock_auth_user(app, make_user())
        with _owner(_make_assistant(visibility="PUBLIC")):
            resp = TestClient(app).post(
                "/agents/ast-001/listing/submit",
                json={"category": "Administration", "makePublic": True},
            )

        assert resp.status_code == 200
        assert _no_writes.call_args.kwargs["visibility"] is None

    def test_consent_does_not_bypass_the_memory_space_block(
        self, app, make_user, _no_writes
    ):
        """Ticking a visibility box must not wave through an unrelated, real block."""
        assistant = _make_assistant(
            visibility="PRIVATE",
            bindings=[AgentBinding(kind="memory_space", ref="mem-042", config={})],
        )
        mock_auth_user(app, make_user())
        with _owner(assistant), patch(f"{SERVICE_MODULE}.MemorySpaceService") as svc:
            svc.return_value.list_spaces_for_user.return_value = []
            resp = TestClient(app).post(
                "/agents/ast-001/listing/submit",
                json={"category": "Administration", "makePublic": True},
            )

        assert resp.status_code == 400
        _no_writes.assert_not_called()

    def test_an_omitted_flag_still_refuses(self, app, make_user, _no_writes):
        """Consent defaults off, so a direct API caller cannot widen an agent by accident."""
        mock_auth_user(app, make_user())
        with _owner(_make_assistant(visibility="PRIVATE")):
            resp = TestClient(app).post(
                "/agents/ast-001/listing/submit", json={"category": "Administration"}
            )

        assert resp.status_code == 400
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
    """One endpoint, two acts — the listing state decides which (§5.1)."""

    def test_withdrawing_a_live_listing_becomes_a_request(self, app, make_user, _no_writes):
        """The author asks; an admin decides. This used to go straight to ``private``.

        That was the side door around the review queue: D2 makes publication stop for a
        human, and un-publication did not — so an author could pull an approved Agent out
        from under everyone who pinned it, with no admin ever seeing it.
        """
        assistant = _make_assistant(
            listing=AgentListing(
                state="published",
                category="Teaching",
                publisherId="user-user-001",
                publishedVersion=3,
            )
        )
        mock_auth_user(app, make_user())
        with _owner(assistant):
            resp = TestClient(app).delete("/agents/ast-001/listing")

        assert resp.status_code == 200
        assert resp.json()["state"] == "withdrawal_requested"
        assert resp.json()["withdrawalRequestedAt"]

    def test_a_pending_request_stays_on_the_shelf(self, app, make_user, _no_writes):
        """⚠️ The load-bearing half. Asking must not itself unpublish anything.

        If the request cleared the index or the pointer, the author would have unilaterally
        delisted it just by asking — and a declined request would need the shelf rebuilt.
        """
        assistant = _make_assistant(
            listing=AgentListing(
                state="published",
                category="Teaching",
                publisherId="user-user-001",
                publishedVersion=3,
            )
        )
        mock_auth_user(app, make_user())
        with _owner(assistant):
            TestClient(app).delete("/agents/ast-001/listing")

        written = _no_writes.call_args.args[1]
        assert written.published_version == 3, "asking to withdraw must not unpublish"
        _no_writes.set_version_index.assert_not_awaited()

    @pytest.mark.parametrize("state", ["in_review", "changes_requested"])
    def test_withdrawing_a_pending_submission_is_immediate(
        self, app, make_user, _no_writes, state
    ):
        """Nothing is on the shelf yet, so this stays the author's own call."""
        assistant = _make_assistant(
            listing=AgentListing(state=state, category="Teaching", publisherId="user-user-001")
        )
        mock_auth_user(app, make_user())
        with _owner(assistant):
            resp = TestClient(app).delete("/agents/ast-001/listing")

        assert resp.status_code == 200
        assert resp.json()["state"] == "private"

    def test_a_second_request_on_an_already_requested_listing_is_refused(
        self, app, make_user, _no_writes
    ):
        """``withdrawal_requested → withdrawal_requested`` is not an edge; nothing self-loops."""
        assistant = _make_assistant(
            listing=AgentListing(
                state="withdrawal_requested", category="Teaching", publisherId="user-user-001"
            )
        )
        mock_auth_user(app, make_user())
        with _owner(assistant):
            resp = TestClient(app).delete("/agents/ast-001/listing")

        assert resp.status_code == 400

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


class TestSubmissionCutsTheSnapshot:
    """version-snapshots §3.2 — the version is cut at submit, not at approve.

    Taking it at approval leaves a window the author can edit through: submit, admin reads,
    author edits, admin approves — and what publishes is not what was read. That is the same
    class of bug the epic exists to close, so it gets asserted rather than assumed.
    """

    def test_submitting_cuts_a_version_and_records_its_number(self, app, make_user, _no_writes):
        mock_auth_user(app, make_user())
        with _owner(_make_assistant(bindings=[])):
            resp = TestClient(app).post(
                "/agents/ast-001/listing/submit", json={"category": "Administration"}
            )

        assert resp.status_code == 200
        _no_writes.create_version.assert_awaited_once()
        assert _no_writes.call_args.args[1].submitted_version == 1

    def test_the_snapshot_carries_the_submission_as_composed(self, app, make_user, _no_writes):
        """Category, publisher and tagline are the author's choices *in this act*.

        Snapshotting the record as it stood a moment earlier would freeze the previous
        category — and the reviewer would be looking at a shelf placement nobody chose.
        """
        mock_auth_user(app, make_user())
        with _owner(_make_assistant(bindings=[], tagline="Old subtitle")):
            TestClient(app).post(
                "/agents/ast-001/listing/submit",
                json={"category": "Teaching", "tagline": "Cite the policy manual"},
            )

        snapshot = _no_writes.create_version.await_args.args[1]
        assert snapshot.category == "Teaching"
        assert snapshot.tagline == "Cite the policy manual"

    def test_submitting_indexes_nothing(self, app, make_user, _no_writes):
        """A pending submission is not in the store. Only approval writes the key."""
        mock_auth_user(app, make_user())
        with _owner(_make_assistant(bindings=[])):
            TestClient(app).post("/agents/ast-001/listing/submit", json={"category": "Administration"})

        _no_writes.set_version_index.assert_not_awaited()

    def test_resubmitting_leaves_the_live_version_serving(self, app, make_user, _no_writes):
        """The shelf must not go blank while a review is pending.

        A resubmission cuts a *new* version and points ``submittedVersion`` at it, but the
        previously approved one keeps its key until an admin promotes the replacement.
        """
        mock_auth_user(app, make_user())
        listing = AgentListing.model_validate(
            {
                "state": "changes_requested",
                "category": "Administration",
                "publisherId": "pub-registrar",
                "publishedVersion": 2,
            }
        )
        with _owner(_make_assistant(bindings=[], listing=listing)):
            TestClient(app).post("/agents/ast-001/listing/submit", json={"category": "Administration"})

        written = _no_writes.call_args.args[1]
        assert written.published_version == 2, "a resubmission must not unpublish"
        assert written.submitted_version == 1
        _no_writes.set_version_index.assert_not_awaited()


class TestAnAuthorCannotGrantTheirOwnWithdrawal:
    """§5.1's core rule, guarded where it can actually be walked.

    ``withdrawal_requested → private`` is a legal edge — it is how an admin *grants* a
    withdrawal — and ``private`` is an author target. So the only thing keeping an author off
    it is that ``withdraw_listing`` never chooses ``private`` as the target for a pending
    request. That used to be implied by the transition table; once the target came from
    ``is_on_shelf`` instead of the state name, a pending request with no published pointer
    resolved to ``private`` and the author granted their own withdrawal.
    """

    @pytest.mark.parametrize("published_version", [3, None])
    def test_asking_twice_is_refused_however_the_pointer_looks(
        self, app, make_user, _no_writes, published_version
    ):
        assistant = _make_assistant(
            listing=AgentListing(
                state="withdrawal_requested",
                category="Teaching",
                publisherId="user-user-001",
                publishedVersion=published_version,
            )
        )
        mock_auth_user(app, make_user())
        with _owner(assistant):
            resp = TestClient(app).delete("/agents/ast-001/listing")

        assert resp.status_code == 400
        assert "already asked" in resp.json()["detail"]
        # The decisive assertion: nothing was written, so the listing did not go private.
        _no_writes.assert_not_awaited()
