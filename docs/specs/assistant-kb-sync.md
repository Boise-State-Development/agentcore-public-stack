# Assistant Knowledge Base Sync (Scheduled Re-Index)

Status: DRAFT — design spec, not yet built
Audience: backend + infra + frontend
Related: `docs/specs/` siblings; provenance fields added in the file-source import work

## 1. Problem

Assistant knowledge bases are indexed once at import time. Web content and Google
Drive files drift out of date, and today the only remedy is manual re-import.
Users creating or editing an Assistant should be able to mark a content source
as **synced**: refetched and reindexed on an interval they choose.

The dominant design constraint is **preventing runaway background work**:

- No sync may outlive its assistant, its document, or its source.
- No sync should keep burning embeddings for an assistant nobody uses.
- No sync should retry a permanently-broken source (deleted Drive file, dead
  site, revoked OAuth grant) forever.
- No unchanged content should ever be re-chunked or re-embedded.

Every mechanism below is chosen so that the *default failure mode is silence*,
not repetition.

## 2. Current state (what we build on)

| Piece | Where | Relevance |
|---|---|---|
| Document provenance | `backend/src/apis/app_api/documents/models.py:13-27` — `source_connector_id`, `source_adapter_key`, `source_file_id`, `source_etag`, `imported_by_user_id` | Captured at import explicitly to enable re-index; sync consumes these |
| Ingestion pipeline | S3 `ObjectCreated` on `assistants/` prefix → rag-ingestion Lambda (`apis/app_api/documents/ingestion/handler.py`) → Docling chunk → Titan embed → S3 Vectors keys `{doc_id}#{i}` | Re-staging a fetched file to the **same S3 key** re-runs the whole pipeline unchanged |
| Web crawls | `CrawlJob` at `PK=AST#{id}, SK=CRAWL#{crawl_id}`, bounded settings (depth ≤3, pages ≤100, concurrency ≤5) | Re-crawl = re-run with stored settings; bounds already enforced |
| Drive adapter | `apis/app_api/file_sources/adapters/google_drive.py`, tokens via AgentCore Identity vault | `download(file_id)` is the refetch primitive |
| Deletion lifecycle | Soft-delete + TTL + fire-and-forget cleanup (`documents/services/cleanup_service.py`) | Sync must join this cascade |
| Scheduling infra | **None.** No EventBridge, SQS, Step Functions, or cron Lambdas anywhere in `infrastructure/lib/constructs/` | Trigger mechanism is green-field |

## 3. Design overview

Three pieces, one trigger:

```
EventBridge rate(15 minutes)  ──►  Sync Dispatcher (Lambda, small)
                                        │  query DueSyncIndex (next_sync_at <= now)
                                        │  per policy: verify liveness, apply guards
                                        ▼
                                   Sync Worker (async-invoked Lambda, rag-ingestion-class image)
                                        │  Drive: metadata check → conditional download → stage to same S3 key
                                        │  Web:   conditional re-crawl → diff page set
                                        ▼
                                   Existing S3-event ingestion pipeline (unchanged)
```

A new **SyncPolicy** record is the single source of truth for "this source
resyncs." There are no per-source schedules, no timers, no self-perpetuating
jobs. The *only* thing that ever initiates sync work is the dispatcher reading
`DueSyncIndex` — delete the record and the sync ceases to exist. This
one-trigger architecture is the primary runaway defense: there is nothing to
orphan except a DynamoDB item, and that item is inert data.

### Why a sweeper, not per-source EventBridge Scheduler entries

Per-source schedules (EventBridge Scheduler one-per-policy) look natural but
are exactly the runaway shape we fear: cloud-side timer objects whose lifecycle
must be kept in perfect distributed agreement with app-side records, with
orphans firing forever when a delete path misses one. A single `rate(15 min)`
rule + a due-time GSI keeps all state in the table we already own, inside the
assistant's partition, covered by the existing delete cascade. Interval
granularity of ~15 minutes is far finer than any interval we offer (§5).

## 4. Data model

### SyncPolicy record (new item type, existing assistants table)

```
PK = AST#{assistant_id}
SK = SYNCPOL#{policy_id}                     policy_id: syn-{12-hex}
```

