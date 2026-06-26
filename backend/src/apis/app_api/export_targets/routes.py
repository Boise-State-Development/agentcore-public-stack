"""User-facing export-target endpoints: catalog + save-a-conversation.

A connector becomes an *export target* only once an admin maps it to an
export-target adapter (the write-direction mirror of file sources). These
endpoints let a signed-in user discover which of their connectors can receive
a conversation and push a full transcript out to one ("Save this chat to
Google Drive").

`GET /export-targets` is the catalog the SPA's "Save to…" dialog reads to
populate its connector picker (and, per connector, which output formats the
destination accepts). `POST /sessions/{id}/export` renders the conversation
and creates the document via the resolved adapter.

Like the connector and file-source routes, these live on the app API: the
AgentCore Runtime that fronts the inference API only proxies `/invocations`
and `/ping`, so custom paths are unreachable there. The app API can mint
per-user OAuth tokens via the workload identity, which is exactly what the
export write needs.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from apis.shared.auth import User, get_current_user_from_session
from apis.shared.oauth.agentcore_identity import (
    CallbackUrlUnavailableError,
    WorkloadTokenUnavailableError,
)
from apis.shared.oauth.disconnect_repository import (
    OAuthDisconnectRepository,
    get_disconnect_repository,
)
from apis.shared.oauth.models import OAuthProvider
from apis.shared.oauth.provider_repository import (
    OAuthProviderRepository,
    get_provider_repository,
)
from apis.shared.rbac.service import AppRoleService, get_app_role_service
from apis.shared.sessions.messages import get_messages
from apis.shared.sessions.metadata import add_export_receipt, get_session_metadata
from apis.shared.sessions.models import ExportReceipt, MessageResponse

from apis.app_api.export_targets.models import (
    ExportFormat,
    ExportInclude,
    ExportTargetError,
)
from apis.app_api.export_targets.registry import registry
from apis.app_api.file_sources.registry import registry as file_source_registry
from apis.app_api.export_targets.render import render_transcript
from apis.app_api.export_targets.service import (
    connector_visible_to_user,
    http_error_for_export_target_error,
    require_export_target_token,
    resolve_export_target,
    resolve_export_target_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["export-targets"])

# Page the transcript out in chunks rather than one unbounded read. The cap is
# a runaway guard, not a product limit; if a conversation is somehow longer we
# log and export what we have rather than silently truncating without a trace.
_EXPORT_PAGE_SIZE = 200
_MAX_EXPORT_PAGES = 100


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class ExportTargetConnector(BaseModel):
    """A connector the current user can save a conversation to."""

    model_config = ConfigDict(populate_by_name=True)

    provider_id: str = Field(..., alias="providerId")
    display_name: str = Field(..., alias="displayName")
    icon_name: str = Field(..., alias="iconName")
    icon_data: Optional[str] = Field(None, alias="iconData")
    # True when AgentCore's vault holds a usable token for this user — the SPA
    # can save straight away. False means it must run the consent flow first.
    connected: bool
    # The output formats this destination accepts, so the dialog's format
    # picker offers only what the adapter can actually produce.
    supported_formats: List[str] = Field(..., alias="supportedFormats")
    # True when this connector is also mapped as a file source, so the SPA can
    # reuse the import browse dialog to pick a destination folder. Only the
    # combined-scope Drive connector (drive.readonly + drive.file) qualifies —
    # `drive.file` alone cannot list folders. False means the export lands in
    # the adapter's default app folder and the SPA hides the folder picker.
    browsable: bool


class ExportTargetListResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    export_targets: List[ExportTargetConnector] = Field(..., alias="exportTargets")


# ---------------------------------------------------------------------------
# Export request / response
# ---------------------------------------------------------------------------


class ExportRequest(BaseModel):
    """Body for `POST /sessions/{id}/export`."""

    model_config = ConfigDict(populate_by_name=True)

    connector_id: str = Field(..., alias="connectorId", description="Connector to save to")
    format: ExportFormat = Field(
        ExportFormat.GOOGLE_DOC, description="Output format; defaults to a native Google Doc"
    )
    parent_id: Optional[str] = Field(
        None,
        alias="parentId",
        description="Destination folder id (v2 picker); omit to use the app's default folder",
    )
    include: Optional[ExportInclude] = Field(
        None, description="Which conversation elements to include; omit for defaults"
    )


class ExportResponse(BaseModel):
    """Result of a successful export."""

    model_config = ConfigDict(populate_by_name=True)

    file_id: str = Field(..., alias="fileId")
    name: str
    web_view_link: Optional[str] = Field(None, alias="webViewLink")
    # The persisted receipt, so the SPA can update its local session state
    # without re-fetching metadata.
    receipt: ExportReceipt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _is_connected(
    provider: OAuthProvider,
    user_id: str,
    disconnect_repo: OAuthDisconnectRepository,
) -> bool:
    """Best-effort check of whether the user has a usable token.

    Mirrors the file-source catalog: a prior disconnect wins over a still-valid
    vault entry, and workload/callback misconfiguration is treated as "not
    connected" rather than failing the whole catalog — the user gets the
    actionable 503 when they try to save.
    """
    if await disconnect_repo.is_disconnected(user_id, provider.provider_id):
        return False
    try:
        result = await resolve_export_target_token(provider, user_id)
    except (WorkloadTokenUnavailableError, CallbackUrlUnavailableError) as err:
        logger.warning(
            "Export-target connectivity check failed for %s: %s",
            provider.provider_id,
            err,
        )
        return False
    return not result.requires_consent


def _is_browsable(provider: OAuthProvider) -> bool:
    """True when the connector can also back the import browse dialog.

    The destination folder picker reuses the file-source `roots`/`browse`
    endpoints, which only resolve when the connector is mapped to a shipped
    file-source adapter (the combined-scope Drive connector). An export-only
    connector has no folder picker; its exports land in the app folder.
    """
    adapter_id = provider.file_source_adapter_id
    return bool(adapter_id) and file_source_registry.get(adapter_id) is not None


async def _collect_transcript(session_id: str, user_id: str) -> List[MessageResponse]:
    """Page the whole conversation into a single chronological list.

    Pages are sequence-ordered and returned oldest-first, so concatenating
    them preserves chronology. Stops at the runaway-guard page cap.
    """
    messages: List[MessageResponse] = []
    next_token: Optional[str] = None
    for _ in range(_MAX_EXPORT_PAGES):
        page = await get_messages(
            session_id=session_id,
            user_id=user_id,
            limit=_EXPORT_PAGE_SIZE,
            next_token=next_token,
        )
        messages.extend(page.messages)
        next_token = page.next_token
        if not next_token:
            return messages
    logger.warning(
        "Export for session %s hit the %d-page cap; transcript may be truncated",
        session_id,
        _MAX_EXPORT_PAGES,
    )
    return messages


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/export-targets", response_model=ExportTargetListResponse)
async def list_export_targets(
    current_user: User = Depends(get_current_user_from_session),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    role_service: AppRoleService = Depends(get_app_role_service),
    disconnect_repo: OAuthDisconnectRepository = Depends(get_disconnect_repository),
) -> ExportTargetListResponse:
    """List the connectors the current user can save a conversation to.

    A connector qualifies when it is enabled, mapped to an export-target
    adapter that ships in this release, and visible to the user's roles.
    `connected` reflects whether the user already has a usable OAuth token, so
    the SPA can decide between "Save" and "Connect"; `supportedFormats` drives
    the format picker.
    """
    permissions = await role_service.resolve_user_permissions(current_user)
    providers = await provider_repo.list_providers(enabled_only=True)

    candidates = []
    for provider in providers:
        if not provider.export_target_adapter_id:
            continue
        if not connector_visible_to_user(provider, permissions.app_roles):
            continue
        adapter = registry.get(provider.export_target_adapter_id)
        if adapter is None:
            # Admin mapped an adapter key that no longer ships — hide it rather
            # than offer a destination that would 404 on save.
            logger.warning(
                "Connector %s maps to unknown export-target adapter '%s'; omitting from catalog",
                provider.provider_id,
                provider.export_target_adapter_id,
            )
            continue
        candidates.append((provider, adapter))

    connected_flags = await asyncio.gather(
        *(
            _is_connected(provider, current_user.user_id, disconnect_repo)
            for provider, _ in candidates
        )
    )

    return ExportTargetListResponse(
        export_targets=[
            ExportTargetConnector(
                provider_id=provider.provider_id,
                display_name=provider.display_name,
                icon_name=provider.icon_name,
                icon_data=provider.icon_data,
                connected=connected,
                supported_formats=[
                    fmt.value for fmt in adapter.metadata.supported_formats
                ],
                browsable=_is_browsable(provider),
            )
            for (provider, adapter), connected in zip(candidates, connected_flags)
        ]
    )


@router.post("/sessions/{session_id}/export", response_model=ExportResponse)
async def export_session(
    session_id: str,
    request: ExportRequest,
    current_user: User = Depends(get_current_user_from_session),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    role_service: AppRoleService = Depends(get_app_role_service),
) -> ExportResponse:
    """Save a conversation transcript to a connected app.

    Resolves the connector to its export-target adapter, renders the full
    transcript in the requested format, and creates the document via the
    user's own OAuth token. A 409 means the user must complete the OAuth
    consent flow (the SPA reuses the connector consent popup, then retries).
    """
    user_id = current_user.user_id

    # Ownership + title source. Session metadata is keyed by user, so a missing
    # record means the session isn't the caller's (or doesn't exist) — 404
    # either way, matching the read path so a user can only export their own
    # conversations.
    metadata = await get_session_metadata(session_id, user_id)
    if not metadata:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}",
        )

    provider, adapter = await resolve_export_target(
        request.connector_id, current_user, provider_repo, role_service
    )

    if request.format not in adapter.metadata.supported_formats:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"'{provider.display_name}' cannot export to format "
                f"'{request.format.value}'"
            ),
        )

    # 409 (not connected) / 503 (no workload) raised here; the SPA's consent
    # retry hooks on the 409.
    access_token = await require_export_target_token(provider, user_id)

    messages = await _collect_transcript(session_id, user_id)

    try:
        rendered = render_transcript(
            metadata.title, messages, request.format, request.include
        )
    except ValueError as err:
        # The renderer can't produce this format (e.g. PDF) even though the
        # adapter claims it — a configuration mismatch, surfaced as 422.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(err)
        )

    try:
        created = await adapter.create_document(
            access_token,
            content=rendered.content,
            name=rendered.suggested_name,
            source_mime_type=rendered.mime_type,
            target_format=request.format,
            parent_id=request.parent_id,
        )
    except ExportTargetError as err:
        logger.warning(
            "create_document failed for connector %s: %s", request.connector_id, err
        )
        raise http_error_for_export_target_error(err)

    receipt = ExportReceipt(
        connector_id=provider.provider_id,
        adapter_key=adapter.metadata.key,
        format=request.format.value,
        file_id=created.file_id,
        file_name=created.name,
        web_view_link=created.web_view_link,
        exported_at=datetime.now(timezone.utc).isoformat(),
    )
    # Best-effort: swallows its own errors so a metadata-write hiccup never
    # fails an export that already succeeded.
    await add_export_receipt(session_id, user_id, receipt)

    return ExportResponse(
        file_id=created.file_id,
        name=created.name,
        web_view_link=created.web_view_link,
        receipt=receipt,
    )
