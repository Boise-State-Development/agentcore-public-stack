"""Agent Designer — the ``/agents/*`` surface (Phase 1, PR-3).

A thin alias over the evolved assistant store: the same ``apis.shared.assistants``
service functions and the same identity-based access gates as ``/assistants/*``, but
returning the **Agent** shape (``compat.to_agent_view`` → ``AgentResponse``) so callers
see ``modelConfig`` + ``bindings``. Legacy ids are valid unchanged (agentId == assistantId).

Gating: the router is mounted unconditionally but every route depends on
``require_agents_enabled`` — a 404 when ``AGENTS_API_ENABLED`` is off, so the surface
behaves as if unmounted while the feature ships incrementally. Auth is the standard SPA
cookie dependency per the CLAUDE.md app-api rule.

Deliberately excluded in Phase 1: ``test-chat`` (the only reason the assistants router is
on the architecture import-boundary allow-list — aliasing it would force a second
exception) and document sub-routes. Those stay on ``/assistants/*``. ``GET /agents`` lists
the caller's own + shared-with-them agents; ``include_public``/pagination parity is a
Phase-4 concern when the Designer consumes it.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from apis.app_api.agent_designer.services.bindable_catalog import (
    BINDABLE_KINDS,
    list_bindable,
)
from apis.app_api.agent_designer.services.agent_detail import (
    resolve_capabilities,
    resolve_listing_display,
    resolve_runnability,
)
from apis.app_api.agent_designer.services.binding_validation import (
    BindingValidationError,
    validate_agent_write,
)
from apis.shared.assistants.compat import to_agent_view
from apis.shared.assistants.models import (
    AgentResponse,
    AgentRunnabilityResponse,
    AgentSharesResponse,
    AgentsListResponse,
    BindableListResponse,
    CreateAssistantDraftRequest,
    CreateAssistantRequest,
    ShareAssistantRequest,
    ShareEntry,
    UnshareAssistantRequest,
    UpdateAssistantRequest,
    UpdateSharePermissionRequest,
)
from apis.shared.assistants.service import (
    assistant_exists,
    create_assistant,
    create_assistant_draft,
    delete_assistant,
    get_assistant_with_access_check,
    list_assistant_shares,
    list_shared_with_user,
    list_user_assistants,
    resolve_assistant_permission,
    share_assistant,
    unshare_assistant,
    update_assistant,
    update_share_permission,
)
from apis.app_api.agent_designer.services.listing_service import (
    ListingError,
    submit_listing,
    withdraw_listing,
)
from apis.app_api.agent_designer.services.store_service import (
    browse_all,
    browse_category,
    store_front,
)
from apis.shared.assistants.models import (
    AgentListing,
    AgentStoreFrontResponse,
    AgentStoreResponse,
    ListingSubmissionResponse,
    SubmitListingRequest,
)
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.feature_flags import agent_marketplace_enabled, agents_enabled

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


async def require_agents_enabled(
    user: User = Depends(get_current_user_from_session),
) -> User:
    """Cookie auth + the environment kill switch.

    404 when ``AGENTS_API_ENABLED`` is off, so the surface behaves as if unmounted
    (mirrors the memory-spaces / schedules pattern).
    """
    if not agents_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return user


async def require_marketplace_enabled(
    user: User = Depends(require_agents_enabled),
) -> User:
    """Cookie auth + both kill switches, for the marketplace listing routes.

    The marketplace is a surface over the Agent record, so it is off whenever the Agent
    surface itself is off. 404 rather than 403 so the routes behave as if unmounted.
    """
    if not agent_marketplace_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return user


# Permissions that may read an Agent's ``instructions`` (Marketplace Phase 3).
# ⚠️ Behaviour change: this used to be returned to any PUBLIC viewer. Under link-sharing
# that exposure was bounded by who had the link; under a store, "PUBLIC" means the whole
# institution can browse to it, so the system prompt is gated to the people who may edit
# it. Viewers get ``capabilities`` — names, not behaviour — instead.
INSTRUCTIONS_PERMISSIONS = ("owner", "editor")


def _agent_response(assistant, *, permission: Optional[str] = None,
                    is_shared_with_me: Optional[bool] = None) -> AgentResponse:
    """Project an Assistant into the Agent read-shape, layering share metadata."""
    view = to_agent_view(assistant)
    if permission not in INSTRUCTIONS_PERMISSIONS:
        # Dropped rather than blanked: every route here serves the model with
        # ``response_model_exclude_none``, so the key is simply absent for a viewer.
        view.pop("instructions", None)
    if permission is not None:
        view["userPermission"] = permission
    if is_shared_with_me is not None:
        view["isSharedWithMe"] = is_shared_with_me
        view["firstInteracted"] = getattr(assistant, "first_interacted", None)
    return AgentResponse.model_validate(view)


def _shares_response(agent_id: str, shares: list) -> AgentSharesResponse:
    return AgentSharesResponse(
        agent_id=agent_id,
        shared_with=[ShareEntry.model_validate(s) for s in shares],
    )


# --------------------------------------------------------------------------- CRUD
@router.post("/draft", response_model=AgentResponse, response_model_exclude_none=True)
async def create_agent_draft_endpoint(
    request: CreateAssistantDraftRequest, current_user: User = Depends(require_agents_enabled)
):
    """Create a draft Agent with an auto-generated id (status=DRAFT)."""
    try:
        assistant = await create_assistant_draft(
            owner_id=current_user.user_id, owner_name=current_user.name, name=request.name
        )
        return _agent_response(assistant, permission="owner")
    except Exception as e:
        logger.error(f"Error creating draft agent: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to create draft agent: {str(e)}")


@router.post("", response_model=AgentResponse, response_model_exclude_none=True)
async def create_agent_endpoint(
    request: CreateAssistantRequest, current_user: User = Depends(require_agents_enabled)
):
    """Create a complete Agent (status=COMPLETE) with optional bindings + modelConfig."""
    try:
        await validate_agent_write(
            current_user, bindings=request.bindings, model_settings=request.model_settings
        )
    except BindingValidationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)

    try:
        assistant = await create_assistant(
            owner_id=current_user.user_id,
            owner_name=current_user.name,
            name=request.name,
            description=request.description,
            instructions=request.instructions,
            visibility=request.visibility,
            tags=request.tags,
            starters=request.starters,
            emoji=request.emoji,
            bindings=request.bindings,
            model_settings=request.model_settings,
        )
        return _agent_response(assistant, permission="owner")
    except Exception as e:
        logger.error(f"Error creating agent: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to create agent: {str(e)}")


@router.get("", response_model=AgentsListResponse, response_model_exclude_none=True)
async def list_agents_endpoint(
    include_drafts: bool = False, current_user: User = Depends(require_agents_enabled)
):
    """List the caller's own Agents plus those shared with them (most-recent first)."""
    try:
        owned, _ = await list_user_assistants(
            owner_id=current_user.user_id, include_drafts=include_drafts, include_public=False
        )
        owned_ids = {a.assistant_id for a in owned}
        shared = [a for a in await list_shared_with_user(current_user.email) if a.assistant_id not in owned_ids]

        agents = [_agent_response(a, permission="owner", is_shared_with_me=False) for a in owned]
        agents += [
            _agent_response(a, permission=getattr(a, "user_permission", None), is_shared_with_me=True)
            for a in shared
        ]
        agents.sort(key=lambda a: a.created_at, reverse=True)
        return AgentsListResponse(agents=agents, next_token=None)
    except Exception as e:
        logger.error(f"Error listing agents: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list agents: {str(e)}")


