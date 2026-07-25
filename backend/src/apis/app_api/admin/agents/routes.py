"""Admin API routes for the Agent Marketplace (Phases 1-2).

Mounted under ``/admin/agents`` per the CLAUDE.md route convention — admin CRUD lives at
``/admin/resource/``, never ``/resource/admin/``. Every route is ``Depends(require_admin)``
(= ``require_app_roles("system_admin")``); D2 is explicit that a granular "marketplace
curator" permission is a deliberate follow-up, so do not open a second permission axis here.

Covers three of D10's seven surfaces — **Review queue**, **Listings** and **Categories**
— plus the **Publishers** records they depend on. Store front, default pins and the
reports queue are Phases 5, 6 and 8.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from apis.app_api.agent_designer.services.listing_service import (
    ListingError,
    list_admin_listings,
    patch_listing_presentation,
    review_listing,
    takedown_listing,
)
from apis.shared.assistants.categories import (
    category_in_use,
    delete_category,
    ensure_seeded,
    get_category,
    put_category,
)
from apis.shared.assistants.models import (
    AdminListingPatchRequest,
    AdminListingsResponse,
    AgentCategoriesResponse,
    AgentCategory,
    AgentCategoryCreateRequest,
    AgentCategoryUpdateRequest,
    AgentListing,
    PublisherCreateRequest,
    PublisherEligibilityRequest,
    PublisherEligibilityResponse,
    PublisherProfile,
    PublishersResponse,
    PublisherUpdateRequest,
    ReviewListingRequest,
    TakedownRequest,
)
from apis.shared.assistants.publishers import (
    delete_publisher,
    get_publisher,
    list_eligibility,
    list_publishers,
    put_publisher,
    set_eligibility,
)
from apis.shared.auth import User, require_admin
from apis.shared.feature_flags import agent_marketplace_enabled

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["admin-agent-marketplace"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat() + "Z"


async def require_marketplace_admin(admin: User = Depends(require_admin)) -> User:
    """Admin auth + the environment kill switch (404 when off, so it reads as unmounted)."""
    if not agent_marketplace_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return admin


# ── review queue + listings (D10) ────────────────────────────────────────────────────
@router.get("/submissions", response_model=AdminListingsResponse)
async def list_submissions(admin: User = Depends(require_marketplace_admin)):
    """The Review queue: every submission awaiting a decision (D2)."""
    try:
        rows, pending = await list_admin_listings(state="in_review")
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
    """Delete a publisher profile and its eligibility items.

    Listings already attributed to it keep the id; the admin Listings table renders them
    without a resolved publisher so the gap is visible and can be reassigned. Deleting an
    attribution must never change who can run an Agent (D12).
    """
    try:
        if not await get_publisher(publisher_id):
            raise HTTPException(status_code=404, detail=f"Publisher not found: {publisher_id}")
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
