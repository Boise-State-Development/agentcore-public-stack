"""Agent Marketplace Phase 1 — the admin review / listings surface (D2, D12, D13).

The rule this file exists to hold in place is D13's split: an admin may edit everything
the store *renders* and nothing about what the agent *does*. The second rule, quieter but
more dangerous to lose, is that ``publisherId`` is display-only — re-attributing a listing
must change the name on the shelf and nothing about who can run it.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.agents.routes import router
from apis.shared.assistants.models import AgentListing, Assistant, PublisherProfile
from apis.shared.auth import require_admin
from tests.conftest import override_admin_auth

SERVICE_MODULE = "apis.app_api.agent_designer.services.listing_service"
ADMIN_MODULE = "apis.app_api.admin.agents.routes"


def _make_assistant(**overrides) -> Assistant:
    defaults = dict(
        assistantId="ast-001",
        ownerId="user-author",
        ownerName="Ada Author",
        name="Policy Lookup",
        description="Find and cite university policy",
        instructions="Answer from the policy manual.",
        vectorIndexId="idx-001",
        # PUBLIC by default: approval now refuses anything else, so a publishable agent is
        # the baseline. Tests of the narrowed-after-submit case override this explicitly.
        visibility="PUBLIC",
        usageCount=12,
        createdAt="2026-07-01T00:00:00Z",
        updatedAt="2026-07-01T00:00:00Z",
        status="COMPLETE",
    )
    defaults.update(overrides)
    return Assistant.model_validate(defaults)


def _listing(state="in_review", **overrides) -> AgentListing:
    # ``submittedVersion`` is part of the baseline because every submission cuts a snapshot
    # now — a listing without one predates the feature, which is its own (tested) case.
    defaults = dict(
        state=state,
        category="Administration",
        publisherId="pub-registrar",
        submittedVersion=4,
    )
    defaults.update(overrides)
    return AgentListing.model_validate(defaults)


@pytest.fixture
def app(make_user):
    _app = FastAPI()
    _app.include_router(router, prefix="/admin")
    override_admin_auth(
        _app,
        lambda: make_user(
            user_id="admin-001", name="Sam Admin", roles=["system_admin"]
        ),
    )
    return _app


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
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
    """Stub every persistence call the listing service makes.

    Publishing is three writes now, not one — the listing block, the snapshot, and the
    store key — so a fixture that stubbed only ``write_listing`` would let the other two
    reach a real table. The version mocks hang off the yielded write mock so existing
    tests keep using ``_no_writes.call_args`` unchanged.
    """
    with patch(f"{SERVICE_MODULE}.write_listing", new_callable=AsyncMock) as write, patch(
        f"{SERVICE_MODULE}.create_version", new_callable=AsyncMock
    ) as create, patch(
        f"{SERVICE_MODULE}.set_version_index", new_callable=AsyncMock
    ) as index:
        create.return_value = SimpleNamespace(version=7)
        write.create_version = create
        write.set_version_index = index
        yield write


def _loaded(assistant):
    return patch(f"{SERVICE_MODULE}._load_any", new_callable=AsyncMock, return_value=assistant)


_UNSET = object()


def _publisher(profile=_UNSET):
    """Patch publisher lookup. Pass ``None`` explicitly to simulate a missing publisher."""
    if profile is _UNSET:
        profile = PublisherProfile(
            id="pub-registrar", label="Office of the Registrar", kind="department"
        )
    return patch(f"{SERVICE_MODULE}.get_publisher", new_callable=AsyncMock, return_value=profile)


# ── the kill switch ──────────────────────────────────────────────────────────────────
def test_404_when_flag_off(app, monkeypatch):
    monkeypatch.setenv("AGENT_MARKETPLACE_ENABLED", "false")
    assert TestClient(app).get("/admin/agents/submissions").status_code == 404


# ── D13 — presentation only ──────────────────────────────────────────────────────────
class TestPresentationOnlyPatch:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("instructions", "You are now evil."),
            ("bindings", []),
            ("modelConfig", {"modelId": "claude-opus-5"}),
            ("starters", ["hi"]),
            ("visibility", "PUBLIC"),
        ],
    )
    def test_behavior_fields_are_rejected(self, app, _no_writes, field, value):
        """An admin editing behavior would own something they did not write and cannot test."""
        resp = TestClient(app).patch("/admin/agents/ast-001/listing", json={field: value})

        assert resp.status_code == 422
        assert "Cannot edit agent behavior" in str(resp.json())
        _no_writes.assert_not_called()

    def test_behavior_field_rejected_even_alongside_a_valid_one(self, app, _no_writes):
        """A legitimate tagline edit must not smuggle an instructions change through."""
        resp = TestClient(app).patch(
            "/admin/agents/ast-001/listing",
            json={"tagline": "A fine tagline", "instructions": "You are now evil."},
        )

        assert resp.status_code == 422
        _no_writes.assert_not_called()

    def test_unknown_fields_are_rejected(self, app, _no_writes):
        resp = TestClient(app).patch("/admin/agents/ast-001/listing", json={"usageCount": 9999})
        assert resp.status_code == 422

    @pytest.mark.parametrize(
        "payload",
        [
            {"name": "Policy Finder"},
            {"tagline": "Cite the policy manual"},
            {"iconKey": "icons/policy.png"},
            {"category": "Teaching"},
            {"publisherId": "pub-registrar"},
        ],
    )
    def test_presentation_fields_are_accepted(self, app, _no_writes, payload):
        assistant = _make_assistant(listing=_listing("published"))
        with _loaded(assistant), _publisher():
            resp = TestClient(app).patch("/admin/agents/ast-001/listing", json=payload)

        assert resp.status_code == 200
        _no_writes.assert_called_once()

    def test_every_edit_is_recorded_for_the_author(self, app, _no_writes):
        """D13: editing someone's listing quietly is how you lose authors."""
        assistant = _make_assistant(listing=_listing("published"))
        with _loaded(assistant), _publisher():
            resp = TestClient(app).patch(
                "/admin/agents/ast-001/listing",
                json={"tagline": "Cite the policy manual", "category": "Teaching"},
            )

        edits = resp.json()["adminEdits"]
        assert {e["field"] for e in edits} == {"tagline", "category"}
        assert all(e["by"] == "Sam Admin" for e in edits)

    def test_edits_are_recorded_by_the_name_the_author_would_recognize(self, app, _no_writes):
        """The author reads this string; internal attribute names would be noise."""
        assistant = _make_assistant(listing=_listing("published"))
        with _loaded(assistant), _publisher():
            resp = TestClient(app).patch(
                "/admin/agents/ast-001/listing",
                json={"iconKey": "icons/policy.png", "publisherId": "pub-registrar"},
            )

        assert {e["field"] for e in resp.json()["adminEdits"]} == {"icon", "publisher"}

    def test_admin_edits_accumulate_rather_than_replace(self, app, _no_writes):
        prior = _listing("published", adminEdits=[{"field": "name", "at": "2026-07-01Z", "by": "Prior"}])
        with _loaded(_make_assistant(listing=prior)), _publisher():
            resp = TestClient(app).patch("/admin/agents/ast-001/listing", json={"tagline": "New"})

        assert [e["field"] for e in resp.json()["adminEdits"]] == ["name", "tagline"]

    def test_unknown_category_is_rejected(self, app, _no_writes):
        with _loaded(_make_assistant(listing=_listing("published"))):
            resp = TestClient(app).patch(
                "/admin/agents/ast-001/listing", json={"category": "Miscellaneous"}
            )
        assert resp.status_code == 400

    def test_empty_patch_is_rejected(self, app, _no_writes):
        with _loaded(_make_assistant(listing=_listing("published"))):
            resp = TestClient(app).patch("/admin/agents/ast-001/listing", json={})
        assert resp.status_code == 400

    def test_patching_an_unsubmitted_agent_is_404(self, app, _no_writes):
        with _loaded(_make_assistant()):
            resp = TestClient(app).patch("/admin/agents/ast-001/listing", json={"tagline": "x"})
        assert resp.status_code == 404