# --------------------------------------------------------------------------- store (D4)
# Declared BEFORE ``/{agent_id}`` so the literal paths are not captured by the path param.
@router.get("/store/front", response_model=AgentStoreFrontResponse)
async def agent_store_front_endpoint(current_user: User = Depends(require_marketplace_enabled)):
    """The browse header: the featured row plus the categories to render (D10).

    ``featured`` is empty until the store-front admin ships in Phase 5; the field is
    present now so the SPA contract does not change when it fills in.
    """
    try:
        featured, categories = await store_front()
        return AgentStoreFrontResponse(featured=featured, categories=categories)
    except Exception as e:
        logger.error(f"Error loading agent store front: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to load store front: {str(e)}")


@router.get("/store", response_model=AgentStoreResponse)
async def agent_store_endpoint(
    category: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(require_marketplace_enabled),
):
    """Browse published Agents, newest-first (D4).

    A pure sparse-GSI5 read: it cannot return an unpublished Agent because an unpublished
    Agent has no key in the index. The response carries icon, name, tagline, publisher and
    category — no ``instructions``, no binding refs, no owner id.

    ``cursor`` paginates within a single ``category`` (one partition). Without a category
    the whole store is merged newest-first and no cursor is returned — see
    ``store_service.browse_all`` for why a half-cursor would be worse than none.
    """
    try:
        if category:
            listings, next_cursor = await browse_category(category, limit=limit, cursor=cursor)
            return AgentStoreResponse(listings=listings, next_cursor=next_cursor)
        return AgentStoreResponse(listings=await browse_all(limit=limit), next_cursor=None)
    except Exception as e:
        logger.error(f"Error browsing agent store: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to browse the store: {str(e)}")


