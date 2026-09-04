"""Artifact render-token API routes."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from apis.shared.auth import User, get_current_user_from_session

from .models import (
    ArtifactContentResponse,
    ArtifactLibraryResponse,
    ArtifactListResponse,
    ArtifactSummary,
    LibraryArtifact,
    RenderTokenRequest,
    RenderTokenResponse,
)
from .service import (
    ArtifactContentService,
    ArtifactListService,
    ArtifactNotFoundError,
    ArtifactQueryError,
    ArtifactTooLargeError,
    RenderTokenConfigError,
    RenderTokenService,
    get_artifact_content_service,
    get_artifact_list_service,
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


@router.get("", response_model=ArtifactListResponse)
async def list_session_artifacts(
    session_id: str = Query(..., description="Chat session id to list artifacts for"),
    user: User = Depends(get_current_user_from_session),
    service: ArtifactListService = Depends(get_artifact_list_service),
) -> ArtifactListResponse:
    """List every version of every artifact created in a chat session.

    Used by the SPA to hydrate per-version artifact cards on session load
    (live creation/updates are delivered separately via the `artifact`
    SSE event). Each artifact is re-checked against the authenticated
    user, so a borrowed session id cannot enumerate another user's
    artifacts. An unknown or artifact-free session is a normal empty
    list, not a 404.
    """
    try:
        rows = service.list_for_session(
            user_id=user.user_id, session_id=session_id
        )
    except RenderTokenConfigError:
        logger.exception("artifact list service misconfigured")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Artifact listing is unavailable",
        )
    except ArtifactQueryError:
        # Feature is configured fine — the backing query just failed
        # (throttle/timeout/transient). Retryable, so signal 503 rather
        # than masquerading as a 500 misconfiguration.
        logger.exception("artifact list query failed")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Artifact listing is temporarily unavailable",
        )

    return ArtifactListResponse(
        artifacts=[ArtifactSummary(**row) for row in rows]
    )


@router.get("/library", response_model=ArtifactLibraryResponse)
async def list_user_artifacts(
    user: User = Depends(get_current_user_from_session),
    service: ArtifactListService = Depends(get_artifact_list_service),
) -> ArtifactLibraryResponse:
    """Every artifact the caller owns, at HEAD, newest-first.

    Backs the artifact library page. Takes no scoping parameter by
    design: the only user it can ever describe is the authenticated one,
    since the underlying Query is keyed on `PK=USER#{uid}`. There is no
    way to ask this endpoint about somebody else.

    A separate route rather than an optional `session_id` on the list
    endpoint above, because the two differ in cardinality: that one
    returns a row per *version* (the SPA anchors each to its turn), this
    one a row per *artifact*. Overloading one path with both shapes would
    make the response type depend on which query params were present.

    Unpaginated, matching the shape of the data: the heaviest partition
    in production holds well under a single 1MB Query page. Pagination
    lands with the user index when it is needed, not before.
    """
    try:
        rows = service.list_for_user(user_id=user.user_id)
    except RenderTokenConfigError:
        logger.exception("artifact library service misconfigured")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Artifact listing is unavailable",
        )
    except ArtifactQueryError:
        logger.exception("artifact library query failed")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Artifact listing is temporarily unavailable",
        )

    return ArtifactLibraryResponse(
        artifacts=[LibraryArtifact(**row) for row in rows]
    )


@router.get("/{artifact_id}/content", response_model=ArtifactContentResponse)
async def get_artifact_content(
    artifact_id: str,
    version: int = Query(..., ge=1, description="Artifact version to read"),
    user: User = Depends(get_current_user_from_session),
    service: ArtifactContentService = Depends(get_artifact_content_service),
) -> ArtifactContentResponse:
    """Return one artifact version's raw source for the panel code view.

    The bytes are inert text the SPA highlights client-side — never
    executed. Ownership is enforced by building the lookup key from the
    authenticated session, so a borrowed artifact/session id can't read
    another user's content. Markdown is unwrapped back to the authored
    source (see ArtifactContentService). Oversized artifacts 413 so the
    SPA can steer the user to the download path instead.
    """
    try:
        content, content_type = service.get(
            # The authenticated session user — self-scoping. See the
            # access-control note on ArtifactContentService.
            owner_id=user.user_id,
            artifact_id=artifact_id,
            version=version,
        )
    except ArtifactNotFoundError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Artifact version not found"
        )
    except ArtifactTooLargeError:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "Artifact is too large to preview — download it instead",
        )
    except RenderTokenConfigError:
        logger.exception("artifact content service misconfigured")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Artifact content is unavailable",
        )
    except ArtifactQueryError:
        logger.exception("artifact content fetch failed")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Artifact content is temporarily unavailable",
        )

    return ArtifactContentResponse(
        content=content,
        content_type=content_type,
        version=version,
    )