# ── D12 — publisher is display only ──────────────────────────────────────────────────
class TestPublisherIsDisplayOnly:
    def test_reattribution_does_not_touch_ownership(self, app, _no_writes):
        """The trap this guards: a display projection that looks like a grant.

        ``ownerId`` governs edit rights and Skills v2 invoke-through
        (``skill.owner_id == agent.owner_id``). Changing the name on the shelf must leave
        both untouched.
        """
        assistant = _make_assistant(listing=_listing("published"))
        institution = PublisherProfile(id="pub-bsu", label="Boise State", kind="institution", verified=True)

        with _loaded(assistant), _publisher(institution):
            resp = TestClient(app).patch(
                "/admin/agents/ast-001/listing", json={"publisherId": "pub-bsu"}
            )

        assert resp.status_code == 200
        assert resp.json()["publisherId"] == "pub-bsu"
        # The agent record's owner is untouched, and the write carried no owner change.
        assert assistant.owner_id == "user-author"
        written_listing = _no_writes.call_args.args[1]
        assert not hasattr(written_listing, "owner_id")

    def test_admin_may_attribute_to_a_publisher_the_author_is_not_eligible_for(
        self, app, _no_writes
    ):
        """D12: this is how the store gets official Agents without a staff member's name.

        Eligibility is a *proposal* allowlist on the author's path only — the admin path
        must not consult it, so ``list_publishers_for_user`` is never called here.
        """
        assistant = _make_assistant(listing=_listing("published"))
        with _loaded(assistant), _publisher(), patch(
            f"{SERVICE_MODULE}.list_publishers_for_user", new_callable=AsyncMock
        ) as eligibility:
            resp = TestClient(app).patch(
                "/admin/agents/ast-001/listing", json={"publisherId": "pub-registrar"}
            )

        assert resp.status_code == 200
        eligibility.assert_not_called()

    def test_unknown_publisher_is_rejected(self, app, _no_writes):
        with _loaded(_make_assistant(listing=_listing("published"))), _publisher(None):
            resp = TestClient(app).patch(
                "/admin/agents/ast-001/listing", json={"publisherId": "pub-ghost"}
            )
        assert resp.status_code == 400