# --------------------------------------------------------------------------- palette
# Declared BEFORE ``/{agent_id}`` so the literal path is not captured by the path param.
@router.get("/bindable", response_model=BindableListResponse)
async def list_bindable_endpoint(
    kind: str, current_user: User = Depends(require_agents_enabled)
):
    """RBAC-filtered catalog of bindable primitives of ``kind`` for the caller (D4).

    The Designer palette: each picker fetches ``?kind=model|tool|skill|knowledge_base|
    memory_space`` and shows only what the user's role enables. Composes the existing
    per-primitive access services (see ``bindable_catalog``).
    """
    if kind not in BINDABLE_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported bindable kind '{kind}'. Expected one of: {', '.join(BINDABLE_KINDS)}.",
        )
    try:
        items = await list_bindable(kind, current_user)
        return BindableListResponse(kind=kind, items=items)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error listing bindable '{kind}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list bindable '{kind}': {str(e)}")


@router.get("/{agent_id}", response_model=AgentResponse, response_model_exclude_none=True)
async def get_agent_endpoint(agent_id: str, current_user: User = Depends(require_agents_enabled)):
    """Retrieve an Agent by id with visibility-based access control.

    This is the marketplace **detail read** (Phase 3). Two things beyond Phase 1:

    * ``instructions`` is gated to owner/editor — see ``INSTRUCTIONS_PERMISSIONS``.
    * ``capabilities`` + ``modelLabel`` are resolved, so the detail page can say what the
      Agent reaches by *name* rather than making the SPA dereference binding refs.

    Capability resolution is best-effort: it is presentation, and a catalog hiccup should
    not turn a readable Agent into a 500. The list route does not resolve them at all.
    """
    try:
        if not await assistant_exists(agent_id):
            raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")
        assistant, permission = await get_assistant_with_access_check(
            assistant_id=agent_id, user_id=current_user.user_id, user_email=current_user.email
        )
        if not assistant:
            raise HTTPException(status_code=403, detail="Access denied: you do not have permission to access this agent")

        response = _agent_response(assistant, permission=permission)
        try:
            capabilities, model_label = await resolve_capabilities(assistant, current_user)
            response.capabilities = capabilities
            response.model_label = model_label
            response.publisher, response.category_label = await resolve_listing_display(assistant)
        except Exception:
            logger.warning(f"Failed to resolve capabilities for agent {agent_id}", exc_info=True)
        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving agent: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to retrieve agent: {str(e)}")


@router.get("/{agent_id}/runnability", response_model=AgentRunnabilityResponse)
async def get_agent_runnability_endpoint(
    agent_id: str, current_user: User = Depends(require_marketplace_enabled)
):
    """Will this Agent run for the requesting user? (D6)

    Resolves the Agent's ``modelConfig`` + ``bindings`` against the **viewer's** own
    RBAC-filtered ``/agents/bindable`` results and answers ``ready`` / ``limits`` /
    ``blocked``, naming what is missing. Access-gated exactly like the detail read: you
    can only ask about an Agent you can already see.

    D6's stated cost: with no badge on the shelf (D4), a user only learns an Agent will
    not run for them after tapping into it. This route is where that lands.
    """
    try:
        if not await assistant_exists(agent_id):
            raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")
        assistant, permission = await get_assistant_with_access_check(
            assistant_id=agent_id, user_id=current_user.user_id, user_email=current_user.email
        )
        if not assistant:
            raise HTTPException(status_code=403, detail="Access denied: you do not have permission to access this agent")
        return await resolve_runnability(assistant, current_user)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resolving agent runnability: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to resolve runnability: {str(e)}")


@router.put("/{agent_id}", response_model=AgentResponse, response_model_exclude_none=True)
async def update_agent_endpoint(
    agent_id: str, request: UpdateAssistantRequest, current_user: User = Depends(require_agents_enabled)
):
    """Update an Agent (owner or editor); visibility changes are owner-only."""
    try:
        assistant, permission = await resolve_assistant_permission(
            assistant_id=agent_id, user_id=current_user.user_id, user_email=current_user.email
        )
        if not assistant:
            raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")
        if permission not in ("owner", "editor"):
            raise HTTPException(status_code=403, detail="You do not have permission to edit this agent")
        if permission == "editor" and request.visibility is not None and request.visibility != assistant.visibility:
            raise HTTPException(status_code=400, detail="Only the owner can change agent visibility")

        try:
            await validate_agent_write(
                current_user, bindings=request.bindings, model_settings=request.model_settings
            )
        except BindingValidationError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message)

        updated = await update_assistant(
            assistant_id=agent_id,
            owner_id=assistant.owner_id,
            name=request.name,
            description=request.description,
            instructions=request.instructions,
            visibility=request.visibility,
            tags=request.tags,
            starters=request.starters,
            emoji=request.emoji,
            status=request.status,
            image_url=request.image_url,
            bindings=request.bindings,
            model_settings=request.model_settings,
        )
        if not updated:
            raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")
        return _agent_response(updated, permission=permission)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating agent: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to update agent: {str(e)}")


