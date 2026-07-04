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

Web re-crawl (source_type == "web_crawl", spec §6.2): re-runs the
policy's CrawlJob with its stored (already-capped) settings in the
crawler's refresh/upsert mode — conditional GET per page, content-hash
gating, no duplicate documents — then applies miss accounting: a page
absent from 2 consecutive re-crawls is deleted (transient outages
don't count; fetch failures are "seen"). The crawl job finalizes
WITHOUT its usual 30-day TTL so the policy's source record can't
auto-expire out from under the schedule.

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
        custom_parameters=custom_parameters_for(provider.custom_parameters),
        callback_url=os.environ.get("AGENTCORE_LOCAL_OAUTH_CALLBACK_URL"),
    )
    if result.requires_consent:
        return None
    return result.access_token


async def _pause_reauth(policy: SyncPolicy, detail: str, provider_id: Optional[str] = None) -> Dict[str, Any]:
    # Log the exact (workload identity, userId) the vault lookup used. The
    # token is keyed by that pair, so a "requires consent" almost always means
    # the token was vaulted under a DIFFERENT workload — e.g. a consent done
    # via local dev (AGENTCORE_RUNTIME_WORKLOAD_NAME=local_dev_inference)
    # can't be read by the deployed worker (platform-workload). Surfacing both
    # turns an opaque reauth pause into a one-line mismatch diagnosis.
    logger.warning(
        f"Sync policy {policy.policy_id}: credentials need re-consent ({detail}); "
        f"vault lookup used workload={os.environ.get('AGENTCORE_RUNTIME_WORKLOAD_NAME')!r} "
        f"userId={policy.created_by_user_id!r} provider={provider_id!r} — a token vaulted "
        f"under a different workload identity will read as consent-required here"
    )
    # "skipped" (not "failed"): resets the breaker streaks and clears the
    # run stamp — reauth is its own terminal state, not a failure count.
    await record_sync_result(policy.assistant_id, policy.policy_id, "skipped")
    await set_policy_state(
        policy.assistant_id,
        policy.policy_id,
        "paused_reauth",
        state_reason="Reconnect Google Drive to resume syncing",
    )
    if provider_id:
        # Marker lets the consent-completion hook find this policy without
        # a table scan; resume re-verifies, so best-effort is fine here.
        from apis.shared.sync_policies.service import put_reauth_marker

        await put_reauth_marker(policy.created_by_user_id, policy.assistant_id, policy.policy_id, provider_id)
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
        return await _pause_reauth(policy, "vault returned authorization URL", provider_id=connector_id)

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
        return await _pause_reauth(policy, str(e), provider_id=connector_id)
    except FileSourceNotFoundError:
        # Deleted OR unshared — Drive won't say which. Never delete our
        # indexed copy on a 404; strike the counter and let the dispatcher
        # pause at 2.
        logger.warning(f"Sync policy {policy.policy_id}: file {file_id} not found/accessible")
        return await _finish(policy, "failed", not_found=True)
    except FileSourceError as e:
        logger.error(f"Sync policy {policy.policy_id}: drive error: {e}")
        return await _finish(policy, "failed")


async def _delete_missing_web_document(assistant_id: str, item: Dict[str, Any], owner_id: str) -> None:
    """Soft-delete a page that vanished from 2 consecutive re-crawls, and
    run the resource cleanup inline (no request context to fire-and-forget
    from — the Lambda can afford to wait)."""
    from apis.app_api.documents.services.cleanup_service import cleanup_document_resources
    from apis.app_api.documents.services.document_service import soft_delete_document

    document_id = item["documentId"]
    document = await soft_delete_document(assistant_id, document_id, owner_id)
    if document is None:
        return
    await cleanup_document_resources(
        document_id=document_id,
        assistant_id=assistant_id,
        s3_key=document.s3_key,
        chunk_count=document.chunk_count,
        source_connector_id=document.source_connector_id,
        source_file_id=document.source_file_id,
    )