# ── D2 — review + takedown ───────────────────────────────────────────────────────────
class TestReview:
    def test_approve_publishes(self, app, _no_writes):
        with _loaded(_make_assistant(listing=_listing("in_review"))):
            resp = TestClient(app).post(
                "/admin/agents/ast-001/review", json={"decision": "approve"}
            )

        assert resp.status_code == 200
        assert resp.json()["state"] == "published"
        assert resp.json()["reviewedBy"] == "admin-001"

    def test_request_changes_requires_a_reason(self, app, _no_writes):
        """The reason renders on the author's card so they never have to ask what happened."""
        with _loaded(_make_assistant(listing=_listing("in_review"))):
            resp = TestClient(app).post(
                "/admin/agents/ast-001/review", json={"decision": "request_changes"}
            )

        assert resp.status_code == 400
        assert "reason" in resp.json()["detail"]
        _no_writes.assert_not_called()

    def test_request_changes_with_a_reason(self, app, _no_writes):
        with _loaded(_make_assistant(listing=_listing("in_review"))):
            resp = TestClient(app).post(
                "/admin/agents/ast-001/review",
                json={"decision": "request_changes", "note": "Please add a tagline."},
            )

        assert resp.json()["state"] == "changes_requested"
        assert resp.json()["reviewNote"] == "Please add a tagline."

    def test_reviewer_may_recategorize_at_approval(self, app, _no_writes):
        with _loaded(_make_assistant(listing=_listing("in_review"))):
            resp = TestClient(app).post(
                "/admin/agents/ast-001/review",
                json={"decision": "approve", "category": "Teaching"},
            )

        assert resp.json()["category"] == "Teaching"

    @pytest.mark.parametrize("visibility", ["PRIVATE", "SHARED"])
    def test_cannot_approve_an_agent_narrowed_since_submission(
        self, app, _no_writes, visibility
    ):
        """``visibility`` is an independent axis — the submit-time gate says nothing about now.

        The author can narrow access between submitting and being reviewed, and approving
        anyway shelves a tile that 404s for everyone who taps it.
        """
        with _loaded(_make_assistant(visibility=visibility, listing=_listing("in_review"))):
            resp = TestClient(app).post(
                "/admin/agents/ast-001/review", json={"decision": "approve"}
            )

        assert resp.status_code == 400
        assert visibility.title() in resp.json()["detail"]
        _no_writes.assert_not_called()

    def test_changes_may_still_be_requested_on_a_narrowed_agent(self, app, _no_writes):
        """The gate is on publishing, not on reviewing — sending it back must still work."""
        with _loaded(_make_assistant(visibility="PRIVATE", listing=_listing("in_review"))):
            resp = TestClient(app).post(
                "/admin/agents/ast-001/review",
                json={"decision": "request_changes", "note": "Set visibility to Public."},
            )

        assert resp.status_code == 200
        assert resp.json()["state"] == "changes_requested"

    def test_cannot_approve_something_not_in_review(self, app, _no_writes):
        """Approval is the only door into the store, and in_review is the only way to it."""
        with _loaded(_make_assistant(listing=_listing("private"))):
            resp = TestClient(app).post(
                "/admin/agents/ast-001/review", json={"decision": "approve"}
            )

        assert resp.status_code == 400
        _no_writes.assert_not_called()

    def test_reviewing_an_unsubmitted_agent_is_404(self, app, _no_writes):
        with _loaded(_make_assistant()):
            resp = TestClient(app).post(
                "/admin/agents/ast-001/review", json={"decision": "approve"}
            )
        assert resp.status_code == 404