```python
class SyncPolicy(BaseModel):
    policy_id: str
    assistant_id: str
    source_type: Literal["web_crawl", "drive_file", "drive_folder"]
    # web_crawl: crawl_id of the CrawlJob whose settings we re-run
    # drive_file: document_id of the imported doc (provenance lives there)
    # drive_folder: "{connector_id}:{folder_id}" (§11)
    source_ref: str
    interval: Literal["daily", "weekly", "monthly"]     # bounded enum, no cron
    state: Literal["active", "paused_error", "paused_inactive",
                   "paused_reauth", "paused_user"]
    next_sync_at: str            # ISO 8601; drives DueSyncIndex
    last_sync_at: Optional[str]
    last_result: Optional[Literal["changed", "unchanged", "failed", "skipped"]]
    consecutive_failures: int = 0
    consecutive_not_found: int = 0     # source-gone counter (distinct from transient failure)
    created_by_user_id: str            # whose credentials background fetches use
    created_at: str
    updated_at: str
```

### DueSyncIndex (new GSI on assistants table, GSI4)

```
GSI4_PK = "SYNCDUE"          (constant; single logical partition — fine at our scale,
                              shard to SYNCDUE#{0..N} later if ever needed)
GSI4_SK = {next_sync_at}#{policy_id}
```

GSI4 attributes are **only present while `state == "active"`** — pausing a
policy removes it from the index (sparse GSI), so the dispatcher physically
cannot see paused work. Paused ≠ filtered-at-query-time; paused = invisible.

### Document additions

```python
content_hash: Optional[str]      # sha256 of last-ingested raw bytes
last_synced_at: Optional[str]
sync_policy_id: Optional[str]    # back-pointer for UI badges
```

### Assistant additions

```python
last_used_at: Optional[str]      # bumped (throttled, ≤1 write/day) on chat use
```

## 5. Scheduling

- **Trigger**: one EventBridge rule, `rate(15 minutes)`, on a new small
  dispatcher Lambda (`backend/src/lambdas/kb-sync-dispatcher/`). CDK construct
  under `infrastructure/lib/constructs/rag-ingestion/` alongside the existing
  ingestion construct.
- **Intervals offered**: Daily / Weekly / Monthly (plus "Manual only" = no
  policy). Enum, not cron — the floor (24 h) is a hard server-side bound, and
  a bounded enum can't express "every minute."
- **Dispatcher tick**:
  1. Query `DueSyncIndex` for `GSI4_SK <= now`, limit **20 per tick** (global
     throughput cap; backlog just waits for the next tick — degradation is
     lateness, never amplification).
  2. For each due policy, run the liveness + guard checks (§7). Guards that
     fail transition the policy state (which removes it from the GSI) — they
     do not reschedule-and-retry.
  3. **Re-arm before work**: set `next_sync_at = now + interval` *first*, then
     async-invoke the worker. A crashed worker means one missed sync, not a
     hot loop; a double-fired dispatcher tick is idempotent because the
     re-arm is a conditional update on the old `next_sync_at`.
- **Fan-out**: direct async Lambda invoke of the worker, one per policy, ≤20
  per tick. No SQS for v1 (nothing else in the stack uses it); if scale ever
  demands it, the dispatcher→worker seam is where a queue slots in.

## 6. Execution (worker)

