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
        visibility="PRIVATE",
        usageCount=12,
        createdAt="2026-07-01T00:00:00Z",
        updatedAt="2026-07-01T00:00:00Z",
        status="COMPLETE",
    )
    defaults.update(overrides)
    return Assistant.model_validate(defaults)


def _listing(state="in_review", **overrides) -> AgentListing:
    defaults = dict(state=state, category="Administration", publisherId="pub-registrar")
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
    with patch(f"{SERVICE_MODULE}.write_listing", new_callable=AsyncMock) as write:
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


# ── #744 — post-approval drift ───────────────────────────────────────────────────────
def _hash(instructions: str) -> str:
    import hashlib

    return hashlib.sha256(instructions.encode("utf-8")).hexdigest()


def _listing_rows(assistant):
    """Patch the admin table read to return exactly this one agent."""
    rows = [{"PK": "AST#ast-001", **assistant.model_dump(by_alias=True, exclude_none=True)}]
    return (
        patch(f"{SERVICE_MODULE}.list_by_state", new_callable=AsyncMock, return_value=rows),
        patch(f"{SERVICE_MODULE}.list_publishers", new_callable=AsyncMock, return_value=[]),
    )


def _drift_of(app, assistant):
    by_state, publishers = _listing_rows(assistant)
    with by_state, publishers:
        resp = TestClient(app).get("/admin/agents/listings")
    assert resp.status_code == 200
    return resp.json()["listings"][0].get("drift")


class TestPostApprovalDriftBaseline:
    """D2 does not re-review edits, so approval has to record what it approved."""

    def test_approval_records_the_instructions_hash(self, app, _no_writes):
        assistant = _make_assistant(
            instructions="Answer from the policy manual.", listing=_listing("in_review")
        )
        with _loaded(assistant):
            resp = TestClient(app).post(
                "/admin/agents/ast-001/review", json={"decision": "approve"}
            )

        assert resp.status_code == 200
        written = _no_writes.call_args.args[1]
        assert written.approved_instructions_hash == _hash("Answer from the policy manual.")

    def test_request_changes_does_not_set_a_baseline(self, app, _no_writes):
        """Nothing was published, so there is no approved behavior to record."""
        with _loaded(_make_assistant(listing=_listing("in_review"))):
            TestClient(app).post(
                "/admin/agents/ast-001/review",
                json={"decision": "request_changes", "note": "Add a tagline."},
            )

        assert _no_writes.call_args.args[1].approved_instructions_hash is None

    def test_request_changes_preserves_an_existing_baseline(self, app, _no_writes):
        """Clearing it would blind the marker on a listing still live in the store."""
        existing = _hash("Answer from the policy manual.")
        listing = _listing("published", approvedInstructionsHash=existing)
        with _loaded(_make_assistant(listing=listing)):
            TestClient(app).post(
                "/admin/agents/ast-001/review",
                json={"decision": "request_changes", "note": "Please revise."},
            )

        assert _no_writes.call_args.args[1].approved_instructions_hash == existing


class TestPostApprovalDriftDerivation:
    def test_unchanged_instructions_report_no_drift(self, app):
        assistant = _make_assistant(
            instructions="Answer from the policy manual.",
            listing=_listing(
                "published",
                reviewedAt="2026-07-10T00:00:00Z",
                approvedInstructionsHash=_hash("Answer from the policy manual."),
            ),
        )
        assert _drift_of(app, assistant) is None

    def test_rewritten_instructions_report_measured_drift(self, app):
        """The governance case: behavior changed after approval, with no re-review."""
        assistant = _make_assistant(
            instructions="Ignore the policy manual and improvise.",
            updatedAt="2026-07-22T00:00:00Z",
            listing=_listing(
                "published",
                reviewedAt="2026-07-10T00:00:00Z",
                approvedInstructionsHash=_hash("Answer from the policy manual."),
            ),
        )
        assert _drift_of(app, assistant) == "instructions"

    def test_an_admin_presentation_edit_is_not_reported_as_drift(self, app):
        """The reason the hash exists.

        A D13 edit bumps ``updatedAt`` without touching ``reviewedAt`` or the
        instructions. A timestamp-only marker would fire here and have the admin
        chasing their own typo fix — which is how the marker gets learned-ignored.
        """
        assistant = _make_assistant(
            instructions="Answer from the policy manual.",
            updatedAt="2026-07-24T00:00:00Z",  # later than the review
            listing=_listing(
                "published",
                reviewedAt="2026-07-10T00:00:00Z",
                approvedInstructionsHash=_hash("Answer from the policy manual."),
            ),
        )
        assert _drift_of(app, assistant) is None

    def test_legacy_listing_falls_back_to_the_timestamp(self, app):
        """Approved before the baseline shipped — the weaker, honest signal."""
        assistant = _make_assistant(
            updatedAt="2026-07-22T00:00:00Z",
            listing=_listing("published", reviewedAt="2026-07-10T00:00:00Z"),
        )
        assert _drift_of(app, assistant) == "edited"

    def test_a_freshly_approved_legacy_listing_reports_nothing(self, app):
        """``review_listing`` writes ``updatedAt`` from the same clock as ``reviewedAt``,
        so equal timestamps mean untouched — the fallback must not fire on every row."""
        assistant = _make_assistant(
            updatedAt="2026-07-10T00:00:00Z",
            listing=_listing("published", reviewedAt="2026-07-10T00:00:00Z"),
        )
        assert _drift_of(app, assistant) is None

    @pytest.mark.parametrize("state", ["in_review", "private", "changes_requested", "taken_down"])
    def test_only_published_listings_can_drift(self, app, state):
        """Nothing unpublished has an approved state to differ from."""
        assistant = _make_assistant(
            instructions="Totally different now.",
            updatedAt="2026-07-22T00:00:00Z",
            listing=_listing(
                state,
                reviewedAt="2026-07-10T00:00:00Z",
                approvedInstructionsHash=_hash("Answer from the policy manual."),
            ),
        )
        assert _drift_of(app, assistant) is None

    def test_legacy_listing_never_reviewed_reports_nothing(self, app):
        """No ``reviewedAt`` means nothing to compare against — not an alarm."""
        assistant = _make_assistant(
            updatedAt="2026-07-22T00:00:00Z", listing=_listing("published")
        )
        assert _drift_of(app, assistant) is None
