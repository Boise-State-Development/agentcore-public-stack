"""Artifact render-token API routes."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from apis.shared.auth import User, get_current_user_from_session

from .models import RenderTokenRequest, RenderTokenResponse
from .service import (
    ArtifactNotFoundError,
    RenderTokenConfigError,
    RenderTokenService,
    get_render_token_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


@router.post("/{artifact_id}/render-token", response_model=RenderTokenResponse)
async def mint_render_token(
    artifact_id: str,
    request: RenderTokenRequest,
    user: User = Depends(get_current_user_from_session),
    service: RenderTokenService = Depends(get_render_token_service),
) -> RenderTokenResponse:
    """Mint a short-lived render token for one artifact version.

    `sub` is taken from the authenticated session, so a caller can only
    ever obtain a token for their own artifact. The version is validated
    against DynamoDB before minting so the SPA gets a clean 404 rather
    than a token that renders the Lambda's error page in the iframe.
    """
    try:
        url, exp = service.mint(
            user_id=user.user_id,
            artifact_id=artifact_id,
            version=request.version,
            session_id=request.session_id,
        )
    except ArtifactNotFoundError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Artifact version not found"
        )
    except RenderTokenConfigError:
        logger.exception("render token service misconfigured")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Artifact rendering is unavailable",
        )

    return RenderTokenResponse(
        url=url,
        expires_at=datetime.fromtimestamp(exp, tz=timezone.utc).isoformat(),
    )
