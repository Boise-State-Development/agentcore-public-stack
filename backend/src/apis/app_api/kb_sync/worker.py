"""KB sync worker — executes a single policy's sync run.

Drive-file path (docs/specs/assistant-kb-sync.md §6.1): resolve the
policy creator's stored Google token from the AgentCore Identity vault
(no live user session — pure IAM + vaulted refresh token), gate on
cheap change detection, and only when bytes actually changed, stage
them to the document's existing S3 key — which re-runs the whole
existing ingestion pipeline (chunk → embed → overwrite vectors)
untouched.

Change detection is two gates, cheapest first:
1. Drive `files.get` metadata — `version` compared against the stored
   `sourceEtag`. Imports capture Drive's `version` as the provenance
   etag (FileEntry.etag), so this stays continuous with documents
   imported before sync existed. `version` over-triggers (bumps on
   comments/metadata), which the second gate absorbs.
2. sha256 of the downloaded bytes vs the stored `contentHash` — the
   authoritative gate; identical bytes never reach Docling/Titan.

Pause/breaker outcomes (spec §7):
- vault says re-consent (TokenResult.requires_consent) or Google 401/403
  → policy paused_reauth; only a fresh consent resumes it (PR-5 hook)
- Drive 404 (deleted OR unshared — indistinguishable) → failed run with
  not_found=True; the dispatcher pauses after 2 consecutive
- trashed=true → "skipped" (grace state — recoverable from trash)
- anything else → failed run; dispatcher backoff/breaker handles it

Web re-crawl (source_type == "web_crawl") lands in PR-4 — until then
those runs record "skipped".

Payload contract with the dispatcher:
    {"policyId", "assistantId", "sourceType", "sourceRef"}
"""

import asyncio
import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from apis.shared.oauth.agentcore_identity import (
    AgentCoreIdentityClient,
    WorkloadTokenUnavailableError,
    custom_parameters_for,
)
from apis.shared.oauth.provider_repository import OAuthProviderRepository
from apis.shared.sync_policies.models import SyncPolicy
from apis.shared.sync_policies.service import get_sync_policy, record_sync_result, set_policy_state

from apis.app_api.file_sources.models import (
    FileSourceAuthError,
    FileSourceError,
    FileSourceNotFoundError,
)
from apis.app_api.file_sources.registry import registry
from apis.app_api.kb_sync import records

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_NATIVE_PREFIX = "application/vnd.google-apps."


def _now_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat() + "Z"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _stage_to_s3(s3_key: str, content: bytes, content_type: str) -> None:
    """Overwrite the document's existing S3 object — the bucket's
    ObjectCreated event re-runs the ingestion pipeline on the new bytes."""
    import boto3

    bucket = os.environ["S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME"]
    boto3.client("s3").put_object(Bucket=bucket, Key=s3_key, Body=content, ContentType=content_type)


async def _resolve_access_token(provider, user_id: str) -> Optional[str]:
    """Vault token for the policy creator; None means re-consent needed.

    Mirrors file_sources.service.resolve_file_source_token (which we
    can't import here — it pulls FastAPI): same provider identity, same
    scopes, and critically the same customParameters — they're part of
    the vault key, and a mismatched map falsely reports consent-required.
    """
    client = AgentCoreIdentityClient()
    result = await client.get_token_for_user(
        provider_name=provider.provider_id,
        scopes=provider.scopes,
        user_id=user_id,
        custom_parameters=custom_parameters_for(
            provider.provider_type.value if provider.provider_type else None,
            provider.custom_parameters,
            force_authentication=True,
        ),
        callback_url=os.environ.get("AGENTCORE_LOCAL_OAUTH_CALLBACK_URL"),
    )
    if result.requires_consent:
        return None
    return result.access_token


async def _pause_reauth(policy: SyncPolicy, detail: str) -> Dict[str, Any]:
    logger.warning(f"Sync policy {policy.policy_id}: credentials need re-consent ({detail})")
    # "skipped" (not "failed"): resets the breaker streaks and clears the
    # run stamp — reauth is its own terminal state, not a failure count.
    await record_sync_result(policy.assistant_id, policy.policy_id, "skipped")
    await set_policy_state(
        policy.assistant_id,
        policy.policy_id,
        "paused_reauth",
        state_reason="Reconnect Google Drive to resume syncing",
    )
    return {"policyId": policy.policy_id, "result": "paused_reauth"}


async def _finish(policy: SyncPolicy, result: str, not_found: bool = False) -> Dict[str, Any]:
    await record_sync_result(policy.assistant_id, policy.policy_id, result, not_found=not_found)
    return {"policyId": policy.policy_id, "result": result}