class TestTakedown:
    def test_takedown_delists(self, app, _no_writes):
        with _loaded(_make_assistant(listing=_listing("published"))):
            resp = TestClient(app).post(
                "/admin/agents/ast-001/takedown", json={"reason": "Off-brand icon."}
            )

        assert resp.status_code == 200
        assert resp.json()["state"] == "taken_down"
        assert resp.json()["reviewNote"] == "Off-brand icon."

    def test_takedown_requires_a_reason(self, app, _no_writes):
        with _loaded(_make_assistant(listing=_listing("published"))):
            resp = TestClient(app).post("/admin/agents/ast-001/takedown", json={"reason": ""})
        assert resp.status_code == 422

    def test_cannot_take_down_something_not_published(self, app, _no_writes):
        with _loaded(_make_assistant(listing=_listing("in_review"))):
            resp = TestClient(app).post(
                "/admin/agents/ast-001/takedown", json={"reason": "nope"}
            )
        assert resp.status_code == 400


# ── the tables ───────────────────────────────────────────────────────────────────────
class TestAdminTables:
    def test_submissions_returns_the_queue_with_a_pending_count(self, app):
        rows = [
            {
                "PK": "AST#ast-001",
                **_make_assistant(listing=_listing("in_review", submittedAt="2026-07-20Z")).model_dump(
                    by_alias=True, exclude_none=True
                ),
            }
        ]
        with patch(f"{SERVICE_MODULE}.list_by_state", new_callable=AsyncMock, return_value=rows), patch(
            f"{SERVICE_MODULE}.list_publishers",
            new_callable=AsyncMock,
            return_value=[
                PublisherProfile(id="pub-registrar", label="Office of the Registrar", kind="department")
            ],
        ):
            resp = TestClient(app).get("/admin/agents/submissions")

        assert resp.status_code == 200
        body = resp.json()
        assert body["pendingCount"] == 1
        row = body["listings"][0]
        # The queue row carries who to talk to about behavior (the author) and the
        # attribution separately — they are different people by design (D12).
        assert row["ownerName"] == "Ada Author"
        assert row["publisher"]["label"] == "Office of the Registrar"
        assert row["state"] == "in_review"

    def test_listings_ignores_records_that_were_never_submitted(self, app):
        rows = [{"PK": "AST#ast-002", **_make_assistant().model_dump(by_alias=True, exclude_none=True)}]
        with patch(f"{SERVICE_MODULE}.list_by_state", new_callable=AsyncMock, return_value=rows), patch(
            f"{SERVICE_MODULE}.list_publishers", new_callable=AsyncMock, return_value=[]
        ):
            resp = TestClient(app).get("/admin/agents/listings")

        assert resp.json()["listings"] == []

    def test_row_renders_without_a_resolved_publisher(self, app):
        """A deleted publisher leaves a visible gap to reassign, not a 500."""
        rows = [
            {
                "PK": "AST#ast-001",
                **_make_assistant(listing=_listing("published")).model_dump(
                    by_alias=True, exclude_none=True
                ),
            }
        ]
        with patch(f"{SERVICE_MODULE}.list_by_state", new_callable=AsyncMock, return_value=rows), patch(
            f"{SERVICE_MODULE}.list_publishers", new_callable=AsyncMock, return_value=[]
        ):
            resp = TestClient(app).get("/admin/agents/listings")

        assert resp.json()["listings"][0]["publisher"] is None

    def test_categories_are_served_for_the_pickers(self, app):
        with patch(
            f"{ADMIN_MODULE}.ensure_seeded",
            new_callable=AsyncMock,
            side_effect=_default_categories,
        ):
            resp = TestClient(app).get("/admin/agents/categories")

        assert resp.status_code == 200
        categories = resp.json()["categories"]
        assert [c["id"] for c in categories][0] == "Administration"
        # Ids double as the GSI5 partition suffix, so they must survive as written.
        assert all(c["id"] == c["label"] for c in categories)