Worker is a Lambda sharing the rag-ingestion image class (needs the
file-source adapters and crawler; lives with them in
`apis/app_api` packaging like the existing ingestion handler — same import-
boundary posture as today's ingestion Lambda).

### 6.1 Drive file (`drive_file`)

1. Load document; resolve provider token for `created_by_user_id` from the
   AgentCore Identity vault. Verified call chain (no live user session
   needed): `GetWorkloadAccessTokenForUserId(workloadName, userId)` — pure
   IAM/SigV4 authorization, no user JWT — then
   `GetResourceOauth2Token(USER_FEDERATION)`; when a valid refresh token is
   vaulted, AgentCore documented-behavior skips federation and returns an
   access token non-interactively. Reuse
   `apis.shared.oauth.agentcore_identity.AgentCoreIdentityClient` verbatim
   (its mint fallback is exactly this path), and
   `custom_parameters_for(provider_type, ..., force_authentication=True)` —
   customParameters are part of the vault key; a mismatched map falsely
   reports consent-required.
2. **Metadata-first change detection**: Drive `files.get` with
   `fields=id,mimeType,trashed,modifiedTime,version,md5Checksum,size`.
   - **Binary files**: compare `md5Checksum` (exact content identity).
   - **Native Docs/Sheets** (exported to .docx/.xlsx): `md5Checksum`,
     `sha256Checksum`, and `headRevisionId` are **not populated** for
     editor files — use `modifiedTime` as the primary signal (`version` is
     a strict superset but bumps on comments/metadata → false-positive
     downloads).
   - `trashed == true` is a **200, not an error**: treat as
     `last_result=skipped` (grace state — recoverable from trash; do not
     delete our copy, do not count as a failure).
   - If unchanged vs `source_etag` → record `last_result=unchanged`, done.
     No download, no embed.
3. If changed: `adapter.download(source_file_id)` → compare sha256 to
   `content_hash` (export formats can differ byte-wise even when content
   didn't; hash is the second gate). If equal → `unchanged`, done.
4. Stage bytes to the **existing S3 key** for the document → S3 event →
   existing ingestion pipeline re-chunks and re-embeds, overwriting vectors
   `{doc_id}#{0..n}`.
5. **Shrinkage cleanup**: before staging, snapshot old `chunk_count`; after
   ingestion completes with new count `m < n`, delete vectors
   `{doc_id}#{m..n-1}`. (Without this, in-place overwrite strands stale
   tail chunks — the one real gap in "reuse the pipeline as-is." Implemented
   as: worker stashes `previous_chunk_count` on the document record; the
   ingestion handler's completion step deletes the tail if present.)
   **Managed knowledge bases** take the same S3 event to the managed
   ingestion consumer instead, where the overwrite's event is
   indistinguishable from a redelivery of the original upload's: the
   document is already `complete` with its bytes settled. So the worker
   also writes `stagedContentHash` before staging, and the consumer
   re-ingests while it differs from the `ingestedContentHash` the last
   re-ingest recorded — reserving the size growth against the byte cap
   *before* submitting (a file that grew past the cap is refused and its
   previous version keeps serving; the next sync retries), then settling
   the difference. The same marker covers a changed page in §6.2. See
   `kb_migration/ingestion_consumer.py` ("A changed source is not a
   redelivery").
6. Update `source_etag`, `content_hash`, `last_synced_at`, `last_result`.

### 6.2 Web crawl (`web_crawl`)

1. Load the referenced `CrawlJob`; re-run the crawler with its **stored
   settings** (bounds already capped: ≤3 depth, ≤100 pages, ≤5 concurrency).
2. Per fetched page, conditional GET (`If-None-Match`/`If-Modified-Since`)
   where the server supports it; otherwise fetch + content-hash compare.
   Only changed pages are re-staged/re-embedded.
3. Diff the page set against existing docs for that crawl root:
   - **New URLs** → new documents (respecting the max_pages cap — the cap
     applies to the crawl total, so a synced crawl can never grow past it).
   - **Missing URLs** → increment a per-doc `consecutive_misses`; delete the
     document (existing soft-delete + cleanup cascade) only after missing in
     **2 consecutive** sync runs. A transient outage must not vaporize a
     knowledge base.
4. Record results on the policy.

### 6.3 Concurrency & re-entrancy

A policy is skipped if its previous run hasn't finished: the worker sets
`sync_run_started_at` on the policy at start and clears it at end; the
dispatcher skips policies with a fresh (<2 h) run-start stamp. Older stamps
are treated as crashed runs and cleared. Combined with the per-tick cap of 20
and per-crawl concurrency of ≤5, worst-case parallel fetch pressure is bounded
and known.

## 7. Runaway-process safeguards (the point of this spec)

Layered, so any single failure of discipline is caught by another layer:

1. **Single trigger, inert state.** Only the dispatcher initiates work, only
   by reading the GSI. There are no timers, queues, or schedules to orphan.
   Deleting the policy record is total and instantaneous revocation.

2. **Lifecycle tie via liveness check.** Every dispatch re-verifies, in order:
   assistant exists → source document / crawl job exists (and isn't
   `deleting`) → policy state is `active`. Any miss ⇒ the policy is
   **hard-deleted on the spot** (self-healing against any delete path that
   forgot the cascade). Additionally, the assistant delete cascade and the
   document delete path both delete associated `SYNCPOL#` records eagerly —
   the liveness check is the backstop, not the mechanism.

3. **Inactivity auto-pause.** If `assistant.last_used_at` is older than
   **30 days** (config: `KB_SYNC_INACTIVITY_PAUSE_DAYS`), the dispatcher
   transitions the policy to `paused_inactive` instead of running it. This is
   the direct answer to "reindexing content that isn't being used."
   **Auto-resume**: the app-api path that bumps `last_used_at` also flips any
   `paused_inactive` policies back to `active` with `next_sync_at = now` —
   the first person to use a dormant assistant gets a fresh index within a
   tick, and nobody has to know a pause happened.

4. **Failure circuit breaker.** Failures back off exponentially
   (`interval × 2^consecutive_failures`, capped at 30 days). At
   **5 consecutive failures** the policy transitions to `paused_error`
   (out of the GSI, no further attempts) and the UI shows a badge with the
   last error; resume is an explicit user action.

5. **Source-gone fast path.** Drive returns **404 `notFound`** both when a
   file is permanently deleted and when the user merely lost access —
   deliberately indistinguishable (anti-enumeration). So: 404 `notFound`
   increments `consecutive_not_found`; at **2**, the policy goes to
   `paused_error` with reason "source no longer accessible" (worded as
   *accessible*, not *deleted* — we cannot know which). We never delete the
   already-indexed content on 404; the user decides. `trashed=true` (a 200)
   is a grace state, not a strike. Discriminate Drive errors on
   `error.errors[0].reason`, **never on bare HTTP status**: 403/429 with
   `usageLimits` domain (`rateLimitExceeded`, `userRateLimitExceeded`) are
   transient → backoff-retry, not strikes; 403 `domainPolicy` (Workspace
   admin disabled Drive apps) pauses connector-wide, not per-policy.

6. **Auth-gone fast path.** Two distinct signals, both ⇒ immediate
   `paused_reauth`:
   - **Vault says re-consent**: `GetResourceOauth2Token` returns **HTTP 200
     with an `authorizationUrl` instead of an `accessToken`** (not an
     exception) — surfaces as `TokenResult.requires_consent` in our wrapper.
     Covers refresh-token expiry/revocation AgentCore can detect. The worker
     must reuse the existing `_ShortCircuitPoller` pattern or the SDK will
     sit in its consent poll loop.
   - **Provider-side revocation AgentCore can't see**: the vault can return
     a token Google then rejects with 401 at the Drive API (documented
     AgentCore limitation) — our adapter already maps this to
     `FileSourceAuthError`.
   Never retried on a timer — only a fresh consent resumes it: the
   `complete_consent()` success path in app-api
   (`apis/app_api/connectors/routes.py:379`, right after
   `complete_resource_token_auth`) flips `paused_reauth` → `active` for that
   `(user, provider)`'s policies. Infra-class failures
   (`WorkloadTokenUnavailableError`, `AccessDeniedException`,
   `ThrottlingException`) are operator errors: alert via metrics, retry with
   backoff, do **not** pause the user's policy.

7. **Change detection before cost.** Metadata check → conditional GET →
   content hash, in that order. The expensive tail (Docling + Titan embed)
   runs only when bytes actually changed. An unchanged corpus on a daily
   sync costs one metadata call per source per day.

8. **Bounded configuration surface.** Interval is an enum with a 24 h floor;
   crawl settings are the already-capped stored settings; **≤ 10 sync
   policies per assistant** (config: `KB_SYNC_MAX_POLICIES_PER_ASSISTANT`);
   dispatcher processes ≤ 20 policies/tick globally. Every knob has a
   ceiling; no user input can express unbounded work.

9. **Observability.** Worker emits CloudWatch metrics:
   `KBSync/RunsStarted`, `RunsChanged`, `RunsUnchanged`, `RunsFailed`,
   `PoliciesPaused` (by reason), `EmbeddingChunksWritten`. One alarm:
   `RunsFailed` sustained high, and an anomaly alarm on
   `EmbeddingChunksWritten` (the "we are somehow re-embedding the world"
   tripwire). Each policy keeps `last_result` + timestamps so the UI never
   needs CloudWatch.

10. **Global kill switch.** `KB_SYNC_ENABLED` env var on the dispatcher
    (default true). Flipping it off stops all sync activity in ≤ 15 minutes
    with zero data changes — policies simply go stale until re-enabled.

## 8. API & UX

### API (app-api, all `Depends(get_current_user_from_session)`, owner-gated)

```
POST   /assistants/{id}/sync-policies                {source_type, source_ref, interval}
PATCH  /assistants/{id}/sync-policies/{policy_id}    {interval? , state?}   # pause/resume/change interval
DELETE /assistants/{id}/sync-policies/{policy_id}
GET    /assistants/{id}/sync-policies
POST   /assistants/{id}/sync-policies/{policy_id}/run-now    # manual trigger; rate-limited 1/10min/policy
```

`run-now` sets `next_sync_at = now` (state must be `active` or resumable) and
lets the normal dispatcher path pick it up — even manual sync flows through
the single trigger, preserving every guard.

### Assistant form UX

Per content-source row (Drive file, web crawl root):

- **"Keep in sync" control**: `Manual only ▾ | Daily | Weekly | Monthly`
  (Manual only = default; selecting an interval creates the policy).
- **Status line** under the row: `Synced 2h ago · next Tue` /
  `Paused — source not found` / `Paused — reconnect Google Drive` /
  `Paused — assistant inactive (resumes on next use)`.
- **Sync now** button (calls `run-now`), disabled while a run is in flight.
- Paused-error rows get the existing error-badge treatment from document
  status rows.

Device-uploaded files show no sync control (no source of truth to refetch).

## 9. Resolved questions (research pass, 2026-07-03)

All five former open questions are resolved; answers below are grounded in
AWS AgentCore Identity docs, Google Drive API docs, and code inspection.

1. **Offline token retrieval — RESOLVED: feasible with existing machinery.**
   `bedrock-agentcore:GetWorkloadAccessTokenForUserId` takes only
   `{workloadName, userId}` and is authorized purely by IAM/SigV4 — no user
   JWT. With that workload token, `GetResourceOauth2Token(USER_FEDERATION)`
   returns the stored token non-interactively when a refresh token is
   vaulted ("If a valid refresh token is stored, AgentCore skips the user
   federation flow and directly returns a new access token" — identity
   devguide). Our `apis.shared.oauth.agentcore_identity` mint fallback IS
   this path and is reusable from the sync Lambda as-is.
   **Worker Lambda requirements**:
   - IAM: `bedrock-agentcore:GetWorkloadAccessTokenForUserId` +
     `bedrock-agentcore:GetResourceOauth2Token` (mirror the app-api
     `AgentCoreWorkloadIdentityAccess` statement in
     `infrastructure/lib/constructs/app-api/app-api-iam-grants.ts`, minus
     provider-CRUD and `CompleteResourceTokenAuth`). Optionally constrain
     with the `bedrock-agentcore:userid` condition key.
   - Env: `AGENTCORE_RUNTIME_WORKLOAD_NAME` = the shared platform workload
     identity (same SSM value app-api/inference-api use — a different
     workload sees an empty vault) and `AGENTCORE_LOCAL_OAUTH_CALLBACK_URL`
     (our wrapper requires it even for pure reads).
   - Must pass the exact consent-time `customParameters`
     (`custom_parameters_for(..., force_authentication=True)`) and the exact
     Cognito `user_id` — both are vault-key components.
   - **Operational caveat**: Google OAuth clients in *Testing* publishing
     status issue 7-day refresh tokens — a background sync would hit
     `paused_reauth` weekly until the client is Published/verified. Also:
     refresh tokens die after 6 months of non-use and can be silently
     evicted past ~50 live tokens per user per client.

2. **Drive loss-of-access semantics — RESOLVED (design §7.5 updated).**
   404 `notFound` deliberately conflates "deleted" with "unshared from this
   user" — no API call distinguishes them. Owner-trashed files are a 200
   with `trashed=true` (content still readable) → grace state. Change
   detection must be per-file-type: `md5Checksum` for binaries;
   `modifiedTime` for native editor files (checksums/headRevisionId are not
   populated for them). Error discrimination is by `reason` string, never
   bare HTTP status (rate-limit 403s look like permission 403s otherwise).

3. **Shrinkage-cleanup stash — RESOLVED: safe.** After the initial
   `put_item` at creation, the ingestion pipeline only ever issues targeted
   `UpdateExpression` writes on known fields (status, updatedAt, chunkCount,
   vectorStoreId, error fields) — `ingestion/status.py:21-108`. A
   `previous_chunk_count` attribute stashed by the worker survives
   ingestion untouched. Final `chunk_count` is written at `mark_embedding`
   (`handler.py:288`); `mark_complete` doesn't touch it — so the worker must
   snapshot the old count *before* staging the new S3 object. Also
   confirmed: overwriting a complete document's S3 key simply re-triggers
   ingestion with no reprocessing guard in the way — the reuse-the-pipeline
   plan works with zero pipeline changes beyond the tail-delete hook.

4. **Whose policies pause on reauth — DECIDED: keyed to
   `created_by_user_id`, acceptable for v1 with an escape hatch.** Any
   editor can delete a paused policy and recreate it (or re-import the
   file), which re-keys it to their own credentials. No credential
   "takeover" PATCH in v1. The resume hook attaches at
   `complete_consent()` success in `apis/app_api/connectors/routes.py:379`.

5. **Shared assistants — DECIDED, with one spec correction.** SHARE records
   already carry `permission: viewer|editor`, and editors can already
   upload/import documents (`documents/routes.py:44-60`
   `_require_edit_permission`) — sync-policy CRUD uses the same helper, so
   **editors manage sync policies**, consistent with the document surface.
   Sync always runs on the policy creator's credentials (same posture as
   import's `imported_by_user_id` today). **Correction**: `usage_count` is
   never incremented anywhere and no `last_used_at` exists — the inactivity
   pause needs a net-new bump: app-api, on the chat path where a session
   resolves its assistant, throttled to ≤1 write/day per assistant, bumping
   `last_used_at` and flipping any `paused_inactive` policies back to
   `active` (§7.3). Any user's use counts, not just the owner's.

## 10. Suggested PR breakdown

1. **PR-1 data**: SyncPolicy model + repository, GSI4, document/assistant
   field additions, delete-cascade integration, unit tests.
2. **PR-2 infra**: EventBridge rule + dispatcher Lambda construct + worker
   Lambda construct, kill switch, metrics/alarms. (platform.yml deploy)
3. **PR-3 worker**: Drive-file sync path incl. change detection + shrinkage
   cleanup + all pause transitions. (backend.yml deploy)
4. **PR-4 worker**: web re-crawl path incl. page-set diff + 2-miss deletion.
5. **PR-5 API**: sync-policy CRUD (owner + editor via
   `_require_edit_permission`) + run-now + reauth resume hook in
   `complete_consent()` + **net-new** `last_used_at` bump (throttled) on the
   app-api chat path with the `paused_inactive` auto-resume.
6. **PR-6 frontend**: assistant-form sync controls + status lines + badges.

PR-1..2 are inert (nothing schedules until a policy exists and the flag is
on); each subsequent PR is independently shippable behind `KB_SYNC_ENABLED`.

## 11. Drive folders (`drive_folder`)

Added for Save as project (`composer-project-folder-picker.md` §5.3): a project
saved from a Drive folder can keep its Files in step with that folder. A
`drive_file` policy cannot do it: it re-fetches one document, so a file added
to the folder later is never covered, and one policy per file runs into the
10-per-assistant cap (§7.8) on any real folder. A `drive_folder` policy is
**one** policy per folder, whatever its file count, on the same dispatcher and
worker. Nothing about the trigger changes: the dispatcher still reads
`DueSyncIndex`, the policy is still inert data, and every guard in §7 applies.

**Flag:** `FOLDER_SYNC_ENABLED` / `CDK_FOLDER_SYNC_ENABLED` (app-api and both
kb-sync Lambdas), `features.folderSync` in the SPA. In development, default
off. While off, app-api refuses to create a `drive_folder` policy (400) and the
dispatcher re-arms a due `drive_folder` policy one interval out without
invoking the worker, so it neither runs nor sits at the head of the due index
starving other policies (§11.7).

**No new AWS resources.** The worker already holds every grant a folder run
uses: the vault token read, the assistants table, the documents bucket.

### 11.1 The policy

```
source_type = "drive_folder"
source_ref  = "{connector_id}:{folder_id}"     # split on the LAST ':'
source_name = "Course readings/Week 3"         # display only: the folder's path
excluded_file_ids = {…}                        # string set, see §11.4
last_run_summary  = {listed, unchanged, updated, added, removedFromSource,
                     failed, skipped, deferred, truncated}
```

- `connector_id` is the OAuth provider id the folder was attached through (the
  folder row's `connectorId`); the worker resolves its adapter from the
  provider's `file_source_adapter_id`, as an import does. `folder_id` is the
  provider's folder id. Drive folder ids never contain `:`, so the last `:` is
  the separator whatever a connector id holds.
- One policy per `(connector, folder)` per assistant: the existing
  duplicate-`source_ref` check.
- It counts **one** against `KB_SYNC_MAX_POLICIES_PER_ASSISTANT`.
- `source_name` is the user's own folder path, shown on the project's Files
  page. Like every folder name in `drive-folder-attachments.md`, it never
  reaches a log line or a metric.
- Creating one needs an editor (the existing gate), the flag, and a file
  source the caller can use (`resolve_file_source`, the import route's check),
  because the worker will fetch with the creator's credentials.

**Which documents belong to it.** A document belongs to a folder policy when
its `syncPolicyId` names that policy. The worker stamps it: on a document it
creates, and on an existing document it **adopts**, one whose provenance
(`sourceConnectorId`, `sourceFileId`) matches a file listed in the folder and
which no other policy covers. That is how the copies Save as project imported
join the policy on its first run with no extra call from the SPA. A document
already covered by another policy (a `drive_file` schedule on it) is left to
that policy, and conversely a `drive_file` policy is refused on a document a
folder policy has adopted (409). One source of truth per document.

**Liveness.** A folder has no record in the assistants table to look up, so the
dispatcher's source-liveness check (§7.2) is the assistant alone for this
type. Whether the folder still exists is the worker's question (§11.3, step 2),
answered through the same not-found streak as a deleted `drive_file`.

### 11.2 How the worker creates documents

The import route (`documents/routes.py` → `import_service.run_import`) is
request-context code, but the pieces it uses are not, and the kb-sync image
already carries them for the web re-crawl path, which creates page documents
today:

1. **Download first**, through the adapter, with a per-file byte cap. Nothing
   is written for a file that cannot be fetched, so a failing file does not
   leave a `failed` document behind on every run.
2. `document_service.create_document` with the real filename, type, size and
   final S3 key (`storage_service._get_s3_key`), status `uploading`, and
   provenance (`source_connector_id`, `source_adapter_key`, `source_file_id`,
   `source_etag` = the listed Drive `version`, `imported_by_user_id` = the
   policy's creator, who is therefore also `addedByUserId`).
3. `records.update_document_sync_fields`: `contentHash`, `syncPolicyId`,
   `lastSyncedAt`.
4. PUT the bytes to that key. The bucket's `ObjectCreated` event runs the
   ingestion that the knowledge base's engine routes to, exactly as an import
   does. A project's knowledge base is managed, and its consumer reads the new
   document as a first upload.
5. If the PUT fails, the document is marked `failed` and its `contentHash` and
   `sourceEtag` are removed, so the next run sees it as changed and stages it
   again rather than matching gates on bytes the knowledge base never got.

`create_document` imports nothing heavy at module level: `apis.shared.assistants`
(and with it the embeddings stack) is reached only from the ownership-checked
reads and the delete path, both function-local, and the folder path takes
neither. A changed existing document goes through the `drive_file` path's
staging, unchanged: pre-stage writes (`previousChunkCount`,
`stagedContentHash`), overwrite of the document's S3 key, rollback if the PUT
fails. The two paths share one helper.

The folder walk itself moves out of app-api's expansion task into
`file_sources/folder_walk.py`, which takes the adapter, token and caps as
arguments, so the expansion task and the worker list a folder the same way
(breadth-first, subfolders included, `FOLDER_ATTACH_MAX_FILES` attachable
files and `FOLDER_ATTACH_MAX_PAGES` pages) and accept the same files
(`attachable_mime_for`: the chat allowlist minus images, which is what Save as
project copied). The kb-sync image gains `apis/shared/files/` for those caps
and that rule: files, not dependencies (boto3 and pydantic, already pinned).

### 11.3 A run

1. Resolve the provider, its adapter, and the creator's vault token (§6.1 step
   1). Consent needed, or Google rejects the token: `paused_reauth`, with the
   reauth marker for the connector.
2. `get_file_metadata(folder_id)`. A 404 is a not-found strike (`failed`,
   `not_found=True`; paused at 2, §7.5). `trashed=true` is the grace state
   (`skipped`). This call is needed because a listing cannot tell a deleted
   folder from an empty one: `'{id}' in parents` on a folder the user lost
   simply returns nothing.
3. **Walk** the folder (§11.2). A listing error is a `failed` run (backoff and
   breaker, §7.4). A listing that stopped at a cap is still used, but marks
   nothing gone (step 6).
4. Load the assistant's documents once (`records.list_document_items`, one
   paginated key query) and index them by `(connector, file id)`.
5. For each listed file, in listing order, existing documents first, then new
   files, until a per-run cap (§11.5) is reached:
   - **excluded** (`excluded_file_ids`): skipped;
   - **existing, gate 1**: the listed Drive `version` against the document's
     `sourceEtag`. Equal: `unchanged`, no download. A listing already carries
     `version`, so an unchanged folder costs its listing pages and one
     metadata call per run, not one call per file;
   - **adoption without a download**: a document that has no `sourceEtag` yet
     (imports do not record one) and was created after the file's
     `modifiedTime` holds the current bytes, so it takes the listed version and
     counts unchanged. Without this the first run would re-ingest every file
     Save as project had just imported;
   - **existing, gate 2**: download, sha256 against `contentHash`. Equal:
     advance `sourceEtag`, `unchanged`. Different: stage (§11.2), `updated`;
   - **new**: create and stage (§11.2), `added`.
   One file's failure (a download error, a refused type) is counted in the
   summary and the run goes on; it is not a run failure.
6. **Gone from the folder.** A document of this policy whose file the listing
   did not return is marked `removedFromSource=true` (with
   `removedFromSourceAt`), **only when the walk was complete**: no listing
   error and no cap reached. It is never deleted: the knowledge base keeps it,
   the agent can still cite it, and the Files page says "No longer in the
   folder" beside it so an editor decides. A file that comes back is
   unmarked on the run that lists it again. Same rule as a refreshed folder
   attachment's `removed_from_source` (`drive-folder-attachments.md` §4.6), and
   for the same reason as §7.5: a 404 or an absence cannot say deleted versus
   unshared, and a moved or briefly trashed file must not vaporize a document.
7. Record the run: `changed` if anything was staged, created or newly marked
   gone, else `unchanged`; the summary goes on the policy as
   `lastRunSummary`, counts only.

### 11.4 A file someone removed from the project

Deleting a document that a folder policy covers adds its provider file id to
the policy's `excluded_file_ids` (a DynamoDB string-set `ADD`, conditioned on
the policy being a `drive_folder`), in the same delete route that already
removes a `drive_file` document's policy. Without it the next run would find
the file in the folder with no document and import it again. A removed file
stays removed, as on a folder attachment. The set is bounded by the folder
(at most the walk cap's worth of ids that matter; the add stops at 500).

Deleting the policy removes the `syncPolicyId` back-pointer from its
documents (one key query over the assistant's documents) and keeps the
documents: they become ordinary imported files.

### 11.5 Caps and the 15-minute Lambda

| Bound | Default | Where |
|---|---|---|
| Attachable files a walk lists | 100 (`FOLDER_ATTACH_MAX_FILES`) | shared with folder attachments |
| Listing pages a walk fetches | 200 (`FOLDER_ATTACH_MAX_PAGES`) | shared with folder attachments |
| Downloads per run (changed + new) | 50 (`KB_SYNC_FOLDER_MAX_FILES_PER_RUN`) | worker |
| Bytes downloaded per run | 250 MB (`KB_SYNC_FOLDER_MAX_BYTES_PER_RUN`) | worker |
| Bytes per file | 50 MB (`KB_SYNC_FOLDER_MAX_FILE_BYTES`); a larger file is skipped, refused before its bytes when Drive reports a size | worker |
| Wall clock | stop starting downloads after 12 min (`KB_SYNC_FOLDER_RUN_BUDGET_SECONDS`) | worker |

Files are processed one at a time, so at most one file's bytes are in memory
(the worker has 1,024 MB). The budget is checked before each download, so the
run ends inside the Lambda's 15 minutes with room for the bookkeeping writes;
a file still in flight at 12 minutes is bounded by the adapter's own request
timeout. Work a cap leaves undone is counted `deferred` and picked up by the
next run, which re-lists and finds those files still changed or still new:
nothing is queued, nothing is carried over. A 100-file folder in which every
file changed converges in two runs.

Worst case per dispatcher tick is 20 policies × 50 files = 1,000 documents
staged, the same order as 20 crawls of 50 pages (§6.2), and each run's
`lastRunSummary` and `KBSync` logs make it visible. An unchanged folder stages
nothing.

### 11.6 Safeguards, by §7 item

1. **Single trigger, inert state**: unchanged; the folder policy is a row.
2. **Liveness**: assistant gone ⇒ policy deleted; folder gone ⇒ not-found
   streak (§11.3 step 2), never a delete of the documents.
3. **Inactivity pause**: unchanged; a project's chat turns bump the harness's
   `lastUsedAt`.
4. **Circuit breaker**: listing failures and failed metadata calls are run
   failures; per-file failures are not, so one broken file cannot pause a
   folder, and a folder whose every file fails still records each in its
   summary.
5. **Source-gone**: §11.3 step 2, plus §11.3 step 6 per file.
6. **Auth-gone**: identical; the reauth marker names the policy's connector.
7. **Change detection before cost**: listing version, then sha256; an
   unchanged file is never downloaded once its document has a `sourceEtag`.
8. **Bounded configuration**: §11.5; still one interval enum, still ≤ 10
   policies per assistant.
9. **Observability**: the run summary on the policy; the worker logs counts,
   never names.
10. **Kill switch**: `KB_SYNC_ENABLED` stops everything; `FOLDER_SYNC_ENABLED`
    stops folder runs alone.

### 11.7 The dispatcher

Two changes. The source-liveness lookup is skipped for `drive_folder`
(§11.1). And while `FOLDER_SYNC_ENABLED` is off a due folder policy is
re-armed one interval out (the same conditional write, no run stamp, no
worker invoke) and counted `FolderSyncDisabled`. Leaving it due would starve
the sweep: the index is read most-overdue first, 20 at a time, so policies that
are never re-armed would fill every tick. The worker checks the flag too and
records `skipped`, for a run already in flight when it is turned off.