@router.delete("/{agent_id}", status_code=204)
async def delete_agent_endpoint(agent_id: str, current_user: User = Depends(require_agents_enabled)):
    """Delete an Agent (owner only)."""
    try:
        deleted = await delete_assistant(agent_id, current_user.user_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting agent: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to delete agent: {str(e)}")


# --------------------------------------------------------------------------- shares
# The share mutations return a bool (False → not owned / not found); we then read the
# updated list. Mirrors the /assistants/{id}/shares handlers exactly (same records).
@router.post("/{agent_id}/shares", response_model=AgentSharesResponse)
async def share_agent_endpoint(
    agent_id: str, request: ShareAssistantRequest, current_user: User = Depends(require_agents_enabled)
):
    """Share an Agent with emails at a permission level (owner only)."""
    user_id = current_user.user_id
    try:
        if not await share_assistant(agent_id, user_id, request.emails, request.permission):
            raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")
        return _shares_response(agent_id, await list_assistant_shares(agent_id, user_id))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sharing agent: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to share agent: {str(e)}")


@router.delete("/{agent_id}/shares", response_model=AgentSharesResponse)
async def unshare_agent_endpoint(
    agent_id: str, request: UnshareAssistantRequest, current_user: User = Depends(require_agents_enabled)
):
    """Remove shares from an Agent (owner only)."""
    user_id = current_user.user_id
    try:
        if not await unshare_assistant(agent_id, user_id, request.emails):
            raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")
        return _shares_response(agent_id, await list_assistant_shares(agent_id, user_id))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error unsharing agent: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to modify shares for agent: {str(e)}")


@router.patch("/{agent_id}/shares", response_model=AgentSharesResponse)
async def update_agent_share_endpoint(
    agent_id: str, request: UpdateSharePermissionRequest, current_user: User = Depends(require_agents_enabled)
):
    """Change an existing share's permission level (owner only)."""
    user_id = current_user.user_id
    try:
        if not await update_share_permission(agent_id, user_id, request.email, request.permission):
            raise HTTPException(status_code=404, detail="Agent or share record not found")
        return _shares_response(agent_id, await list_assistant_shares(agent_id, user_id))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating agent share permission: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to update share permission: {str(e)}")


@router.get("/{agent_id}/shares", response_model=AgentSharesResponse)
async def get_agent_shares_endpoint(agent_id: str, current_user: User = Depends(require_agents_enabled)):
    """List an Agent's share records (owners and editors may read)."""
    try:
        assistant, permission = await resolve_assistant_permission(
            assistant_id=agent_id, user_id=current_user.user_id, user_email=current_user.email
        )
        if not assistant:
            raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")
        if permission not in ("owner", "editor"):
            raise HTTPException(status_code=403, detail="You do not have permission to view shares for this agent")
        return _shares_response(agent_id, await list_assistant_shares(agent_id, assistant.owner_id))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting agent shares: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get agent shares: {str(e)}")


# ------------------------------------------------------------------- marketplace (D2)
# The author's half of the listing lifecycle. The reviewer's half lives under
# /admin/agents/* behind require_admin. Nothing here is user-visible in Phase 1 — the
# SPA surfaces that call these ship with the Discover page in Phase 2.
@router.post("/{agent_id}/listing/submit", response_model=ListingSubmissionResponse)
async def submit_agent_listing_endpoint(
    agent_id: str,
    request: SubmitListingRequest,
    current_user: User = Depends(require_marketplace_enabled),
):
    """Submit an Agent for marketplace review (owner only).

    Runs the D7 checks first: a ``memory_space`` binding rejects the submission with 400,
    and the response enumerates the author's own skills that publication would make
    readable to anyone who runs the Agent.
    """
    try:
        listing, exposed = await submit_listing(agent_id, current_user, request)
        return ListingSubmissionResponse(agent_id=agent_id, listing=listing, exposed_skills=exposed)
    except ListingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        logger.error(f"Error submitting agent listing: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to submit agent listing: {str(e)}")


@router.delete("/{agent_id}/listing", response_model=AgentListing)
async def withdraw_agent_listing_endpoint(
    agent_id: str, current_user: User = Depends(require_marketplace_enabled)
):
    """Unpublish an Agent or withdraw a pending submission (owner only).

    Returns the listing to ``private``. This revokes nothing retroactively — pins keep
    working and conversations underway keep running; it is a delisting, not a recall.
    """
    try:
        return await withdraw_listing(agent_id, current_user)
    except ListingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        logger.error(f"Error withdrawing agent listing: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to withdraw agent listing: {str(e)}")