# ── version snapshots — promotion, not drift detection ───────────────────────────────
# The `#744` drift-baseline and drift-derivation suites lived here. They are gone with the
# feature: they tested that an author's post-approval edit was *detected*, and such an edit
# can no longer reach a published listing at all. What replaces them tests the control that
# made the detector unnecessary.


def _listing_rows(assistant):
    """Patch the admin table read to return exactly this one agent."""
    rows = [{"PK": "AST#ast-001", **assistant.model_dump(by_alias=True, exclude_none=True)}]
    return (
        patch(f"{SERVICE_MODULE}.list_by_state", new_callable=AsyncMock, return_value=rows),
        patch(f"{SERVICE_MODULE}.list_publishers", new_callable=AsyncMock, return_value=[]),
    )


class TestApprovalPromotesTheSubmittedVersion:
    def test_approval_publishes_the_version_the_reviewer_read(self, app, _no_writes):
        """Not "the latest" — an admin presentation edit could have moved that underneath."""
        index = _no_writes.set_version_index
        listing = _listing("in_review", submittedVersion=4)
        with _loaded(_make_assistant(listing=listing)):
            resp = TestClient(app).post(
                "/admin/agents/ast-001/review", json={"decision": "approve"}
            )

        assert resp.status_code == 200
        assert _no_writes.call_args.args[1].published_version == 4
        assert index.await_args.args[1] == 4

    def test_the_key_lands_on_the_version_row_in_the_listings_partition(
        self, app, _no_writes
    ):
        index = _no_writes.set_version_index
        with _loaded(_make_assistant(listing=_listing("in_review", submittedVersion=4))):
            TestClient(app).post("/admin/agents/ast-001/review", json={"decision": "approve"})

        assert index.await_args.args[2] == {
            "GSI5_PK": "LISTED#Administration",
            # The Agent's creation timestamp, not the version's: browse is newest-first by
            # Agent, and a re-approved old Agent must not jump the shelf.
            "GSI5_SK": "CREATED#2026-07-01T00:00:00Z",
        }

    def test_recategorizing_at_approval_shelves_it_where_the_reviewer_put_it(
        self, app, _no_writes
    ):
        """Placement is the key. The frozen snapshot is never rewritten to match."""
        index = _no_writes.set_version_index
        with _loaded(_make_assistant(listing=_listing("in_review", submittedVersion=4))):
            TestClient(app).post(
                "/admin/agents/ast-001/review",
                json={"decision": "approve", "category": "Teaching"},
            )

        assert index.await_args.args[2]["GSI5_PK"] == "LISTED#Teaching"

    def test_promotion_takes_the_key_off_the_version_it_supersedes(
        self, app, _no_writes
    ):
        """Two versions of one Agent must never sit on the shelf together."""
        index = _no_writes.set_version_index
        listing = _listing("in_review", submittedVersion=4, publishedVersion=2)
        with _loaded(_make_assistant(listing=listing)):
            TestClient(app).post("/admin/agents/ast-001/review", json={"decision": "approve"})

        assert [(c.args[1], c.args[2]) for c in index.await_args_list] == [
            (4, {"GSI5_PK": "LISTED#Administration", "GSI5_SK": "CREATED#2026-07-01T00:00:00Z"}),
            (2, None),
        ]

    def test_a_submission_with_no_snapshot_is_refused(self, app, _no_writes):
        """Predates the feature. Publishing it would shelve an empty tile."""
        with _loaded(_make_assistant(listing=_listing("in_review", submittedVersion=None))):
            resp = TestClient(app).post(
                "/admin/agents/ast-001/review", json={"decision": "approve"}
            )

        assert resp.status_code == 400
        assert "resubmit" in resp.json()["detail"]
        _no_writes.assert_not_awaited()

    def test_request_changes_promotes_nothing(self, app, _no_writes):
        index = _no_writes.set_version_index
        with _loaded(_make_assistant(listing=_listing("in_review", submittedVersion=4))):
            TestClient(app).post(
                "/admin/agents/ast-001/review",
                json={"decision": "request_changes", "note": "Add a tagline."},
            )

        index.assert_not_awaited()
        assert _no_writes.call_args.args[1].published_version is None

    def test_request_changes_leaves_a_live_listing_on_the_shelf(self, app, _no_writes):
        """It does not unpublish. The approved version keeps serving until one replaces it."""
        index = _no_writes.set_version_index
        listing = _listing("published", publishedVersion=2)
        with _loaded(_make_assistant(listing=listing)):
            TestClient(app).post(
                "/admin/agents/ast-001/review",
                json={"decision": "request_changes", "note": "Please revise."},
            )

        index.assert_not_awaited()
        assert _no_writes.call_args.args[1].published_version == 2