async def _sync_drive_file(policy: SyncPolicy) -> Dict[str, Any]:
    assistant_id = policy.assistant_id
    document = records.get_document_item(assistant_id, policy.source_ref)
    if document is None or document.get("status") == "deleting":
        # Dispatcher's liveness check races a just-deleted doc; nothing to do.
        logger.info(f"Sync policy {policy.policy_id}: document {policy.source_ref} gone; skipping")
        return await _finish(policy, "skipped")

    connector_id = document.get("sourceConnectorId")
    file_id = document.get("sourceFileId")
    adapter_key = document.get("sourceAdapterKey") or "google-drive"
    if not connector_id or not file_id:
        logger.error(f"Sync policy {policy.policy_id}: document {policy.source_ref} has no import provenance")
        await set_policy_state(
            assistant_id, policy.policy_id, "paused_error", state_reason="document has no import source"
        )
        return await _finish(policy, "skipped")

    adapter = registry.get(adapter_key)
    if adapter is None:
        await set_policy_state(
            assistant_id, policy.policy_id, "paused_error", state_reason=f"adapter '{adapter_key}' not available"
        )
        return await _finish(policy, "skipped")

    provider = await OAuthProviderRepository().get_provider(connector_id)
    if provider is None:
        await set_policy_state(
            assistant_id, policy.policy_id, "paused_error", state_reason="connector no longer configured"
        )
        return await _finish(policy, "skipped")

    try:
        access_token = await _resolve_access_token(provider, policy.created_by_user_id)
    except WorkloadTokenUnavailableError as e:
        # Operator/config error (workload env, IAM) — not the user's grant.
        logger.error(f"Sync policy {policy.policy_id}: workload token unavailable: {e}")
        return await _finish(policy, "failed")
    if access_token is None:
        return await _pause_reauth(policy, "vault returned authorization URL")

    try:
        # Gate 1 — metadata. Cheap; an unchanged corpus costs one
        # files.get per source per interval.
        metadata = await adapter.get_file_metadata(access_token, file_id)
        if metadata.get("trashed"):
            logger.info(f"Sync policy {policy.policy_id}: file {file_id} is trashed; grace-skipping")
            return await _finish(policy, "skipped")

        new_etag = str(metadata.get("version") or metadata.get("modifiedTime") or "")
        stored_etag = document.get("sourceEtag")
        if stored_etag and new_etag and str(stored_etag) == new_etag:
            return await _finish(policy, "unchanged")

        # Gate 2 — bytes. version over-triggers, so hash before re-embedding.
        downloaded = await adapter.download(access_token, file_id)
        content_hash = _sha256(downloaded.content)
        if document.get("contentHash") and document["contentHash"] == content_hash:
            # Content identical; advance the etag so gate 1 passes next run.
            records.update_document_sync_fields(
                assistant_id, policy.source_ref, source_etag=new_etag, last_synced_at=_now_timestamp()
            )
            return await _finish(policy, "unchanged")

        # Changed: stash the old chunk count for the ingestion tail-delete
        # (shrinkage cleanup) BEFORE staging, then overwrite the S3 object.
        previous_chunk_count = int(document.get("chunkCount") or 0)
        records.update_document_sync_fields(
            assistant_id,
            policy.source_ref,
            source_etag=new_etag,
            content_hash=content_hash,
            previous_chunk_count=previous_chunk_count,
            last_synced_at=_now_timestamp(),
        )
        _stage_to_s3(document["s3Key"], downloaded.content, downloaded.content_type)
        logger.info(
            f"Sync policy {policy.policy_id}: staged {len(downloaded.content)} changed bytes for "
            f"document {policy.source_ref} (prev chunks: {previous_chunk_count})"
        )
        return await _finish(policy, "changed")

    except FileSourceAuthError as e:
        # Provider-side revocation the vault can't see (Google rejected the
        # token). Same terminal state as consent-required.
        return await _pause_reauth(policy, str(e))
    except FileSourceNotFoundError:
        # Deleted OR unshared — Drive won't say which. Never delete our
        # indexed copy on a 404; strike the counter and let the dispatcher
        # pause at 2.
        logger.warning(f"Sync policy {policy.policy_id}: file {file_id} not found/accessible")
        return await _finish(policy, "failed", not_found=True)
    except FileSourceError as e:
        logger.error(f"Sync policy {policy.policy_id}: drive error: {e}")
        return await _finish(policy, "failed")


async def run_sync(payload: Dict[str, Any]) -> Dict[str, Any]:
    policy_id = payload["policyId"]
    assistant_id = payload["assistantId"]

    policy = await get_sync_policy(assistant_id, policy_id)
    if policy is None:
        logger.info(f"KB sync worker: policy {policy_id} no longer exists; dropping run")
        return {"policyId": policy_id, "result": "dropped"}

    if policy.source_type == "drive_file":
        try:
            return await _sync_drive_file(policy)
        except Exception as e:
            # Last-resort catch: always record the run so the stamp clears
            # and the breaker counts, never leave a policy half-run.
            logger.error(f"KB sync worker: unexpected failure on policy {policy_id}: {e}", exc_info=True)
            return await _finish(policy, "failed")

    # web_crawl lands in PR-4.
    logger.info(f"KB sync worker: source_type {policy.source_type} not implemented yet; skipping")
    return await _finish(policy, "skipped")


def lambda_handler(event, context):
    """Async-invoke entry point (InvocationType=Event from the dispatcher)."""
    return asyncio.run(run_sync(event))
