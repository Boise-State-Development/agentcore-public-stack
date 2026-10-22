"""Admin API routes for the Agent Marketplace (Phases 1-2, 5).

Mounted under ``/admin/agents`` per the CLAUDE.md route convention — admin CRUD lives at
``/admin/resource/``, never ``/resource/admin/``. Every route is ``Depends(require_marketplace_scope)``
(= ``require_app_roles("system_admin")``); D2 is explicit that a granular "marketplace
curator" permission is a deliberate follow-up, so do not open a second permission axis here.

Covers five of D10's seven surfaces — **Review queue**, **Reports**, **Listings**,
**Categories** and **Store front** — plus the **Publishers** records they depend on.
Default pins live under ``/admin/roles/`` because the AppRole record is the source of
truth for a seed.
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from apis.app_api.agent_designer.services.listing_service import (
    ListingError,
    list_admin_listings,
    list_agent_versions,
    rollback_listing,
    patch_listing_presentation,
    review_listing,
    decide_withdrawal,
    diff_pending_version,
    read_submission_for_review,
    takedown_listing,
)
from apis.shared.assistants.categories import (
    category_in_use,
    delete_category,
    ensure_seeded,
    get_category,
    put_category,
)
from apis.app_api.agent_designer.services.report_service import (
    ReportError,
    list_agent_report_history,
    list_report_queue,
    open_report_count,
    triage_report,
)
from apis.app_api.agent_designer.services.store_service import resolve_featured
from apis.shared.assistants.models import (
    AdminListingPatchRequest,
    AdminListingsResponse,
    AdminQueueCountsResponse,
    AdminReportRow,
    AdminReportsResponse,
    AdminStoreFrontResponse,
    AdminSubmissionReview,
    AgentCategoriesResponse,
    AgentCategory,
    AgentCategoryCreateRequest,
    AgentCategoryUpdateRequest,
    AgentListing,
    AgentVersionDiffResponse,
    AgentVersionsResponse,
    PublisherCreateRequest,
    PublisherEligibilityRequest,
    PublisherEligibilityResponse,
    PublisherProfile,
    PublishersResponse,
    PublisherUpdateRequest,
    ResolveReportRequest,
    ReviewListingRequest,
    RollbackListingRequest,
    StoreFrontUpdateRequest,
    TakedownRequest,
    WithdrawalDecisionRequest,
)
from apis.shared.assistants.storefront import (
    MAX_FEATURED,
    get_featured_ids,
    put_featured_ids,
)
from apis.shared.assistants.publishers import (
    delete_publisher,
    get_publisher,
    list_eligibility,
    list_publishers,
    publisher_in_use,
    put_publisher,
    set_eligibility,
)
from apis.shared.auth import User, require_admin_scope
from apis.shared.feature_flags import agent_marketplace_enabled
from apis.shared.security.log_sanitize import scrub_log
from apis.shared.timestamps import utc_now_iso

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["admin-agent-marketplace"])

# Every route in this package is guarded by this one scope, so the
# permission boundary is the package boundary. Enforced by
# tests/architecture/test_admin_scope_coverage.py.
require_marketplace_scope = require_admin_scope("admin.marketplace")


def _now() -> str:
    return utc_now_iso()


async def require_marketplace_admin(admin: User = Depends(require_marketplace_scope)) -> User:
    """Admin auth + the environment kill switch (404 when off, so it reads as unmounted)."""
    if not agent_marketplace_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return admin


# ── review queue + listings (D10) ────────────────────────────────────────────────────
@router.get("/submissions", response_model=AdminListingsResponse)
async def list_submissions(admin: User = Depends(require_marketplace_admin)):
    """The Review queue: everything awaiting an admin decision (D2, §5.1).

    Submissions *and* withdrawal requests. One queue rather than two surfaces — §5.1 is
    explicit about that, and a second queue is one an admin has to remember exists.
    """
    try:
        rows, pending = await list_admin_listings(state="pending")
        return AdminListingsResponse(listings=rows, pending_count=pending)
    except Exception as e:
        logger.error(f"Error listing agent submissions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list submissions: {str(e)}")


@router.get("/listings", response_model=AdminListingsResponse)
async def list_listings(
    state: Optional[str] = Query(None, description="Filter by listing state"),
    admin: User = Depends(require_marketplace_admin),
):
    """The Listings table: every Agent that has ever been submitted (D10).

    ``updatedAt`` is on every row on purpose — D2 accepts that an approved listing can
    drift from what was reviewed, and the mitigation is this audit trail rather than a
    re-review gate that would make iteration miserable.
    """
    try:
        rows, pending = await list_admin_listings(state=state)
        return AdminListingsResponse(listings=rows, pending_count=pending)
    except Exception as e:
        logger.error(f"Error listing agent listings: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list listings: {str(e)}")


@router.get("/{agent_id}/submission", response_model=AdminSubmissionReview)
async def get_agent_submission_review(
    agent_id: str,
    admin: User = Depends(require_marketplace_admin),
):
    """The full reviewer read of a listing — instructions, capabilities, model (D2).

    The queue could name a submission but not show one. ``instructions`` is gated to
    owner/editor on ``GET /agents/{id}``, and that read refuses a non-owner outright when
    the Agent is PRIVATE — so the person deciding whether to publish could not read the
    system prompt or see what the Agent binds, and on a first submission the review diff
    (their only other window onto it) is empty by construction.

    Serves the **frozen snapshot**, not the live record. See ``AdminSubmissionReview`` for
    why that distinction is the design rather than an implementation detail: the live record
    is the author's draft, and approval promotes ``submittedVersion``.

    Deliberately a separate endpoint rather than a widened ``GET /agents/{id}``. That route
    is the store's detail read *and* the Agent Designer's form loader; teaching it an admin
    bypass would put an access exception on the busiest read in the feature, to serve the
    wrong version anyway.
    """
    try:
        return await read_submission_for_review(agent_id, admin)
    except ListingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        logger.error(f"Error reading submission for review: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to read submission: {str(e)}")


@router.get("/{agent_id}/diff", response_model=AgentVersionDiffResponse)
async def get_agent_review_diff(
    agent_id: str,
    admin: User = Depends(require_marketplace_admin),
):
    """What the pending submission changes against what is published (§6.1).

    The reviewer's actual question — "what changed since I approved this?" — which the queue
    could not answer before: a submission arrived with no reference to what it replaces, so
    a typo fix and a full rewrite looked identical and both got the same careful read.

    Admin-only for the same reason the review queue is: this returns ``instructions``, which
    the user-facing Agent read gates to owner/editor.
    """
    try:
        return await diff_pending_version(agent_id)
    except ListingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        logger.error(f"Error building agent review diff: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to build review diff: {str(e)}")


@router.post("/{agent_id}/review", response_model=AgentListing)
async def review_agent_listing(
    agent_id: str,
    request: ReviewListingRequest,
    admin: User = Depends(require_marketplace_admin),
):
    """Approve a submission, or return it with a reason (D2).

    Approving writes the sparse directory key and the Agent appears in the store
    immediately. Requesting changes requires a reason, which renders on the author's own
    card so they never have to ask what happened.
    """
    try:
        return await review_listing(
            agent_id,
            admin,
            decision=request.decision,
            note=request.note,
            category=request.category,
            publisher_id=request.publisher_id,
        )
    except ListingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        logger.error(f"Error reviewing agent listing: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to review listing: {str(e)}")


@router.post("/{agent_id}/takedown", response_model=AgentListing)
async def takedown_agent_listing(
    agent_id: str,
    request: TakedownRequest,
    admin: User = Depends(require_marketplace_admin),
):
    """Delist a published Agent, with a reason sent to the author (D2).

    A delisting, not a revocation: existing pins keep working, conversations underway keep
    running, and the Agent stays reachable by direct link — ``visibility`` is a separate
    axis. Clearing the directory key is all this does.
    """
    try:
        return await takedown_listing(agent_id, admin, request.reason)
    except ListingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        logger.error(f"Error taking down agent listing: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to take down listing: {str(e)}")


@router.post("/{agent_id}/withdrawal", response_model=AgentListing)
async def decide_agent_withdrawal(
    agent_id: str,
    request: WithdrawalDecisionRequest,
    admin: User = Depends(require_marketplace_admin),
):
    """Grant or decline an author's request to pull a live listing (§5.1).

    Deliberately **not** folded into ``POST /{agent_id}/review``. That endpoint answers "may
    this go into the store?"; this one answers "may this come out?", and the two decisions
    have different inputs, different notes and opposite defaults. One endpoint with four
    decision values would make an accidental unpublication a one-character mistake.

    ``grant`` → ``private`` and off the shelf. ``decline`` → back to whatever state the
    request came from (``withdrawal_from``), which restores nothing because the listing never
    stopped being live while the request was pending — that is the point of leaving the store
    index alone in ``withdrawal_requested``.
    """
    try:
        return await decide_withdrawal(
            agent_id, admin, decision=request.decision, note=request.note
        )
    except ListingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        logger.error(f"Error deciding agent withdrawal: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to decide withdrawal: {str(e)}")


@router.get("/{agent_id}/versions", response_model=AgentVersionsResponse)
async def list_agent_version_history(
    agent_id: str, admin: User = Depends(require_marketplace_admin),
):
    """Every snapshot this Agent has, newest first — the rollback picker's source (§8).

    Admin-only, like the diff and for the same reason: version *names* are harmless but this
    is the history of an Agent's approvals, and the surface that reads it is the one that can
    change what the store serves.
    """
    try:
        versions, published = await list_agent_versions(agent_id)
        return AgentVersionsResponse(versions=versions, published_version=published)
    except ListingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        logger.error(f"Error listing agent versions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list versions: {str(e)}")


@router.post("/{agent_id}/rollback", response_model=AgentListing)
async def rollback_agent_listing(
    agent_id: str,
    request: RollbackListingRequest,
    admin: User = Depends(require_marketplace_admin),
):
    """Repoint a published listing at an earlier snapshot (§8).

    The answer to "the approved version turned out to be wrong". Separate from ``/review``
    because it is not a review decision — no version is cut, nothing moves through the queue,
    and the listing stays ``published`` throughout. It only changes *which* approved artifact
    the store serves, which is why it can only act on a listing that is already published.
    """
    try:
        return await rollback_listing(
            agent_id, admin, version=request.version, reason=request.reason
        )
    except ListingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        logger.error(f"Error rolling back agent listing: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to roll back listing: {str(e)}")


@router.patch("/{agent_id}/listing", response_model=AgentListing)
async def patch_agent_listing(
    agent_id: str,
    request: AdminListingPatchRequest,
    admin: User = Depends(require_marketplace_admin),
):
    """Edit a listing's presentation — and only its presentation (D13).

    Accepts ``name``, ``tagline``, ``iconKey``, ``category``, ``publisherId``. Behavior
    fields (``instructions``, ``bindings``, ``modelConfig``, ``starters``, ``visibility``)
    are refused at the model boundary with a 422 naming the rule: an admin editing
    behavior would be responsible for something they did not write and cannot test.

    Every edit is appended to ``listing.adminEdits`` and surfaced to the author. Editing
    someone's listing quietly is how you lose authors.
    """
    try:
        return await patch_listing_presentation(agent_id, admin, request)
    except ListingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        logger.error(f"Error patching agent listing: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to patch listing: {str(e)}")


# ── problem reports (D15) ────────────────────────────────────────────────────────────
# The second work stream beside submissions. Everything here is admin-only for a reason
# stated once: **the reporter is visible to the admin and never to the author** (D15.2).
# Admins need identity to spot a brigade or a grudge; authors need the substance, not the
# name. Nothing in this section is reachable from a user-facing route.
@router.get("/reports", response_model=AdminReportsResponse)
async def list_agent_reports(admin: User = Depends(require_marketplace_admin)):
    """The Reports queue: every open report, oldest first within severity (D15).

    Reports are **triaged, never auto-forwarded to the author** (D15.1). Piping raw user
    text straight to the person who built the thing is how one bad message ends a
    volunteer's willingness to publish — and the author cannot act on "this is stupid"
    anyway. When a report is actionable the reviewer uses the existing **request changes**
    or **takedown** path, whose reason field is already the author-facing channel. Reports
    are the *evidence* for that reason, not a substitute for it.
    """
    try:
        rows, open_count = await list_report_queue()
        return AdminReportsResponse(reports=rows, open_count=open_count)
    except Exception as e:
        logger.error(f"Error listing agent reports: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list reports: {str(e)}")


@router.get("/queues", response_model=AdminQueueCountsResponse)
async def get_queue_counts(admin: User = Depends(require_marketplace_admin)):
    """The two counts D10 puts on the admin nav, without loading either queue.

    Its own route because the badges have to be right on *every* admin page: making the
    nav call ``/submissions`` and ``/reports`` to render two integers would put a table
    scan and a full row projection behind every click in the console.

    Fail-soft per half. A badge is orientation, not data — an unreachable count should
    show as zero rather than break the shell around a page that works.
    """
    pending = 0
    open_reports = 0
    try:
        _rows, pending = await list_admin_listings(state="in_review")
    except Exception:
        logger.warning("Failed to count pending submissions for the nav badge", exc_info=True)
    try:
        open_reports = await open_report_count()
    except Exception:
        logger.warning("Failed to count open reports for the nav badge", exc_info=True)
    return AdminQueueCountsResponse(pending_count=pending, open_report_count=open_reports)


@router.get("/{agent_id}/reports", response_model=AdminReportsResponse)
async def list_agent_report_history_endpoint(
    agent_id: str, admin: User = Depends(require_marketplace_admin)
):
    """Every report ever filed on one Agent, newest first.

    The context a reviewer wants before deciding whether one complaint is a pattern. Note
    the count returned here is this Agent's, not the queue's — it badges nothing.
    """
    try:
        rows = await list_agent_report_history(agent_id)
        return AdminReportsResponse(
            reports=rows, open_count=len([r for r in rows if r.state == "open"])
        )
    except Exception as e:
        logger.error(f"Error listing reports for agent {scrub_log(agent_id)}: {scrub_log(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list reports: {str(e)}")


@router.post("/{agent_id}/reports/{report_id}/resolve", response_model=AdminReportRow)
async def resolve_agent_report(
    agent_id: str,
    report_id: str,
    request: ResolveReportRequest,
    admin: User = Depends(require_marketplace_admin),
):
    """Resolve or dismiss a report (D15.5).

    ⚠️ **This never changes ``listing.state``.** A report has its own tiny lifecycle —
    ``open → resolved | dismissed`` — deliberately not a mirror of the listing state
    machine, because a report is a note *about* an Agent, not a state *of* it. If one
    warrants delisting, the admin uses ``POST /{agent_id}/takedown`` and that is a
    separate, recorded act with its own author-facing reason.

    The path carries ``agent_id`` because reports are child rows of the Agent — the spec's
    ``/reports/{reportId}/resolve`` would need a second index to locate the parent, which
    is exactly what D15's storage note declines to add.
    """
    try:
        return await triage_report(
            agent_id, report_id, admin, decision=request.decision, note=request.note
        )
    except ReportError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        logger.error(f"Error resolving report {scrub_log(report_id)}: {scrub_log(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to resolve report: {str(e)}")


# ── store front (D10) ────────────────────────────────────────────────────────────────
# The featured row deserves its own surface because **it is the only ranking lever that
# exists**: ``GSI5_SK`` is ``created_at``, so everything below Featured is newest-first and
# there is no popularity sort. Promotion is how a good Agent gets found.
@router.get("/storefront", response_model=AdminStoreFrontResponse)
async def get_store_front(admin: User = Depends(require_marketplace_admin)):
    """The featured row, resolved in its configured order.

    ``unavailable`` names configured ids that no longer resolve as published listings —
    a taken-down or deleted Agent. They are reported rather than pruned, because a GET
    that silently rewrote an admin's curation would also make a reversed takedown
    permanently cost the Agent its slot.
    """
    try:
        featured, unavailable = await resolve_featured(await get_featured_ids())
        return AdminStoreFrontResponse(featured=featured, unavailable=unavailable)
    except Exception as e:
        logger.error(f"Error loading admin store front: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to load store front: {str(e)}")


@router.put("/storefront", response_model=AdminStoreFrontResponse)
async def put_store_front(
    request: StoreFrontUpdateRequest, admin: User = Depends(require_marketplace_admin)
):
    """Replace the featured row, in order (D10).

    A whole-list PUT: the row is short and reordering must be atomic, so the ordered array
    is the record. Every id must resolve to a **published** listing — promoting something
    the store cannot show would put a tile at the top of Discover that nobody can open —
    and the refusal names the offending ids rather than reporting a count.
    """
    try:
        if len(request.agent_ids) > MAX_FEATURED:
            raise HTTPException(
                status_code=400,
                detail=f"The store front holds at most {MAX_FEATURED} agents.",
            )

        _rows, unavailable = await resolve_featured(request.agent_ids)
        if unavailable:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Only published agents can be featured. These are not published: "
                    + ", ".join(unavailable)
                ),
            )

        saved = await put_featured_ids(request.agent_ids, updated_by=admin.user_id)
        featured, still_unavailable = await resolve_featured(saved)
        return AdminStoreFrontResponse(featured=featured, unavailable=still_unavailable)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error saving admin store front: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to save store front: {str(e)}")


# ── categories (D10) ─────────────────────────────────────────────────────────────────
# Admin-managed records rather than a build-time constant: a category set that requires a
# deploy to change will not be maintained. ``ensure_seeded`` writes the defaults on the
# first read in a fresh environment so the store is never category-less.
@router.get("/categories", response_model=AgentCategoriesResponse)
async def list_agent_categories(admin: User = Depends(require_marketplace_admin)):
    """The category set a listing may reference, in browse order."""
    try:
        return AgentCategoriesResponse(categories=await ensure_seeded())
    except Exception as e:
        logger.error(f"Error listing agent categories: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list categories: {str(e)}")


@router.post("/categories", response_model=AgentCategory, status_code=201)
async def create_agent_category(
    request: AgentCategoryCreateRequest, admin: User = Depends(require_marketplace_admin)
):
    """Create a category.

    The id defaults to the label and is **immutable** thereafter — it is half of
    ``GSI5_PK = LISTED#{category}``, so changing it would strand every listing in that
    category in a partition browse no longer queries. Rename the label instead.
    """
    try:
        await ensure_seeded()
        category_id = (request.id or request.label).strip()
        if not category_id:
            raise HTTPException(status_code=400, detail="Category id cannot be empty")
        if await get_category(category_id):
            raise HTTPException(status_code=409, detail=f"Category '{category_id}' already exists")

        now = _now()
        return await put_category(
            AgentCategory(
                id=category_id,
                label=request.label,
                order=request.order,
                enabled=request.enabled,
                created_at=now,
                updated_at=now,
            )
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating agent category: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to create category: {str(e)}")


@router.patch("/categories/{category_id}", response_model=AgentCategory)
async def update_agent_category(
    category_id: str,
    request: AgentCategoryUpdateRequest,
    admin: User = Depends(require_marketplace_admin),
):
    """Rename, reorder, or disable a category. The id cannot change (see create)."""
    try:
        existing = await get_category(category_id)
        if not existing:
            raise HTTPException(status_code=404, detail=f"Category not found: {category_id}")
        changes = request.model_dump(exclude_none=True)
        return await put_category(existing.model_copy(update={**changes, "updated_at": _now()}))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating agent category: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to update category: {str(e)}")


@router.delete("/categories/{category_id}", status_code=204)
async def delete_agent_category(
    category_id: str, admin: User = Depends(require_marketplace_admin)
):
    """Delete a category, but only while nothing references it.

    A referenced category cannot be deleted because its listings would point at a
    partition with no label to render. Disable it instead: it leaves the pickers and the
    browse header while its listings keep working — which is almost always what the admin
    actually meant.
    """
    try:
        if not await get_category(category_id):
            raise HTTPException(status_code=404, detail=f"Category not found: {category_id}")
        if await category_in_use(category_id):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"'{category_id}' still has listings in it. Disable it instead — that "
                    "removes it from the pickers and the browse header while the agents "
                    "already in it keep working."
                ),
            )
        await delete_category(category_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting agent category: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to delete category: {str(e)}")


# ── publishers (D12) ─────────────────────────────────────────────────────────────────
@router.get("/publishers", response_model=PublishersResponse)
async def list_publisher_profiles(admin: User = Depends(require_marketplace_admin)):
    """Every publisher profile, ordered."""
    try:
        return PublishersResponse(publishers=await list_publishers())
    except Exception as e:
        logger.error(f"Error listing publishers: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list publishers: {str(e)}")


@router.post("/publishers", response_model=PublisherProfile, status_code=201)
async def create_publisher_profile(
    request: PublisherCreateRequest, admin: User = Depends(require_marketplace_admin)
):
    """Create a publisher profile.

    ``verified`` is settable only here and on update — it is the admin-only mark meaning
    "a university team stands behind this", which is why individual profiles (auto-created
    at submission) are never created verified.
    """
    try:
        now = _now()
        profile = PublisherProfile(
            id=f"pub-{uuid.uuid4().hex[:12]}",
            label=request.label,
            kind=request.kind,
            verified=request.verified,
            icon_key=request.icon_key,
            order=request.order,
            enabled=request.enabled,
            created_at=now,
            updated_at=now,
        )
        return await put_publisher(profile)
    except Exception as e:
        logger.error(f"Error creating publisher: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to create publisher: {str(e)}")


@router.patch("/publishers/{publisher_id}", response_model=PublisherProfile)
async def update_publisher_profile(
    publisher_id: str,
    request: PublisherUpdateRequest,
    admin: User = Depends(require_marketplace_admin),
):
    """Update a publisher profile, including the admin-only ``verified`` mark."""
    try:
        existing = await get_publisher(publisher_id)
        if not existing:
            raise HTTPException(status_code=404, detail=f"Publisher not found: {publisher_id}")
        changes = request.model_dump(exclude_none=True)
        updated = existing.model_copy(update={**changes, "updated_at": _now()})
        return await put_publisher(updated)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating publisher: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to update publisher: {str(e)}")


@router.delete("/publishers/{publisher_id}", status_code=204)
async def delete_publisher_profile(
    publisher_id: str, admin: User = Depends(require_marketplace_admin)
):
    """Delete a publisher profile, but only while nothing is attributed to it.

    Same rule and same wording as categories: a referenced profile cannot be deleted,
    because listings store the *id* and would be left pointing at nothing. The admin
    Listings table renders those as "Unattributed", and there is no surface for putting the
    attribution back — so the delete is not a visible gap to repair, it is silent data loss
    across every listing that named it, live ones included.

    Disable instead. That drops it from the submit picker while existing attributions keep
    rendering, which is nearly always what was meant.

    Deleting an attribution never changes who can run an Agent — publisher is display only
    (D12). This guard is about not stranding the display, not about access.
    """
    try:
        if not await get_publisher(publisher_id):
            raise HTTPException(status_code=404, detail=f"Publisher not found: {publisher_id}")
        if await publisher_in_use(publisher_id):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Listings are still attributed to this publisher. Disable it instead — "
                    "that removes it from the submit picker while the listings already "
                    "credited to it keep rendering."
                ),
            )
        await delete_publisher(publisher_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting publisher: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to delete publisher: {str(e)}")


@router.get("/publishers/{publisher_id}/eligibility", response_model=PublisherEligibilityResponse)
async def get_publisher_eligibility(
    publisher_id: str, admin: User = Depends(require_marketplace_admin)
):
    """Who may *propose* this publisher at submission (D12)."""
    try:
        if not await get_publisher(publisher_id):
            raise HTTPException(status_code=404, detail=f"Publisher not found: {publisher_id}")
        return PublisherEligibilityResponse(
            publisher_id=publisher_id, user_ids=await list_eligibility(publisher_id)
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reading publisher eligibility: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to read eligibility: {str(e)}")


@router.put("/publishers/{publisher_id}/eligibility", response_model=PublisherEligibilityResponse)
async def put_publisher_eligibility(
    publisher_id: str,
    request: PublisherEligibilityRequest,
    admin: User = Depends(require_marketplace_admin),
):
    """Replace the set of users who may propose this publisher (D12).

    A *proposal* allowlist for the submit dialog and nothing more. An admin may attribute
    any listing to any publisher regardless of eligibility, so this list never appears in
    an access check — it decides what an author is offered, not what anyone may run.
    """
    try:
        if not await get_publisher(publisher_id):
            raise HTTPException(status_code=404, detail=f"Publisher not found: {publisher_id}")
        user_ids = await set_eligibility(publisher_id, request.user_ids)
        return PublisherEligibilityResponse(publisher_id=publisher_id, user_ids=user_ids)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting publisher eligibility: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to set eligibility: {str(e)}")
