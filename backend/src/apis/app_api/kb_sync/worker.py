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

Drive folder (source_type == "drive_folder", spec §11): one policy for a
whole folder. Each run checks the folder still exists, re-lists it with the
folder-attachment walk and caps, applies the two gates above to every file a
document already holds (the listing carries Drive's version, so gate 1
costs no extra call), imports new files as documents with provenance, and
marks documents whose file left the folder rather than deleting them. Per-run
caps on downloads, bytes and wall clock bound it inside this Lambda's 15
minutes; what they leave is picked up by the next run.

Payload contract with the dispatcher:
    {"policyId", "assistantId", "sourceType", "sourceRef"}
"""

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from apis.shared.oauth.agentcore_identity import (
    AgentCoreIdentityClient,
    WorkloadTokenUnavailableError,
    custom_parameters_for,
)
from apis.shared.oauth.provider_repository import OAuthProviderRepository
from apis.shared.feature_flags import folder_sync_enabled
from apis.shared.sync_policies.models import FolderRunSummary, SyncPolicy, parse_folder_source_ref
from apis.shared.sync_policies.service import get_sync_policy, record_sync_result, set_policy_state

from apis.app_api.file_sources.adapter import FileSourceAdapter
from apis.app_api.file_sources.folder_walk import WALK_AUTH, WALK_NOT_FOUND, WalkedFile, walk_folder
from apis.app_api.file_sources.models import (
    DownloadedFile,
    FileSourceAuthError,
    FileSourceError,
    FileSourceNotFoundError,
    FileSourceTooLargeError,
)
from apis.app_api.file_sources.registry import registry
from apis.app_api.kb_sync import records

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _now_timestamp() -> str:
    # Normalize the +00:00 offset to a single trailing Z; "…+00:00Z" (offset AND
    # Z) is invalid ISO 8601 and renders as Invalid Date in strict JS engines,
    # leaving the last-synced time blank in the UI.
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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


async def _finish(
    policy: SyncPolicy, result: str, not_found: bool = False, summary: Optional[FolderRunSummary] = None
) -> Dict[str, Any]:
    await record_sync_result(policy.assistant_id, policy.policy_id, result, not_found=not_found, summary=summary)
    outcome: Dict[str, Any] = {"policyId": policy.policy_id, "result": result}
    if summary is not None:
        outcome["summary"] = summary.model_dump(by_alias=True)
    return outcome


def _stage_changed_document(
    assistant_id: str,
    document_id: str,
    document: Dict[str, Any],
    new_etag: str,
    downloaded: DownloadedFile,
    content_hash: str,
    sync_policy_id: Optional[str] = None,
) -> None:
    """Overwrite an existing document's S3 object with changed bytes.

    Stashes the old chunk count for the ingestion tail-delete (shrinkage
    cleanup) and marks the version being staged — what a managed KB's
    consumer uses to tell this overwrite from a redelivery — BEFORE staging,
    then overwrites the S3 object. Raises when the stage fails, after rolling
    the gates back: left advanced, the next run would match them and never
    stage this change. previousChunkCount is harmless to leave — the next
    changed run rewrites it before its own stage.
    """
    previous_chunk_count = int(document.get("chunkCount") or 0)
    synced_at = _now_timestamp()
    records.update_document_sync_fields(
        assistant_id,
        document_id,
        source_etag=new_etag,
        content_hash=content_hash,
        previous_chunk_count=previous_chunk_count,
        last_synced_at=synced_at,
        sync_policy_id=sync_policy_id,
        staged_content_hash=content_hash,
    )
    try:
        _stage_to_s3(document["s3Key"], downloaded.content, downloaded.content_type)
    except Exception:
        written = {
            "sourceEtag": new_etag,
            "contentHash": content_hash,
            "stagedContentHash": content_hash,
            "lastSyncedAt": synced_at,
        }
        records.rollback_document_sync_fields(
            assistant_id,
            document_id,
            written=written,
            previous={attribute: document.get(attribute) for attribute in written},
        )
        raise
    logger.info(
        f"Staged {len(downloaded.content)} changed bytes for document {document_id} "
        f"(prev chunks: {previous_chunk_count})"
    )


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

        # Changed: stage over the document's own S3 key.
        _stage_changed_document(assistant_id, policy.source_ref, document, new_etag, downloaded, content_hash)
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
    # document_id -> (values written before staging, values they replaced),
    # so a page whose stage then fails can be rolled back.
    pre_stage_writes: Dict[str, Any] = {}

    async def on_result(url: str, document_id: str, outcome: str, etag, content_hash) -> None:
        if outcome == "changed":
            # BEFORE the S3 overwrite: stash the previous chunk count for
            # the ingestion tail-delete and the staged version for a managed
            # KB's re-ingest, alongside the new gate values.
            records.update_document_sync_fields(
                assistant_id,
                document_id,
                source_etag=etag,
                content_hash=content_hash,
                previous_chunk_count=int(web_docs[url].get("chunkCount") or 0),
                last_synced_at=now,
                staged_content_hash=content_hash,
            )
            written = {"contentHash": content_hash, "stagedContentHash": content_hash, "lastSyncedAt": now}
            if etag is not None:
                written["sourceEtag"] = etag
            pre_stage_writes[document_id] = (
                written,
                {attribute: web_docs[url].get(attribute) for attribute in written},
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
            pre_stage_writes[document_id] = ({"contentHash": content_hash, "lastSyncedAt": now}, {})
        elif outcome == "stage_failed" and document_id in pre_stage_writes:
            # The page's gates advanced above but its bytes never reached S3;
            # roll them back so the next re-crawl stages it instead of
            # hash-matching a version the knowledge base never received.
            written, previous = pre_stage_writes.pop(document_id)
            records.rollback_document_sync_fields(assistant_id, document_id, written=written, previous=previous)

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
        f"{refresh.created} new, {refresh.unchanged} unchanged, {deleted} deleted, "
        f"{refresh.stage_failed} failed to stage"
    )
    # A page that failed to stage was counted as changed/created when it was
    # emitted, but nothing reached the knowledge base.
    staged = refresh.changed + refresh.created - refresh.stage_failed
    result = "changed" if (staged or deleted) else "unchanged"
    return await _finish(policy, result)


# ── Drive folder (spec §11) ─────────────────────────────────────────────────


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _folder_walk_caps() -> Tuple[int, int]:
    """The folder-attachment walk's caps (spec §11.5), shared on purpose: a
    synced folder lists exactly what an attached one would."""
    from apis.shared.files.folder_attachments import FOLDER_ATTACH_MAX_FILES, FOLDER_ATTACH_MAX_PAGES

    return FOLDER_ATTACH_MAX_FILES, FOLDER_ATTACH_MAX_PAGES


@dataclass
class _FolderRunBudget:
    """What one folder run may still spend (spec §11.5). Checked before each
    download; a file the budget refuses is ``deferred`` to the next run."""

    files_left: int
    bytes_left: int
    deadline: float
    max_file_bytes: int

    @classmethod
    def from_env(cls) -> "_FolderRunBudget":
        return cls(
            files_left=_env_int("KB_SYNC_FOLDER_MAX_FILES_PER_RUN", 50),
            bytes_left=_env_int("KB_SYNC_FOLDER_MAX_BYTES_PER_RUN", 250 * 1024 * 1024),
            deadline=time.monotonic() + _env_int("KB_SYNC_FOLDER_RUN_BUDGET_SECONDS", 12 * 60),
            max_file_bytes=_env_int("KB_SYNC_FOLDER_MAX_FILE_BYTES", 50 * 1024 * 1024),
        )

    def allows(self, size_hint: Optional[int]) -> bool:
        if self.files_left <= 0 or time.monotonic() >= self.deadline:
            return False
        return size_hint is None or size_hint <= self.bytes_left

    def spend(self, size: int) -> None:
        self.files_left -= 1
        self.bytes_left -= size


class _FolderAuthLost(Exception):
    """Google rejected the token part way through a run; the run stops."""


def _is_newer(document_time: Optional[str], file_time: Optional[str]) -> bool:
    """Whether a document was created after its file last changed in Drive."""
    from apis.shared.timestamps import from_iso

    if not document_time or not file_time:
        return False
    try:
        created, modified = from_iso(document_time), from_iso(file_time)
    except (ValueError, AttributeError):
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if modified.tzinfo is None:
        modified = modified.replace(tzinfo=timezone.utc)
    return created > modified


async def _download_for_folder(
    adapter: FileSourceAdapter, access_token: str, walked: WalkedFile, budget: _FolderRunBudget
) -> Tuple[str, Optional[DownloadedFile]]:
    """Fetch one file within the run's budget.

    Returns ``(outcome, bytes)``: ``ok`` with the bytes, or ``deferred`` (a
    run cap), ``skipped`` (over the per-file cap) or ``failed`` without.
    Raises :class:`_FolderAuthLost` when the provider rejects the token.
    """
    size = walked.entry.size_bytes
    if size is not None and size > budget.max_file_bytes:
        return "skipped", None
    if not budget.allows(size):
        return "deferred", None
    try:
        downloaded = await adapter.download(access_token, walked.entry.id, max_bytes=budget.max_file_bytes)
    except FileSourceAuthError as e:
        raise _FolderAuthLost(str(e)) from e
    except FileSourceTooLargeError:
        return "skipped", None
    except FileSourceError as e:
        logger.warning(f"Folder sync download failed ({type(e).__name__})")
        return "failed", None
    budget.spend(len(downloaded.content))
    return "ok", downloaded


async def _sync_folder_document(
    policy: SyncPolicy,
    adapter: FileSourceAdapter,
    access_token: str,
    walked: WalkedFile,
    document: Dict[str, Any],
    budget: _FolderRunBudget,
) -> str:
    """Bring one existing document up to date with its file in the folder.

    Returns the summary bucket: ``unchanged``, ``updated``, ``failed``,
    ``skipped`` or ``deferred``.
    """
    assistant_id = policy.assistant_id
    document_id = document["documentId"]
    listed_etag = str(walked.entry.etag or walked.entry.modified_at or "")

    if document.get("removedFromSource"):
        records.set_document_removed_from_source(assistant_id, document_id, False)
    adopt = policy.policy_id if document.get("syncPolicyId") != policy.policy_id else None

    # Gate 1: the listing already carries Drive's version.
    stored_etag = document.get("sourceEtag")
    if stored_etag and listed_etag and str(stored_etag) == listed_etag:
        if adopt:
            records.update_document_sync_fields(assistant_id, document_id, sync_policy_id=adopt)
        return "unchanged"

    # Adoption without a download (spec §11.3): an import records no version,
    # but a document created after the file last changed holds its bytes.
    if (
        not stored_etag
        and listed_etag
        and document.get("status") != "failed"
        and _is_newer(document.get("createdAt"), walked.entry.modified_at)
    ):
        records.update_document_sync_fields(
            assistant_id, document_id, source_etag=listed_etag, sync_policy_id=adopt
        )
        return "unchanged"

    if adopt:
        # Belongs to this policy from now on, whether or not this run fetches it,
        # so a later run can tell when its file leaves the folder.
        records.update_document_sync_fields(assistant_id, document_id, sync_policy_id=adopt)

    # Gate 2: bytes.
    outcome, downloaded = await _download_for_folder(adapter, access_token, walked, budget)
    if downloaded is None:
        return outcome
    content_hash = _sha256(downloaded.content)
    if document.get("contentHash") and document["contentHash"] == content_hash:
        records.update_document_sync_fields(
            assistant_id, document_id, source_etag=listed_etag, last_synced_at=_now_timestamp()
        )
        return "unchanged"
    try:
        _stage_changed_document(assistant_id, document_id, document, listed_etag, downloaded, content_hash)
    except Exception as e:  # noqa: BLE001 - one file's failure keeps its old version
        logger.error(f"Sync policy {policy.policy_id}: could not stage document {document_id}: {type(e).__name__}")
        return "failed"
    return "updated"


async def _import_folder_file(
    policy: SyncPolicy,
    adapter: FileSourceAdapter,
    access_token: str,
    connector_id: str,
    walked: WalkedFile,
    budget: _FolderRunBudget,
) -> str:
    """Import a file new to the folder as a document with provenance (spec §11.2).

    Downloads first, so a file that cannot be fetched leaves nothing behind.
    Returns ``added``, ``failed``, ``skipped`` or ``deferred``.
    """
    from apis.app_api.documents.models import DocumentProvenance
    from apis.app_api.documents.services.document_service import (
        _generate_document_id,
        create_document,
        update_document_status,
    )
    from apis.app_api.documents.services.storage_service import _get_s3_key, _sanitize_filename

    outcome, downloaded = await _download_for_folder(adapter, access_token, walked, budget)
    if downloaded is None:
        return outcome

    assistant_id = policy.assistant_id
    listed_etag = str(walked.entry.etag or walked.entry.modified_at or "") or None
    document_id = _generate_document_id()
    s3_key = _get_s3_key(assistant_id, document_id, _sanitize_filename(downloaded.filename))
    content_hash = _sha256(downloaded.content)
    synced_at = _now_timestamp()
    try:
        await create_document(
            assistant_id=assistant_id,
            filename=downloaded.filename,
            content_type=downloaded.content_type,
            size_bytes=len(downloaded.content),
            s3_key=s3_key,
            document_id=document_id,
            provenance=DocumentProvenance(
                source_connector_id=connector_id,
                source_adapter_key=adapter.metadata.key,
                source_file_id=walked.entry.id,
                imported_by_user_id=policy.created_by_user_id,
                source_etag=listed_etag,
            ),
        )
        records.update_document_sync_fields(
            assistant_id,
            document_id,
            content_hash=content_hash,
            last_synced_at=synced_at,
            sync_policy_id=policy.policy_id,
        )
    except Exception as e:  # noqa: BLE001 - one file's failure is not the run's
        logger.error(f"Sync policy {policy.policy_id}: could not record a new folder file: {type(e).__name__}")
        return "failed"

    try:
        _stage_to_s3(s3_key, downloaded.content, downloaded.content_type)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Sync policy {policy.policy_id}: could not stage document {document_id}: {type(e).__name__}")
        # Without its gates the next run sees it as changed and stages it again.
        written = {"contentHash": content_hash, "lastSyncedAt": synced_at}
        if listed_etag:
            written["sourceEtag"] = listed_etag
        records.rollback_document_sync_fields(assistant_id, document_id, written=written, previous={})
        await update_document_status(
            assistant_id=assistant_id,
            document_id=document_id,
            status="failed",
            error_message="Could not stage this file for ingestion. The next sync will try again.",
            error_details=type(e).__name__,
        )
        return "failed"
    return "added"


def _folder_documents(
    assistant_id: str, connector_id: str, policy_id: str
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """The assistant's live documents, and those from this connector indexed
    by provider file id (this policy's own document first when a file was
    imported twice)."""
    documents = [d for d in records.list_document_items(assistant_id) if d.get("status") != "deleting"]
    by_file: Dict[str, Dict[str, Any]] = {}
    for item in documents:
        file_id = item.get("sourceFileId")
        if item.get("sourceConnectorId") != connector_id or not file_id:
            continue
        current = by_file.get(file_id)
        if current is None or (
            item.get("syncPolicyId") == policy_id and current.get("syncPolicyId") != policy_id
        ):
            by_file[file_id] = item
    return documents, by_file


async def _sync_drive_folder(policy: SyncPolicy) -> Dict[str, Any]:
    assistant_id = policy.assistant_id
    if not folder_sync_enabled():
        # Turned off while this run was already dispatched.
        return await _finish(policy, "skipped")

    parsed = parse_folder_source_ref(policy.source_ref)
    if parsed is None:
        await set_policy_state(assistant_id, policy.policy_id, "paused_error", state_reason="folder reference is not valid")
        return await _finish(policy, "skipped")
    connector_id, folder_id = parsed

    provider = await OAuthProviderRepository().get_provider(connector_id)
    if provider is None:
        await set_policy_state(
            assistant_id, policy.policy_id, "paused_error", state_reason="connector no longer configured"
        )
        return await _finish(policy, "skipped")
    adapter_key = provider.file_source_adapter_id or ""
    adapter = registry.get(adapter_key)
    if adapter is None:
        await set_policy_state(
            assistant_id, policy.policy_id, "paused_error", state_reason=f"adapter '{adapter_key}' not available"
        )
        return await _finish(policy, "skipped")

    try:
        access_token = await _resolve_access_token(provider, policy.created_by_user_id)
    except WorkloadTokenUnavailableError as e:
        logger.error(f"Sync policy {policy.policy_id}: workload token unavailable: {e}")
        return await _finish(policy, "failed")
    if access_token is None:
        return await _pause_reauth(policy, "vault returned authorization URL", provider_id=connector_id)

    # The folder itself: a listing cannot tell a lost folder from an empty one.
    try:
        folder = await adapter.get_file_metadata(access_token, folder_id)
    except FileSourceAuthError as e:
        return await _pause_reauth(policy, str(e), provider_id=connector_id)
    except FileSourceNotFoundError:
        logger.warning(f"Sync policy {policy.policy_id}: folder not found/accessible")
        return await _finish(policy, "failed", not_found=True)
    except FileSourceError as e:
        logger.error(f"Sync policy {policy.policy_id}: drive error on the folder: {type(e).__name__}")
        return await _finish(policy, "failed")
    if folder.get("trashed"):
        logger.info(f"Sync policy {policy.policy_id}: folder is trashed; grace-skipping")
        return await _finish(policy, "skipped")

    max_files, max_pages = _folder_walk_caps()
    walk = await walk_folder(adapter, access_token, folder_id, max_files=max_files, max_pages=max_pages)
    if walk.error == WALK_AUTH:
        return await _pause_reauth(policy, "listing refused the token", provider_id=connector_id)
    if walk.error == WALK_NOT_FOUND:
        return await _finish(policy, "failed", not_found=True)

    documents, by_file = _folder_documents(assistant_id, connector_id, policy.policy_id)
    excluded = set(policy.excluded_file_ids or [])
    summary = FolderRunSummary(listed=walk.listed, truncated=walk.truncated)
    listed_ids = set()
    existing: List[Tuple[WalkedFile, Dict[str, Any]]] = []
    new: List[WalkedFile] = []
    for walked in walk.files:
        listed_ids.add(walked.entry.id)
        if walked.entry.id in excluded:
            continue
        document = by_file.get(walked.entry.id)
        if document is None:
            new.append(walked)
        elif document.get("syncPolicyId") in (None, policy.policy_id):
            existing.append((walked, document))
        # else: another policy keeps that document fresh.

    budget = _FolderRunBudget.from_env()
    try:
        for walked, document in existing:
            bucket = await _sync_folder_document(policy, adapter, access_token, walked, document, budget)
            setattr(summary, bucket, getattr(summary, bucket) + 1)
        for walked in new:
            bucket = await _import_folder_file(policy, adapter, access_token, connector_id, walked, budget)
            setattr(summary, bucket, getattr(summary, bucket) + 1)
    except _FolderAuthLost as e:
        return await _pause_reauth(policy, str(e), provider_id=connector_id)

    # Gone from the folder: only a complete listing proves an absence.
    if walk.complete:
        now = _now_timestamp()
        for item in documents:
            if item.get("syncPolicyId") != policy.policy_id or item.get("removedFromSource"):
                continue
            if item.get("sourceFileId") in listed_ids:
                continue
            records.set_document_removed_from_source(assistant_id, item["documentId"], True, at=now)
            summary.removed_from_source += 1

    logger.info(
        f"Sync policy {policy.policy_id}: folder run — listed {summary.listed}, {summary.unchanged} unchanged, "
        f"{summary.updated} updated, {summary.added} added, {summary.removed_from_source} gone from the folder, "
        f"{summary.failed} failed, {summary.skipped} over the size cap, {summary.deferred} deferred"
        + (", listing truncated" if summary.truncated else "")
    )
    if walk.error is not None:
        # A listing that failed part way: what it did list was still synced,
        # but the run counts toward backoff and the breaker.
        return await _finish(policy, "failed", summary=summary)
    staged = summary.updated + summary.added + summary.removed_from_source
    return await _finish(policy, "changed" if staged else "unchanged", summary=summary)


async def run_sync(payload: Dict[str, Any]) -> Dict[str, Any]:
    policy_id = payload["policyId"]
    assistant_id = payload["assistantId"]

    policy = await get_sync_policy(assistant_id, policy_id)
    if policy is None:
        logger.info(f"KB sync worker: policy {policy_id} no longer exists; dropping run")
        return {"policyId": policy_id, "result": "dropped"}

    sync_fn = {
        "drive_file": _sync_drive_file,
        "drive_folder": _sync_drive_folder,
    }.get(policy.source_type, _sync_web_crawl)
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
