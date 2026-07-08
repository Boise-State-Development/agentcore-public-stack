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

from fastapi import APIRouter, Depends, HTTPException, status

from apis.app_api.agent_designer.services.bindable_catalog import (
    BINDABLE_KINDS,
    list_bindable,
)
from apis.app_api.agent_designer.services.binding_validation import (
    BindingValidationError,
    validate_agent_write,
)
from apis.shared.assistants.compat import to_agent_view
from apis.shared.assistants.models import (
    AgentResponse,
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
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.feature_flags import agents_enabled

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


def _agent_response(assistant, *, permission: Optional[str] = None,
                    is_shared_with_me: Optional[bool] = None) -> AgentResponse:
    """Project an Assistant into the Agent read-shape, layering share metadata."""
    view = to_agent_view(assistant)
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
    """Retrieve an Agent by id with visibility-based access control."""
    try:
        if not await assistant_exists(agent_id):
            raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")
        assistant, permission = await get_assistant_with_access_check(
            assistant_id=agent_id, user_id=current_user.user_id, user_email=current_user.email
        )
        if not assistant:
            raise HTTPException(status_code=403, detail="Access denied: you do not have permission to access this agent")
        return _agent_response(assistant, permission=permission)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving agent: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to retrieve agent: {str(e)}")


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
