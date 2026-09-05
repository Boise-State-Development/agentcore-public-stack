"""Share API routes

Provides endpoints for sharing conversations via shareable URLs.
Three routers:
  - conversations_share_router: mounted at /conversations (create, list shares)
  - shares_router: mounted at /shares (update, revoke, export individual shares)
  - shared_view_router: mounted at /shared (read-only retrieval)
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User

# The artifacts domain owns render-token minting; this module owns the
# grant that authorizes it. Both live under app_api, so this is a
# same-package import, not a service boundary crossing.
from apis.app_api.artifacts.models import RenderTokenResponse
from apis.app_api.artifacts.service import (
    ArtifactNotFoundError,
    RenderTokenConfigError,
    get_render_token_service,
)

from .models import (
    CreateShareRequest,
    ShareListResponse,
    ShareResponse,
    SharedConversationResponse,
    UpdateShareRequest,
)
from .service import (
    AccessDeniedError,
    NotOwnerError,
    SessionNotFoundError,
    ShareNotFoundError,
    ShareStorageUnavailableError,
    ShareTableNotFoundError,
    get_share_service,
)

logger = logging.getLogger(__name__)

# Router for /conversations/{session_id}/share endpoints
conversations_share_router = APIRouter(prefix="/conversations", tags=["shares"])

# Router for /shares/{share_id} endpoints (update, revoke, export)
shares_router = APIRouter(prefix="/shares", tags=["shares"])

# Router for /shared/{share_id} endpoint (read-only view)
shared_view_router = APIRouter(prefix="/shared", tags=["shares"])


# ------------------------------------------------------------------
# Conversation-scoped endpoints
# ------------------------------------------------------------------


@conversations_share_router.post(
    "/{session_id}/share",
    response_model=ShareResponse,
    response_model_by_alias=True,
    status_code=201,
)
async def create_share(
    session_id: str,
    request: CreateShareRequest,
    current_user: User = Depends(get_current_user_from_session),
):
    """Create a point-in-time share snapshot for a conversation."""
    try:
        return await get_share_service().create_share(
            session_id=session_id,
            user=current_user,
            request=request,
        )
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
    except NotOwnerError:
        raise HTTPException(status_code=403, detail="You do not have permission to share this session")
    except ShareTableNotFoundError:
        raise HTTPException(status_code=503, detail="Share feature unavailable - table not deployed")
    except ShareStorageUnavailableError:
        raise HTTPException(
            status_code=503,
            detail="Sharing is temporarily unavailable. Please try again later.",
        )
    except Exception as e:
        safe_session_id = session_id.replace("\r", "").replace("\n", "")
        logger.error(f"Error creating share for session {safe_session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create share")


@conversations_share_router.get(
    "/{session_id}/shares",
    response_model=ShareListResponse,
    response_model_by_alias=True,
)
async def list_shares_for_session(
    session_id: str,
    current_user: User = Depends(get_current_user_from_session),
):
    """List all shares for a session (owner only)."""
    try:
        return await get_share_service().get_shares_for_session(session_id, current_user.user_id)
    except ShareTableNotFoundError:
        raise HTTPException(status_code=503, detail="Share feature unavailable - table not deployed")
    except Exception as e:
        sanitized_session_id = session_id.replace("\r", "").replace("\n", "")
        logger.error(f"Error listing shares for session {sanitized_session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list shares")


# ------------------------------------------------------------------
# Share-scoped endpoints (by share_id)
# ------------------------------------------------------------------


@shares_router.patch(
    "/{share_id}",
    response_model=ShareResponse,
    response_model_by_alias=True,
)
async def update_share(
    share_id: str,
    request: UpdateShareRequest,
    current_user: User = Depends(get_current_user_from_session),
):
    """Update access level or allowed emails on an existing share."""
    try:
        return await get_share_service().update_share(
            share_id=share_id,
            user=current_user,
            request=request,
        )
    except ShareNotFoundError:
        raise HTTPException(status_code=404, detail="Share not found")
    except NotOwnerError:
        raise HTTPException(status_code=403, detail="You do not have permission to update this share")
    except ShareTableNotFoundError:
        raise HTTPException(status_code=503, detail="Share feature unavailable - table not deployed")
    except Exception as e:
        sanitized_share_id = share_id.replace("\r", "").replace("\n", "")
        logger.error(f"Error updating share {sanitized_share_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update share")


@shares_router.delete("/{share_id}", status_code=204)
async def revoke_share(
    share_id: str,
    current_user: User = Depends(get_current_user_from_session),
):
    """Revoke (delete) a specific share."""
    try:
        await get_share_service().revoke_share(share_id=share_id, user=current_user)
        return Response(status_code=204)
    except ShareNotFoundError:
        raise HTTPException(status_code=404, detail="Share not found")
    except NotOwnerError:
        raise HTTPException(status_code=403, detail="You do not have permission to revoke this share")
    except ShareTableNotFoundError:
        raise HTTPException(status_code=503, detail="Share feature unavailable - table not deployed")
    except Exception as e:
        sanitized_share_id = share_id.replace("\r", "").replace("\n", "")
        logger.error(f"Error revoking share {sanitized_share_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to revoke share")


@shares_router.post("/{share_id}/export", status_code=201)
async def export_shared_conversation(
    share_id: str,
    current_user: User = Depends(get_current_user_from_session),
):
    """Export a shared conversation into a new session for the current user."""
    try:
        return await get_share_service().export_shared_conversation(
            share_id=share_id,
            requester=current_user,
        )
    except ShareNotFoundError:
        raise HTTPException(status_code=404, detail="Share not found")
    except AccessDeniedError:
        raise HTTPException(status_code=403, detail="Access denied")
    except ShareTableNotFoundError:
        raise HTTPException(status_code=503, detail="Share feature unavailable - table not deployed")
    except Exception as e:
        sanitized_share_id = share_id.replace("\r", "").replace("\n", "")
        logger.error(f"Error exporting share {sanitized_share_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to export shared conversation")


# ------------------------------------------------------------------
# Shared view endpoint (public read-only)
# ------------------------------------------------------------------


@shared_view_router.get(
    "/{share_id}",
    response_model=SharedConversationResponse,
    response_model_by_alias=True,
)
async def get_shared_conversation(
    share_id: str,
    current_user: User = Depends(get_current_user_from_session),
):
    """Retrieve a shared conversation snapshot (access-controlled)."""
    try:
        return await get_share_service().get_shared_conversation(
            share_id=share_id,
            requester=current_user,
        )
    except ShareNotFoundError:
        raise HTTPException(status_code=404, detail="Share not found")
    except AccessDeniedError:
        raise HTTPException(status_code=403, detail="Access denied")
    except ShareTableNotFoundError:
        raise HTTPException(status_code=503, detail="Share feature unavailable - table not deployed")
    except Exception as e:
        sanitized_share_id = share_id.replace("\r", "").replace("\n", "")
        logger.error(f"Error retrieving shared conversation {sanitized_share_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve shared conversation")


@shared_view_router.post(
    "/{share_id}/artifacts/{artifact_id}/render-token",
    response_model=RenderTokenResponse,
)
async def mint_shared_conversation_artifact_token(
    share_id: str,
    artifact_id: str,
    current_user: User = Depends(get_current_user_from_session),
):
    """Mint a render URL for an artifact inside a shared conversation.

    The grant is the CONVERSATION share — there is no separate artifact
    share record, and none is created. Sharing a conversation shares the
    artifacts it produced, so the conversation share is the single thing
    that grants, updates and revokes them: change its allowlist and the
    artifacts follow in the same write, revoke it and they die with it.
    Parallel artifact-share rows would each need cascading, and a missed
    cascade is an artifact still readable after its conversation was
    locked down.

    The version served is the one pinned in the snapshot, so a recipient
    sees the artifact the transcript around it describes rather than
    whatever the owner has edited it into since.

    `resolve_shared_artifact` is the access boundary and does both
    checks — may this viewer open this share, and is this artifact one
    the snapshot pinned. The mint it feeds performs none of its own.
    """
    try:
        owner_id, version = get_share_service().resolve_shared_artifact(
            share_id=share_id,
            artifact_id=artifact_id,
            requester=current_user,
        )
        url, exp = get_render_token_service().mint_for_conversation_share(
            owner_id=owner_id,
            artifact_id=artifact_id,
            version=version,
            conversation_share_id=share_id,
            viewer=current_user,
        )
    except ShareNotFoundError:
        # Also the "not in this snapshot" case, deliberately: an artifact
        # the share does not carry is indistinguishable from one that
        # does not exist, so this reveals nothing about what the owner has.
        raise HTTPException(status_code=404, detail="Artifact not found")
    except AccessDeniedError:
        raise HTTPException(status_code=403, detail="Access denied")
    except ArtifactNotFoundError:
        # Pinned by the snapshot but gone from the table — deleted since
        # the conversation was shared.
        raise HTTPException(status_code=404, detail="Artifact not found")
    except ShareTableNotFoundError:
        raise HTTPException(
            status_code=503,
            detail="Share feature unavailable - table not deployed",
        )
    except RenderTokenConfigError:
        logger.error("artifact render token service misconfigured", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Artifact rendering is unavailable"
        )

    return RenderTokenResponse(
        url=url,
        expires_at=datetime.fromtimestamp(exp, tz=timezone.utc).isoformat(),
    )