class TestTakedownClearsTheShelf:
    def test_takedown_unindexes_the_published_version_before_recording_it(
        self, app, _no_writes
    ):
        """Fail-closed ordering: a half-failed takedown must leave it invisible."""
        index = _no_writes.set_version_index
        calls = []
        index.side_effect = lambda *a, **k: calls.append("unindex")
        _no_writes.side_effect = lambda *a, **k: calls.append("write")

        with _loaded(_make_assistant(listing=_listing("published", publishedVersion=2))):
            resp = TestClient(app).post(
                "/admin/agents/ast-001/takedown", json={"reason": "Out of date."}
            )

        assert resp.status_code == 200
        assert calls == ["unindex", "write"]
        assert index.await_args.args[1:] == (2, None)

    def test_takedown_clears_the_published_pointer(self, app, _no_writes):
        """A taken-down listing naming a live version reads as published to every reader."""
        with _loaded(_make_assistant(listing=_listing("published", publishedVersion=2))):
            TestClient(app).post("/admin/agents/ast-001/takedown", json={"reason": "Stale."})

        assert _no_writes.call_args.args[1].published_version is None


class TestAdminEditsCutAVersion:
    """§6.2 — the store renders the snapshot, so a presentation edit has to cut one."""

    def test_editing_a_live_listing_promotes_a_new_snapshot(self, app, _no_writes):
        create, index = _no_writes.create_version, _no_writes.set_version_index
        with _loaded(_make_assistant(listing=_listing("published", publishedVersion=2))), _publisher():
            resp = TestClient(app).patch(
                "/admin/agents/ast-001/listing", json={"tagline": "Cite the policy manual"}
            )

        assert resp.status_code == 200
        assert create.await_count == 1
        # The snapshot carries the admin's new text, not the record's old one.
        assert create.await_args.args[1].tagline == "Cite the policy manual"
        assert _no_writes.call_args.args[1].published_version == 7
        assert index.await_args_list[0].args[1] == 7

    def test_the_new_version_is_attributed_to_the_admin(self, app, _no_writes):
        """``createdBy`` is audit, never authorization — the author did not make this edit."""
        create = _no_writes.create_version
        with _loaded(_make_assistant(listing=_listing("published", publishedVersion=2))), _publisher():
            TestClient(app).patch("/admin/agents/ast-001/listing", json={"name": "Policy Finder"})

        assert create.await_args.args[1].created_by == "admin-001"

    def test_editing_an_unpublished_listing_cuts_nothing(self, app, _no_writes):
        """Nothing is being served, so there is nothing to re-bless. The draft just changes."""
        create, index = _no_writes.create_version, _no_writes.set_version_index
        with _loaded(_make_assistant(listing=_listing("in_review"))), _publisher():
            resp = TestClient(app).patch(
                "/admin/agents/ast-001/listing", json={"tagline": "A new subtitle"}
            )

        assert resp.status_code == 200
        create.assert_not_awaited()
        index.assert_not_awaited()


class TestListingsRowReportsTheLiveVersion:
    def test_a_published_row_names_the_version_the_store_serves(self, app):
        assistant = _make_assistant(listing=_listing("published", publishedVersion=3))
        by_state, publishers = _listing_rows(assistant)
        with by_state, publishers:
            resp = TestClient(app).get("/admin/agents/listings")

        assert resp.json()["listings"][0]["publishedVersion"] == 3

    def test_the_drift_marker_is_gone_rather_than_dormant(self, app):
        """A governance marker that can never fire is worse than none."""
        assistant = _make_assistant(listing=_listing("published", publishedVersion=3))
        by_state, publishers = _listing_rows(assistant)
        with by_state, publishers:
            resp = TestClient(app).get("/admin/agents/listings")

        assert "drift" not in resp.json()["listings"][0]