async def _sync_web_crawl(policy: SyncPolicy) -> Dict[str, Any]:
    """Re-run the policy's crawl with its stored (already-capped) settings
    in upsert/refresh mode, then apply the 2-consecutive-miss deletion rule
    (docs/specs/assistant-kb-sync.md §6.2)."""
    from apis.app_api.documents.models import DocumentProvenance
    from apis.app_api.documents.services.document_service import _generate_document_id, create_document
    from apis.app_api.documents.services.storage_service import _get_s3_key, _sanitize_filename
    from apis.app_api.web_sources import crawler
    from apis.app_api.web_sources.crawl_repository import get_crawl_job, reset_crawl_for_refresh
    from apis.app_api.web_sources.url_utils import normalize_url, url_extension_hint

    assistant_id = policy.assistant_id
    job = await get_crawl_job(assistant_id, policy.source_ref)
    if job is None:
        # Dispatcher liveness races a just-expired/deleted job.
        await set_policy_state(
            assistant_id, policy.policy_id, "paused_error", state_reason="crawl configuration no longer exists"
        )
        return await _finish(policy, "skipped")

    root = normalize_url(job.root_url)

    # Existing web pages under this root, keyed by their normalized URL.
    web_docs: Dict[str, Dict[str, Any]] = {}
    for item in records.list_document_items(assistant_id):
        url = str(item.get("sourceFileId") or "")
        if (
            item.get("sourceConnectorId") == "web"
            and url.startswith(root)
            and item.get("status") != "deleting"
        ):
            web_docs[url] = item

    now = _now_timestamp()

    async def on_result(url: str, document_id: str, outcome: str, etag, content_hash) -> None:
        if outcome == "changed":
            # BEFORE the S3 overwrite: stash the previous chunk count for
            # the ingestion tail-delete, alongside the new gate values.
            records.update_document_sync_fields(
                assistant_id,
                document_id,
                source_etag=etag,
                content_hash=content_hash,
                previous_chunk_count=int(web_docs[url].get("chunkCount") or 0),
                last_synced_at=now,
            )
        elif outcome == "unchanged":
            records.update_document_sync_fields(
                assistant_id, document_id, source_etag=etag, content_hash=content_hash, last_synced_at=now
            )
        elif outcome == "created":
            # The crawler's own metadata update owns etag/filename; we add
            # the hash so the next refresh can gate on it.
            records.update_document_sync_fields(
                assistant_id, document_id, content_hash=content_hash, last_synced_at=now
            )

    refresh = crawler.RefreshState(
        docs={
            url: crawler.RefreshDoc(
                document_id=item["documentId"],
                source_etag=item.get("sourceEtag"),
                content_hash=item.get("contentHash"),
                chunk_count=int(item.get("chunkCount") or 0),
            )
            for url, item in web_docs.items()
        },
        on_result=on_result,
    )

    # Root document: reuse if alive, else recreate (mirrors the crawl route).
    root_item = web_docs.get(root)
    if root_item is not None:
        root_document_id = root_item["documentId"]
    else:
        root_document_id = _generate_document_id()
        filename = f"{_sanitize_filename(url_extension_hint(root))}.html"
        await create_document(
            assistant_id=assistant_id,
            filename=filename,
            content_type="text/html",
            size_bytes=0,
            s3_key=_get_s3_key(assistant_id, root_document_id, filename),
            document_id=root_document_id,
            provenance=DocumentProvenance(
                source_connector_id="web",
                source_adapter_key="http",
                source_file_id=root,
                imported_by_user_id=policy.created_by_user_id,
            ),
        )

    if not await reset_crawl_for_refresh(assistant_id=assistant_id, crawl_id=job.crawl_id):
        await set_policy_state(
            assistant_id, policy.policy_id, "paused_error", state_reason="crawl configuration no longer exists"
        )
        return await _finish(policy, "skipped")

    # finalize_with_ttl=False: a sync-covered crawl job must never TTL-expire
    # out from under its policy. budget_seconds shaves the crawler's default
    # 15-minute ceiling so finalize + miss accounting fit inside this
    # Lambda's own 15-minute timeout.
    await crawler.run_crawl(
        assistant_id=assistant_id,
        crawl_id=job.crawl_id,
        user_id=policy.created_by_user_id,
        root_url=job.root_url,
        settings=job.settings,
        root_document_id=root_document_id,
        refresh=refresh,
        finalize_with_ttl=False,
        budget_seconds=13 * 60,
    )

    # Miss accounting: pages that never survived the robots gate this run.
    # Fetch failures ARE in seen_urls (transient outage ≠ gone); only a
    # 2-consecutive-run absence deletes.
    assistant_item = records.get_assistant_item(assistant_id)
    owner_id = str(assistant_item.get("ownerId")) if assistant_item else policy.created_by_user_id
    deleted = 0
    for url, item in web_docs.items():
        if url in refresh.seen_urls:
            if int(item.get("consecutiveMisses") or 0) > 0:
                records.set_document_miss_count(assistant_id, item["documentId"], 0)
            continue
        misses = int(item.get("consecutiveMisses") or 0) + 1
        if misses >= 2:
            logger.info(f"Sync policy {policy.policy_id}: {url} missing {misses} consecutive runs; deleting")
            await _delete_missing_web_document(assistant_id, item, owner_id)
            deleted += 1
        else:
            records.set_document_miss_count(assistant_id, item["documentId"], misses)

    logger.info(
        f"Sync policy {policy.policy_id}: re-crawl done — {refresh.changed} changed, "
        f"{refresh.created} new, {refresh.unchanged} unchanged, {deleted} deleted"
    )
    result = "changed" if (refresh.changed or refresh.created or deleted) else "unchanged"
    return await _finish(policy, result)


async def run_sync(payload: Dict[str, Any]) -> Dict[str, Any]:
    policy_id = payload["policyId"]
    assistant_id = payload["assistantId"]

    policy = await get_sync_policy(assistant_id, policy_id)
    if policy is None:
        logger.info(f"KB sync worker: policy {policy_id} no longer exists; dropping run")
        return {"policyId": policy_id, "result": "dropped"}

    sync_fn = _sync_drive_file if policy.source_type == "drive_file" else _sync_web_crawl
    try:
        return await sync_fn(policy)
    except Exception as e:
        # Last-resort catch: always record the run so the stamp clears
        # and the breaker counts, never leave a policy half-run.
        logger.error(f"KB sync worker: unexpected failure on policy {policy_id}: {e}", exc_info=True)
        return await _finish(policy, "failed")


def lambda_handler(event, context):
    """Async-invoke entry point (InvocationType=Event from the dispatcher)."""
    return asyncio.run(run_sync(event))
