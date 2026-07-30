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

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)

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
from apis.shared.assistants.version_resolution import (
    AgentVersionUnavailableError,
    resolve_display_agent,
)
from apis.shared.assistants.service import (
    assistant_exists,
    create_assistant,
    create_assistant_draft,
    AssistantListedError,
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
from apis.app_api.agent_designer.services.icon_service import (
    AgentIconError,
    read_icon,
    remove_icon,
    upload_icon,
)
from apis.app_api.agent_designer.services.listing_service import (
    ListingError,
    preflight_listing,
    submit_listing,
    withdraw_listing,
)
from apis.app_api.agent_designer.services.pin_service import (
    PinError,
    list_pins,
    pin_agent,
    unpin_agent,
)
from apis.app_api.agent_designer.services.report_service import ReportError, file_report
from apis.app_api.agent_designer.services.store_service import (
    browse_all,
    browse_category,
    store_front,
)
from apis.shared.assistants.models import (
    AgentIconResponse,
    AgentListing,
    AgentPinsResponse,
    AgentStoreFrontResponse,
    AgentStoreResponse,
    ListingPreflightResponse,
    ListingSubmissionResponse,
    PinnedAgentResponse,
    SubmitListingRequest,
    SubmitReportRequest,
    SubmitReportResponse,
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
        # ``approvedInstructionsHash`` rides the same gate: a hash *of* the instructions is
        # not reversible alone, but it would confirm a guessed prompt for anyone who could
        # produce one.
        #
        # Nothing writes this field any more — version snapshots replaced drift detection
        # and it is off the model. It is still stripped because ``AgentListing`` is
        # ``extra="allow"``: a listing approved *before* that removal still carries the
        # attribute in DynamoDB, and it would now round-trip straight through to a viewer.
        # Transitional, and safe to delete once no stored listing carries it.
        if isinstance(view.get("listing"), dict):
            view["listing"].pop("approvedInstructionsHash", None)
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

    ``featured`` is the admin's curated order — the store's only ranking lever, since
    everything below it is newest-first (see the spec's ranking caveat). Entries whose
    listing is no longer published are dropped here rather than rendered as dead tiles.
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


# ----------------------------------------------------------------------- pins (D8/D9)
# Declared BEFORE ``/{agent_id}`` so the literal path is not captured by the path param.
@router.get("/pins", response_model=AgentPinsResponse)
async def list_agent_pins_endpoint(current_user: User = Depends(require_marketplace_enabled)):
    """The caller's effective pin list (D9).

    Phase 5 resolves the user's own pins. Role-seeded pins union in here in Phase 6
    without changing the shape — ``source`` and ``locked`` are already on every row.

    Rows the caller can no longer reach (deleted, or visibility narrowed) are omitted from
    the response while the stored pin is left untouched: both conditions are reversible,
    and a read is not the place to garbage-collect someone's shelf.
    """
    try:
        return AgentPinsResponse(pins=await list_pins(current_user))
    except Exception as e:
        logger.error(f"Error listing agent pins: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list pinned agents: {str(e)}")


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

    This is the marketplace **detail read** (Phase 3). Three things beyond Phase 1:

    * ``instructions`` is gated to owner/editor — see ``INSTRUCTIONS_PERMISSIONS``.
    * ``capabilities`` + ``modelLabel`` are resolved, so the detail page can say what the
      Agent reaches by *name* rather than making the SPA dereference binding refs.
    * A published Agent is served from its **approved snapshot** to everyone who cannot edit
      it (§4). This is the page a store user reads before deciding to open something, so it
      has to describe the configuration that will actually run — otherwise the author's
      unreviewed draft supplies the name, the summary and, through ``bindings``, the
      capability list, while invocation quietly runs the approved version instead.

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

        # Snapshot before anything reads ``assistant``, so ``capabilities`` and the listing
        # display below resolve against the same configuration the response describes.
        # ``can_edit`` is the instructions gate: whoever may edit the draft must be shown the
        # draft, because this endpoint is also what loads the Agent Designer's form.
        try:
            assistant, _ = await resolve_display_agent(
                assistant, can_edit=permission in INSTRUCTIONS_PERMISSIONS
            )
        except AgentVersionUnavailableError as unavailable:
            logger.error(f"Published version unavailable for detail read: {unavailable}")
            raise HTTPException(
                status_code=503,
                detail=(
                    "This agent's published version could not be loaded. Please try again, "
                    "or contact an administrator if it persists."
                ),
            ) from unavailable

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
    except AssistantListedError as e:
        # 409, not 400: the request is well-formed and the caller is allowed — the Agent is
        # simply in a state that forbids it, and the message says how to change that.
        raise HTTPException(status_code=409, detail=e.message)
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
# /admin/agents/* behind require_admin.
@router.get("/{agent_id}/listing/preflight", response_model=ListingPreflightResponse)
async def agent_listing_preflight_endpoint(
    agent_id: str, current_user: User = Depends(require_marketplace_enabled)
):
    """What the submit dialog needs before the author commits (D7, owner only).

    A read-only rehearsal of the submit checks: the skills publication would expose, the
    memory-space block if there is one, and whether the author still has to consent to
    going public. Declared before ``/listing/submit`` only for reading order — the paths
    are literal and do not collide.
    """
    try:
        exposed, block_reason, reachability, requires_public = await preflight_listing(
            agent_id, current_user
        )
        return ListingPreflightResponse(
            agent_id=agent_id,
            exposed_skills=exposed,
            block_reason=block_reason,
            reachability=reachability,
            requires_public=requires_public,
        )
    except ListingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        logger.error(f"Error preflighting agent listing: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to preflight agent listing: {str(e)}")


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


@router.post("/{agent_id}/pin", response_model=PinnedAgentResponse, status_code=201)
async def pin_agent_endpoint(
    agent_id: str, current_user: User = Depends(require_marketplace_enabled)
):
    """Add an Agent to the caller's own set (D8).

    A pointer, never a fork: nothing is copied, so the Agent the user reaches tomorrow is
    the one its author maintains. Pinning something already pinned is a no-op that returns
    the existing row, and it clears any earlier dismissal of that Agent (D9.3).
    """
    try:
        return await pin_agent(current_user, agent_id)
    except PinError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        logger.error(f"Error pinning agent: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to pin agent: {str(e)}")


@router.delete("/{agent_id}/pin", status_code=204)
async def unpin_agent_endpoint(
    agent_id: str, current_user: User = Depends(require_marketplace_enabled)
):
    """Remove an Agent from the caller's own set, and remember the dismissal (D9.3).

    The tombstone is the point: role-seeded pins (Phase 6) are resolved live, so without a
    remembered dismissal a seeded pin would re-appear on the next request and the user
    could never remove it.
    """
    try:
        await unpin_agent(current_user, agent_id)
    except PinError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        logger.error(f"Error unpinning agent: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to unpin agent: {str(e)}")


# ----------------------------------------------------------------- problem reports (D15)
@router.post("/{agent_id}/report", response_model=SubmitReportResponse, status_code=201)
async def report_agent_endpoint(
    agent_id: str,
    request: SubmitReportRequest,
    current_user: User = Depends(require_marketplace_enabled),
):
    """Report a problem with a published Agent (D15).

    **This is not a review, and the distinction is the whole design.** There are no stars,
    no public comments and no visible counts: a report is a *private message to the
    curator*, never shelf content, and nothing the reporter writes is ever rendered to
    another browsing user. It is also never a ranking input — nothing here touches
    ``usageCount`` or the store front, because the moment report volume influenced
    placement, reporting would become a way to bury a competitor's Agent.

    Reportable means **published** (D15.3): you may report what the store offered you.
    A second report while the reporter's first is still open **updates** it rather than
    stacking (D15.4), and the response says so — without that the queue is trivially
    floodable and the count at the top of the nav stops meaning anything.
    """
    try:
        report, replaced = await file_report(
            agent_id, current_user, reason=request.reason, note=request.note
        )
        return SubmitReportResponse(
            agent_id=agent_id,
            reason=report.reason,
            state=report.state,
            created_at=report.created_at,
            replaced_existing=replaced,
        )
    except ReportError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        logger.error(f"Error reporting agent: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to report agent: {str(e)}")


# ------------------------------------------------------------------------ icons (D5)
# Bytes to S3, key on the record, and one route that serves them back. The serve route
# is what makes ``iconUrl`` a stable path instead of a presigned URL that changes on
# every read — see ``apis.shared.assistants.icons.icon_url``.
@router.post("/{agent_id}/icon", response_model=AgentIconResponse, response_model_exclude_none=True)
async def upload_agent_icon_endpoint(
    agent_id: str,
    file: UploadFile = File(...),
    current_user: User = Depends(require_marketplace_enabled),
):
    """Upload the Agent's square icon (owner or editor).

    512×512 PNG or JPEG, ≤ 400 KB (D5). The image is re-encoded server-side — that is
    what normalizes the dimensions *and* strips EXIF, so an icon cropped from a phone
    photo does not publish its GPS coordinates. Rejections carry the limit and the
    supplied value, since "invalid image" sends an author back to the file picker with
    nothing to change.
    """
    content = await file.read()
    try:
        return await upload_icon(agent_id, content, current_user)
    except AgentIconError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        logger.error(f"Error uploading agent icon: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to upload agent icon: {str(e)}")


@router.delete("/{agent_id}/icon", response_model=AgentIconResponse, response_model_exclude_none=True)
async def delete_agent_icon_endpoint(
    agent_id: str, current_user: User = Depends(require_marketplace_enabled)
):
    """Clear the icon, returning the Agent to its generated gradient (owner or editor)."""
    try:
        return await remove_icon(agent_id, current_user)
    except AgentIconError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        logger.error(f"Error removing agent icon: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to remove agent icon: {str(e)}")


@router.get("/{agent_id}/icon")
async def get_agent_icon_endpoint(
    agent_id: str,
    request: Request,
    current_user: User = Depends(require_marketplace_enabled),
):
    """Serve the icon bytes.

    The object is immutable — its key *is* its content digest — so this answers with a
    one-year ``immutable`` cache directive and the digest as the ETag. A replacement
    changes ``iconUrl``'s ``?v=``, which is what busts the cache; the ``If-None-Match``
    304 below is for the same URL being asked for twice.

    A missing icon is a 404 and the SPA falls through to the generated gradient, so a key
    that outlived its object degrades to the designed default rather than a broken tile.
    """
    try:
        data, content_type, version = await read_icon(agent_id, current_user)
    except AgentIconError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        logger.error(f"Error reading agent icon: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to read agent icon: {str(e)}")

    etag = f'"{version}"'
    headers = {"Cache-Control": "public, max-age=31536000, immutable", "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type=content_type, headers=headers)
