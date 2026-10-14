"""Sync-policy routes — manage scheduled re-index of assistant KB sources.

All routes are edit-gated (owner or editor share), matching the document
management surface: whoever can add knowledge can decide whether it stays
fresh. Every mutation is plain data — nothing here schedules work directly;
the dispatcher's DueSyncIndex sweep remains the single trigger
(docs/specs/assistant-kb-sync.md §3).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.assistants.service import resolve_assistant_permission
from apis.shared.feature_flags import folder_sync_enabled
from apis.shared.sync_policies.models import parse_folder_source_ref
from apis.shared.sync_policies.service import (
    DuplicateSyncPolicy,
    RunNowCooldown,
    SyncPolicyLimitExceeded,
    change_policy_interval,
    create_sync_policy,
    delete_reauth_marker,
    delete_sync_policy,
    get_sync_policy,
    list_sync_policies,
    set_policy_state,
    trigger_run_now,
)
from apis.shared.sync_policies.service import _get_current_timestamp  # timestamp house-style

from apis.app_api.kb_sync import records
from apis.app_api.sync_policies.models import (
    CreateSyncPolicyRequest,
    SyncPoliciesListResponse,
    SyncPolicyResponse,
    UpdateSyncPolicyRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assistants/{assistant_id}/sync-policies", tags=["sync-policies"])


async def _require_edit_permission(assistant_id: str, current_user: User) -> str:
    """Owner or editor share required — same gate as the documents surface."""
    assistant, permission = await resolve_assistant_permission(
        assistant_id=assistant_id, user_id=current_user.user_id, user_email=current_user.email
    )
    if not assistant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Assistant not found: {assistant_id}")
    if permission not in ("owner", "editor"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage sync for this assistant",
        )
    return assistant.owner_id


async def _validate_folder_source(source_ref: str, current_user: User) -> None:
    """A ``drive_folder`` source (spec §11.1): the flag is on, the reference
    names a connector and a folder, and the caller can use that connector as a
    file source, since every run fetches with their credentials. The folder
    itself is the worker's to find: a run on a folder that is gone counts a
    not-found strike."""
    if not folder_sync_enabled():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Folder sync is not enabled")
    parsed = parse_folder_source_ref(source_ref)
    if parsed is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A folder sync source is {connectorId}:{folderId}",
        )
    # Function-local: file_sources.service pulls FastAPI, like this module.
    from apis.app_api.file_sources.service import resolve_file_source
    from apis.shared.oauth.provider_repository import get_provider_repository
    from apis.shared.rbac.service import get_app_role_service

    await resolve_file_source(parsed[0], current_user, get_provider_repository(), get_app_role_service())


def _validate_source(assistant_id: str, source_type: str, source_ref: str) -> None:
    """The source must exist before a schedule can cover it; drive_file
    sources additionally need import provenance to re-fetch from, and must
    not already be kept fresh by a folder policy."""
    source = records.get_source_item(assistant_id, source_type, source_ref)
    if source is None or source.get("status") == "deleting":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sync source not found: {source_ref}",
        )
    if source_type == "drive_file" and not source.get("sourceFileId"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Device-uploaded documents have no external source to sync from",
        )
    if source_type == "drive_file" and source.get("syncPolicyId"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This file is already kept in sync with its folder",
        )


@router.post("", response_model=SyncPolicyResponse, status_code=status.HTTP_201_CREATED)
async def create_policy(
    assistant_id: str,
    request: CreateSyncPolicyRequest,
    current_user: User = Depends(get_current_user_from_session),
) -> SyncPolicyResponse:
    await _require_edit_permission(assistant_id, current_user)
    if request.source_type == "drive_folder":
        await _validate_folder_source(request.source_ref, current_user)
    else:
        _validate_source(assistant_id, request.source_type, request.source_ref)

    # Only a folder carries a display name; every other source is named by its record.
    folder_name = {"source_name": request.source_name} if request.source_type == "drive_folder" else {}
    try:
        policy = await create_sync_policy(
            assistant_id=assistant_id,
            source_type=request.source_type,
            source_ref=request.source_ref,
            interval=request.interval,
            created_by_user_id=current_user.user_id,
            **folder_name,
        )
    except DuplicateSyncPolicy:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This source already has a sync policy"
        )
    except SyncPolicyLimitExceeded as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    if request.source_type == "drive_file":
        records.update_document_sync_fields(assistant_id, request.source_ref, sync_policy_id=policy.policy_id)

    return SyncPolicyResponse.from_policy(policy)


@router.get("", response_model=SyncPoliciesListResponse)
async def list_policies(
    assistant_id: str,
    current_user: User = Depends(get_current_user_from_session),
) -> SyncPoliciesListResponse:
    await _require_edit_permission(assistant_id, current_user)
    policies = await list_sync_policies(assistant_id)
    return SyncPoliciesListResponse(policies=[SyncPolicyResponse.from_policy(p) for p in policies])


@router.patch("/{policy_id}", response_model=SyncPolicyResponse)
async def update_policy(
    assistant_id: str,
    policy_id: str,
    request: UpdateSyncPolicyRequest,
    current_user: User = Depends(get_current_user_from_session),
) -> SyncPolicyResponse:
    await _require_edit_permission(assistant_id, current_user)
    policy = await get_sync_policy(assistant_id, policy_id)
    if policy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Sync policy not found: {policy_id}")

    if request.state == "active" and policy.state == "paused_reauth":
        # Only a fresh OAuth consent resumes a reauth pause — resuming here
        # would just re-fail and burn a run.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Reconnect the content source to resume syncing",
        )

    if request.interval is not None and request.interval != policy.interval:
        policy = await change_policy_interval(assistant_id, policy_id, request.interval)

    if request.state is not None and request.state != policy.state:
        if request.state == "active":
            await set_policy_state(assistant_id, policy_id, "active", next_sync_at=_get_current_timestamp())
        else:
            await set_policy_state(assistant_id, policy_id, "paused_user", state_reason="Paused by user")
        policy = await get_sync_policy(assistant_id, policy_id)

    return SyncPolicyResponse.from_policy(policy)


@router.delete("/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy(
    assistant_id: str,
    policy_id: str,
    current_user: User = Depends(get_current_user_from_session),
) -> None:
    await _require_edit_permission(assistant_id, current_user)
    policy = await get_sync_policy(assistant_id, policy_id)
    if policy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Sync policy not found: {policy_id}")

    await delete_sync_policy(assistant_id, policy_id)
    await delete_reauth_marker(policy.created_by_user_id, policy_id)

    if policy.source_type == "web_crawl":
        # Un-synced crawl jobs go back to normal 30-day auto-expiry.
        from apis.app_api.web_sources.crawl_repository import restore_crawl_ttl

        await restore_crawl_ttl(assistant_id=assistant_id, crawl_id=policy.source_ref)
    elif policy.source_type == "drive_folder":
        # The folder's documents stay, as ordinary imported files.
        records.clear_sync_policy_back_pointers(assistant_id, policy_id)
    else:
        records.clear_document_sync_policy_id(assistant_id, policy.source_ref)

    return None


@router.post("/{policy_id}/run-now", response_model=SyncPolicyResponse, status_code=status.HTTP_202_ACCEPTED)
async def run_now(
    assistant_id: str,
    policy_id: str,
    current_user: User = Depends(get_current_user_from_session),
) -> SyncPolicyResponse:
    """Make the policy due immediately. Flows through the normal dispatcher
    sweep, so every runaway guard still applies; ≥10-minute cooldown."""
    await _require_edit_permission(assistant_id, current_user)
    try:
        policy = await trigger_run_now(assistant_id, policy_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Sync policy not found: {policy_id}")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except RunNowCooldown:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="A manual sync was already requested recently; try again in a few minutes",
        )
    return SyncPolicyResponse.from_policy(policy)
