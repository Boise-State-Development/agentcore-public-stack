"""Artifact sharing API routes.

Two routers, mounted together under the same enablement signal that
gates `artifacts/routes.py` (presence of
`ARTIFACTS_RENDER_TOKEN_SECRET_ARN`):

  - ``artifact_shares_router`` — owner CRUD, under ``/artifacts``.
  - ``shared_artifacts_router`` — the recipient surface, under
    ``/shared-artifacts``.

Every route depends on ``get_current_user_from_session``. "Public" here
means *any authenticated tenant user*, exactly as it does for
conversation shares — never anonymous. Governance is Entra JWT identity,
so there is no unauthenticated path to an artifact at all.

The recipient render-token route is the security-critical one: it hands
a viewer a short-lived credential addressed to the *owner's* DynamoDB
partition. See the block comment on ``RenderTokenService.mint_for_share``
before touching it.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from apis.shared.auth import User, get_current_user_from_session
from apis.shared.feature_flags import artifact_share_inbox_enabled
from apis.shared.security.log_sanitize import scrub_log

from .models import (
    ArtifactContentResponse,
    ArtifactShareListResponse,
    ArtifactShareResponse,
    CreateArtifactShareRequest,
    RenderTokenResponse,
    SharedArtifactResponse,
    SharedWithMeArtifact,
    SharedWithMeResponse,
    UpdateArtifactShareRequest,
)
from .service import (
    ArtifactContentService,
    ArtifactNotFoundError,
    ArtifactQueryError,
    ArtifactShareService,
    ArtifactTooLargeError,
    NotShareOwnerError,
    RenderTokenConfigError,
    RenderTokenService,
    ShareAccessDeniedError,
    ShareNotFoundError,
    get_artifact_content_service,
    get_artifact_share_service,
    get_render_token_service,
)

logger = logging.getLogger(__name__)

artifact_shares_router = APIRouter(prefix="/artifacts", tags=["artifact-shares"])
shared_artifacts_router = APIRouter(
    prefix="/shared-artifacts", tags=["artifact-shares"]
)


def _share_response(share: dict) -> ArtifactShareResponse:
    return ArtifactShareResponse(
        share_id=share["share_id"],
        artifact_id=share.get("artifact_id", ""),
        version=int(share.get("version", 0)),
        owner_id=share.get("owner_id", ""),
        access_level=share.get("access_level", "specific"),
        allowed_emails=share.get("allowed_emails"),
        title=share.get("title", ""),
        content_type=share.get("content_type", ""),
        created_at=share.get("created_at", ""),
        updated_at=share.get("updated_at"),
        share_url=f"/shared-artifact/{share['share_id']}",
    )


# ------------------------------------------------------------------
# Owner endpoints
# ------------------------------------------------------------------


@artifact_shares_router.post(
    "/{artifact_id}/shares",
    response_model=ArtifactShareResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
async def create_artifact_share(
    artifact_id: str,
    request: CreateArtifactShareRequest,
    user: User = Depends(get_current_user_from_session),
    service: ArtifactShareService = Depends(get_artifact_share_service),
) -> ArtifactShareResponse:
    """Share one immutable artifact version.

    The version row is looked up with a partition key built from the
    authenticated session, so a caller can only share their own
    artifact — someone else's version is an indistinguishable 404.
    """
    try:
        share = service.create(
            owner=user,
            artifact_id=artifact_id,
            version=request.version,
            access_level=request.access_level,
            allowed_emails=request.allowed_emails,
        )
    except ArtifactNotFoundError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Artifact version not found"
        )
    except RenderTokenConfigError:
        logger.exception("artifact share service misconfigured")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Artifact sharing is unavailable",
        )
    except ArtifactQueryError:
        logger.exception("artifact share write failed")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Artifact sharing is temporarily unavailable",
        )

    return _share_response(share)


@artifact_shares_router.get(
    "/{artifact_id}/shares",
    response_model=ArtifactShareListResponse,
    response_model_by_alias=True,
)
async def list_artifact_shares(
    artifact_id: str,
    user: User = Depends(get_current_user_from_session),
    service: ArtifactShareService = Depends(get_artifact_share_service),
) -> ArtifactShareListResponse:
    """List the caller's shares for one artifact.

    Partition-scoped to the authenticated user, so an unknown or
    unowned artifact id is a normal empty list rather than a 404 — it
    reveals nothing about whether that artifact exists.
    """
    try:
        shares = service.list_for_artifact(
            owner_id=user.user_id, artifact_id=artifact_id
        )
    except RenderTokenConfigError:
        logger.exception("artifact share service misconfigured")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Artifact sharing is unavailable",
        )
    except ArtifactQueryError:
        logger.exception("artifact share list query failed")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Artifact sharing is temporarily unavailable",
        )

    return ArtifactShareListResponse(
        shares=[_share_response(share) for share in shares]
    )


@artifact_shares_router.patch(
    "/shares/{share_id}",
    response_model=ArtifactShareResponse,
    response_model_by_alias=True,
)
async def update_artifact_share(
    share_id: str,
    request: UpdateArtifactShareRequest,
    user: User = Depends(get_current_user_from_session),
    service: ArtifactShareService = Depends(get_artifact_share_service),
) -> ArtifactShareResponse:
    """Change who may view an existing share. Owner only."""
    try:
        share = service.update(
            share_id=share_id,
            owner=user,
            access_level=request.access_level,
            allowed_emails=request.allowed_emails,
        )
    except ShareNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Share not found")
    except NotShareOwnerError:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You do not have permission to update this share",
        )
    except RenderTokenConfigError:
        logger.exception("artifact share service misconfigured")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Artifact sharing is unavailable",
        )
    except ArtifactQueryError:
        logger.exception("artifact share update failed")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Artifact sharing is temporarily unavailable",
        )

    return _share_response(share)


@artifact_shares_router.delete(
    "/shares/{share_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def revoke_artifact_share(
    share_id: str,
    user: User = Depends(get_current_user_from_session),
    service: ArtifactShareService = Depends(get_artifact_share_service),
) -> Response:
    """Revoke a share. Owner only.

    Deletes both rows, so the next recipient open finds nothing to mint
    against. Already-issued tokens stay valid until they expire, which
    bounds the revocation window at the ~120s token TTL rather than at
    the length of the recipient's session.
    """
    try:
        service.revoke(share_id=share_id, owner=user)
    except ShareNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Share not found")
    except NotShareOwnerError:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You do not have permission to revoke this share",
        )
    except RenderTokenConfigError:
        logger.exception("artifact share service misconfigured")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Artifact sharing is unavailable",
        )
    except ArtifactQueryError:
        logger.exception("artifact share revoke failed")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Artifact sharing is temporarily unavailable",
        )

    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------
# Recipient endpoints
# ------------------------------------------------------------------


@shared_artifacts_router.get(
    "",
    response_model=SharedWithMeResponse,
    response_model_by_alias=True,
)
async def list_shared_with_me(
    limit: int = Query(
        25, ge=1, le=100, description="Maximum shares to return"
    ),
    cursor: str | None = Query(
        None, description="Opaque continuation token from a previous page"
    ),
    user: User = Depends(get_current_user_from_session),
    service: ArtifactShareService = Depends(get_artifact_share_service),
) -> SharedWithMeResponse:
    """Artifacts other people have shared with the caller, newest first.

    Scoped by construction, like every other listing in this domain: the
    partition is built from the authenticated session's email and there
    is no parameter that could widen it. There is no way to ask this
    endpoint about somebody else's inbox.

    Only `specific` shares appear. `public` means "any authenticated
    tenant user", which has no recipient list to fan out to — those stay
    link-delivered. Listing every public share in the tenant would be a
    different feature with a different consent story.

    404s while `ARTIFACT_SHARE_INBOX_ENABLED` is off, matching the
    mid-turn-steering endpoint's behaviour under its own flag: the
    surface does not exist in this environment, which is exactly what a
    404 says. The SPA reads that as "no tabs" and renders the library it
    always did. Note the flag gates only this read — the rows behind it
    are written regardless, so turning it on shows a complete inbox
    rather than one that starts from the flip.
    """
    if not artifact_share_inbox_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    try:
        items, next_cursor = service.list_for_recipient(
            viewer=user, limit=limit, cursor=cursor
        )
    except RenderTokenConfigError:
        logger.exception("artifact share inbox misconfigured")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Artifact sharing is unavailable",
        )
    except ArtifactQueryError:
        logger.exception("artifact share inbox query failed")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Artifact sharing is temporarily unavailable",
        )

    return SharedWithMeResponse(
        artifacts=[
            SharedWithMeArtifact(
                share_id=item["share_id"],
                title=item["title"],
                content_type=item["content_type"],
                version=item["version"],
                owner_email=item["owner_email"],
                shared_at=item["shared_at"],
                share_url=f"/shared-artifact/{item['share_id']}",
            )
            for item in items
        ],
        next_cursor=next_cursor,
    )


@shared_artifacts_router.get(
    "/{share_id}",
    response_model=SharedArtifactResponse,
    response_model_by_alias=True,
)
async def get_shared_artifact(
    share_id: str,
    user: User = Depends(get_current_user_from_session),
    service: ArtifactShareService = Depends(get_artifact_share_service),
) -> SharedArtifactResponse:
    """Metadata for a shared artifact. Never returns content.

    Access-controlled: a revoked share is a 404 and a viewer outside the
    allowlist is a 403, so a recipient learns nothing about a share they
    cannot open beyond whether the link is dead or simply not theirs.
    """
    try:
        share = service.get_for_viewer(share_id=share_id, viewer=user)
    except ShareNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Share not found")
    except ShareAccessDeniedError:
        logger.info(
            "artifact share access denied share=%s viewer=%s",
            scrub_log(share_id),
            scrub_log(user.user_id),
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    except RenderTokenConfigError:
        logger.exception("artifact share service misconfigured")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Artifact sharing is unavailable",
        )
    except ArtifactQueryError:
        logger.exception("shared artifact lookup failed")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Artifact sharing is temporarily unavailable",
        )

    return SharedArtifactResponse(
        share_id=share["share_id"],
        title=share.get("title", ""),
        content_type=share.get("content_type", ""),
        version=int(share.get("version", 0)),
        created_at=share.get("created_at", ""),
        owner_email=share.get("owner_email", ""),
        can_download=True,
    )


@shared_artifacts_router.post(
    "/{share_id}/render-token", response_model=RenderTokenResponse
)
async def mint_shared_render_token(
    share_id: str,
    user: User = Depends(get_current_user_from_session),
    service: RenderTokenService = Depends(get_render_token_service),
) -> RenderTokenResponse:
    """Mint a render token for a shared artifact version.

    Returns the same ``{url, expires_at}`` shape as the owner endpoint,
    so the SPA's iframe and ``?download=1`` paths work unchanged.

    The minted token's ``sub`` is the OWNER, because it is the DynamoDB
    partition key the render Lambda builds — the ACL check inside
    ``mint_for_share`` is what makes that safe. Read the block comment
    there before changing anything on this path.
    """
    try:
        url, exp = service.mint_for_share(share_id=share_id, viewer=user)
    except ShareNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Share not found")
    except ShareAccessDeniedError:
        logger.info(
            "artifact share mint denied share=%s viewer=%s",
            scrub_log(share_id),
            scrub_log(user.user_id),
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    except ArtifactNotFoundError:
        # The share row outlived the artifact version it points at. A
        # 404 beats minting a token that renders the Lambda's error page
        # inside the recipient's iframe.
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Artifact version not found"
        )
    except RenderTokenConfigError:
        logger.exception("render token service misconfigured")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Artifact rendering is unavailable",
        )
    except ArtifactQueryError:
        logger.exception("shared render token lookup failed")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Artifact rendering is temporarily unavailable",
        )

    return RenderTokenResponse(
        url=url,
        expires_at=datetime.fromtimestamp(exp, tz=timezone.utc).isoformat(),
    )


@shared_artifacts_router.get(
    "/{share_id}/content", response_model=ArtifactContentResponse
)
async def get_shared_artifact_content(
    share_id: str,
    user: User = Depends(get_current_user_from_session),
    shares: ArtifactShareService = Depends(get_artifact_share_service),
    content: ArtifactContentService = Depends(get_artifact_content_service),
) -> ArtifactContentResponse:
    """Raw source of a shared artifact version, for the recipient's code view.

    The parallel of ``GET /artifacts/{id}/content``, which builds its
    lookup key from the authenticated session and must stay that way —
    a recipient is not the owner, so that route can never serve them.

    ############################################################
    # SECURITY: the ACL check is the first thing that happens, and
    # `get_for_viewer` is what performs it. Only after it admits the
    # viewer may the owner's id be handed to ArtifactContentService,
    # which does no access control of its own and will read whatever
    # partition it is given. Resolving the owner before (or without)
    # the ACL check turns this route into read-any-artifact-by-id.
    ############################################################

    The bytes are inert text the SPA highlights client-side — never
    executed. Markdown is unwrapped back to the authored source, and an
    oversized artifact 413s so the recipient is steered to download.
    """
    try:
        share = shares.get_for_viewer(share_id=share_id, viewer=user)
        body, content_type = content.get(
            owner_id=str(share["owner_id"]),
            artifact_id=str(share["artifact_id"]),
            version=int(share["version"]),
        )
    except ShareNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Share not found")
    except ShareAccessDeniedError:
        logger.info(
            "shared artifact content denied share=%s viewer=%s",
            scrub_log(share_id),
            scrub_log(user.user_id),
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    except ArtifactNotFoundError:
        # The share outlived the version it points at, or its content
        # object is gone.
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
        logger.exception("shared artifact content fetch failed")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Artifact content is temporarily unavailable",
        )

    return ArtifactContentResponse(
        content=body,
        content_type=content_type,
        version=int(share["version"]),
    )
