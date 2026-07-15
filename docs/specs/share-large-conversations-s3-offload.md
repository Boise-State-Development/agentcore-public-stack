# Sharing large conversations — S3 snapshot offload

**Status:** Draft (spec-first; implementation gated on approval)
**Branch:** `feature/share-large-conversations-s3-offload` (off `develop`)
**Owner:** Phil Merrell
**Related:** Conversation share feature (`apis/app_api/shares/`); PR #657 (share IAM-grant fix — *separate* bug); Memory Spaces S3 store (`apis/shared/memory/store.py`); Artifacts / Skills / RAG S3-offload precedent.

---

## 1. Problem

Creating a share for a large conversation fails. `ShareService.create_share` inlines the full message list into a single DynamoDB item on the `shared-conversations` table:

```python
item = {
    "share_id": share_id,
    ...
    "metadata": metadata_snapshot,
    "messages": messages_snapshot,   # ← entire conversation, inline
}
self._table.put_item(Item=item)
```

DynamoDB caps an item at **400 KB**. A long conversation — especially one with tool results, images, documents, or reasoning blocks — blows past that. Observed in **prod-ai** (`897729136999`, `us-west-2`) app-api logs:

```
apis.app_api.shares.routes - ERROR - Error creating share for session 69ea19d9-...:
An error occurred (ValidationException) when calling the PutItem operation:
Item size has exceeded the maximum allowed size
```

The route's catch-all turns this into a bare `500 {"detail":"Failed to create share"}`. The user is told sharing failed with no reason and no recourse.

This is **distinct** from the `AccessDeniedException` IAM-grant bug fixed in PR #657. This one is `ValidationException` / item-size.

### Why this recurs

The snapshot is a *copy* of the conversation taken at share time, so it grows monotonically with conversation length and with the richness of each turn (a single image or document block can be tens/hundreds of KB after base64). There is no upper bound on conversation size, so any "just trim it" mitigation only moves the cliff.

---

## 2. Goal

Support sharing conversations of any size, transparently, while:

- Keeping the existing share read/write/update/revoke/export API contract unchanged (SPA needs no changes).
- Mirroring the **established S3-offload pattern** already used for Memory Spaces, Skills reference files, Artifacts, and RAG documents — not inventing a new one.
- Remaining backward-compatible with existing inline-item shares in prod/dev.
- Replacing the bare `500` with a specific, honest error if a share genuinely cannot be created.

---

## 3. Design overview

Offload the **snapshot body** (`messages`, and defensively `metadata`) to an S3 object. Keep in the DynamoDB item only:

- the share's control-plane fields (`share_id`, `session_id`, `owner_id`, `owner_email`, `access_level`, `allowed_emails`, `created_at`) — these are small, queried by GSIs, and mutated by `update_share`; they **must** stay in DynamoDB, and
- a **pointer** to the S3 object holding the body.

The DynamoDB item thus becomes small and bounded regardless of conversation size. The read path (`get_shared_conversation`, `export_shared_conversation`, `get_shares_for_session`) fetches the body from S3 when the pointer is present, and falls back to the inline `messages`/`metadata` fields when it is not (legacy items).

```
create_share
  ├─ snapshot messages + metadata (unchanged)
  ├─ serialize body → JSON bytes
  ├─ store.put(share_id, body) → s3_key            (NEW)
  └─ put_item { control fields..., body_ref: {bucket_key, format, ...} }   (no inline messages)

get_shared_conversation / export_shared_conversation
  ├─ get_item → control fields + (body_ref | inline messages)
  ├─ if body_ref: store.get(key) → messages/metadata     (NEW)
  └─ else: read inline item["messages"]/["metadata"]     (legacy fallback)
```

### Why S3, always (not a size threshold)

The task raised "inline under N KB vs always-S3." **Recommendation: always offload the body to S3.** Rationale:

- **One code path.** A size gate means two write paths and two read paths, each needing tests and each a place for the 400 KB cliff to hide (the gate has to account for DynamoDB's *item* overhead — attribute names, the `Decimal` re-encoding, the control fields — not just `len(json)`, so a naive threshold is itself a source of bugs).
- **The body is never queried.** Nothing in the share feature does a DynamoDB `Query`/`Scan` *on message content*; the GSIs key on `session_id` and `owner_id` only. So there is zero DynamoDB-side benefit to keeping messages inline.
- **The offloaded read is one `get_object`.** For a share view that already crosses the network and renders a whole conversation, a single S3 GET is negligible latency and removes a class of failure entirely.
- Precedent: Memory Spaces, Skills, and Artifacts all offload bytes to S3 unconditionally and keep only manifests/pointers in DynamoDB. This proposal is deliberately the *same shape*, for reviewer familiarity and code reuse.

The one concession to "small items": we still **read** legacy inline items (they predate this change), but we never **write** new ones.

---

## 4. Storage design

### 4.1 New bucket: `shared-conversations`

A dedicated private S3 bucket, created by extending `SharedConversationsConstruct` (co-locating bucket + table in the one construct that already owns this domain — same as `MemorySpacesConstruct` owning both its bucket and table, and `ArtifactsDataConstruct` owning both).

```ts
// infrastructure/lib/constructs/data/shared-conversations-construct.ts
public readonly bucket: s3.Bucket;

this.bucket = new s3.Bucket(this, 'SharedConversationsBucket', {
  bucketName: getResourceName(config, 'shared-conversations'),
  encryption: s3.BucketEncryption.S3_MANAGED,
  blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
  enforceSSL: true,
  lifecycleRules: [
    { id: 'abort-stale-multipart', abortIncompleteMultipartUploadAfter: cdk.Duration.days(7) },
  ],
  removalPolicy: getRemovalPolicy(config),
  autoDeleteObjects: getAutoDeleteObjects(config),
});
```

Private, server-side-only access (app-api reads/writes; never loaded cross-origin — the share view is JSON served by app-api, not an iframe). Byte-for-byte the Memory Spaces bucket recipe.

**No expiration lifecycle rule.** A share's object lives exactly as long as the share row: it is deleted when the share is revoked or the session's shares are cleaned up (§5.4). Age-based reaping would break live shares — the same "reference recency ≠ object age" lesson from the MCP App UI-resource persistence work (`project_mcp_apps_uires_persistence`: never age-based deletes when a live row can still point at the object).

> **Decision point for review:** dedicated bucket vs. reusing an existing one. A dedicated bucket keeps IAM scoping clean (its own ARN, its own grant sid) and lifecycle independent, at the cost of one more bucket. This is the pattern every sibling feature follows, so the spec assumes a dedicated bucket. (Reusing e.g. the file-upload bucket would muddy IAM and lifecycle for no real saving.)

### 4.2 Object key layout

Content-addressed, mirroring `memory/store.py` and the skills resource store:

```
shares/{share_id}/{content_hash}
```

- `content_hash` = `sha256(body_bytes)` hex.
- One object per share (a share is immutable once created — `update_share` only touches access-control fields, never the body). Content-addressing gives us free idempotency on retry: a re-run of the same `create_share` body writes the same key.
- Keyed under `share_id` so §5.4 revoke can delete the object by known key, and a stray-object sweep can list by `shares/{share_id}/` prefix.

### 4.3 DynamoDB item shape (new writes)

```python
item = {
    "share_id": share_id,
    "session_id": session_id,
    "owner_id": user.user_id,
    "owner_email": user.email,
    "access_level": request.access_level,
    "created_at": now,
    "allowed_emails": [...],          # when access_level == "specific"
    "body_ref": {                     # ← NEW: pointer replaces inline body
        "bucket_key": "shares/{share_id}/{hash}",
        "format": "json",             # serialization of the body object
        "schema_version": 1,          # snapshot schema, for forward migration
        "byte_size": 812345,          # observability / future gating
    },
    # NOTE: no "messages" / "metadata" attributes on new items
}
```

The body object itself is the JSON serialization of:

```json
{ "metadata": { ...session metadata snapshot... },
  "messages": [ ...MessageResponse dicts... ] }
```

Serialized with plain `json.dumps` (UTF-8 bytes). **The float→Decimal conversion is dropped for the offloaded body** — that conversion only exists to satisfy DynamoDB's boto3 resource, which rejects Python floats. S3 stores opaque bytes, so we serialize the raw Pydantic `model_dump` directly and skip `_convert_floats_to_decimal` for anything going to S3. (Control fields going into DynamoDB are all strings/lists, so no Decimal concern there.) This also sidesteps the read-side `Decimal`→float round-trip.

### 4.4 Backward compatibility

Reads must handle three item shapes:

| Item shape | How it's read |
|---|---|
| **New** (`body_ref` present, no inline `messages`) | fetch body from S3 via `store.get(body_ref["bucket_key"])` |
| **Legacy inline** (`messages`/`metadata` present, no `body_ref`) | read inline exactly as today |
| **Malformed** (neither) | raise `ShareNotFoundError` / log; treat as unreadable |

No data migration is required — existing inline shares keep working untouched. New shares are S3-backed. Optional one-time backfill is **out of scope** (existing inline shares are, by definition, already small enough to have been written).

---

## 5. Backend changes

All under `backend/src/apis/app_api/shares/`, plus one shared store.

### 5.1 New: snapshot body store

`apis/app_api/shares/snapshot_store.py` — a thin S3 put/get keyed by `share_id`, structurally identical to `memory/store.py` but domain-scoped to shares. (It lives under `app_api/shares/` because app-api is the only consumer; if a second consumer ever appears it moves to `apis/shared/` per the import-boundary rule. Memory's store is under `apis/shared/` precisely because both app-api and inference-api use it — shares are app-api-only.)

```python
class ShareSnapshotStore:
    def __init__(self, bucket_name: str | None = None, s3_client=None): ...
    @property
    def enabled(self) -> bool: ...            # bucket configured AND boto3 present
    def put(self, *, share_id: str, body: bytes) -> str:   # → bucket_key
    def get(self, bucket_key: str) -> bytes:
    def delete(self, bucket_key: str) -> None:             # best-effort
```

- `put`: content-address (`sha256`), `head_object` dedupe, `put_object` with `ServerSideEncryption="AES256"`, `ContentType="application/json"`.
- `get`: `get_object`; `NoSuchKey`/`404` → a typed `ShareSnapshotStoreError` the service maps to a friendly error.
- Errors raise `ShareSnapshotStoreError` (module-local), consistent with `MemorySpaceStoreError`.

Env var: **`SHARED_CONVERSATIONS_BUCKET_NAME`** (naming parallels the existing `SHARED_CONVERSATIONS_TABLE_NAME`).

### 5.2 `create_share`

- Build `metadata_snapshot` + `messages_snapshot` as today (still via `model_dump(by_alias=True, exclude_none=True)`), **without** `_convert_floats_to_decimal` for the S3 body.
- Serialize `{"metadata": ..., "messages": ...}` → UTF-8 JSON bytes.
- `bucket_key = store.put(share_id=share_id, body=body_bytes)`.
- Put the item with `body_ref` and **no** inline `messages`/`metadata`.
- If `store.enabled` is False (bucket unset) → raise a new `ShareStorageUnavailableError` (maps to a specific message, §5.5), instead of silently falling back to inline (which would reintroduce the 400 KB cliff).

### 5.3 Read paths

- `_get_share_item` unchanged (still `get_item` by `share_id`).
- New helper `_load_snapshot_body(item) -> tuple[metadata, messages]`:
  - if `item.get("body_ref")`: `json.loads(store.get(item["body_ref"]["bucket_key"]))` → `(body["metadata"], body["messages"])`.
  - elif `item.get("messages") is not None`: legacy inline → `(item.get("metadata", {}), item["messages"])`.
  - else: log + raise `ShareNotFoundError`.
- `_build_shared_conversation_response` and `export_shared_conversation._copy_messages_to_memory` consume `messages` from this helper rather than `item["messages"]` directly.
- `get_shares_for_session` / `_build_share_response` **do not** need the body — they only surface control fields — so listing stays a pure DynamoDB read with **no** S3 fetch. (Good: the list view is unaffected and fast.)

### 5.4 Delete / revoke

- `revoke_share`: after `delete_item`, best-effort `store.delete(body_ref["bucket_key"])` (guarded on `body_ref` present).
- `delete_shares_for_session`: for each item being batch-deleted, best-effort delete its S3 object. (These are cleanup paths; an S3 delete failure logs but never blocks the DynamoDB delete — matching the "revoked link stops working" guarantee, which is enforced by the DynamoDB row's absence, not the object's.)
- Legacy inline items have no `body_ref` → nothing to delete in S3.

### 5.5 Friendlier errors (route layer)

Replace the bare `500` with specific handling in `routes.create_share`:

- New `ShareStorageUnavailableError` → `503 {"detail":"Sharing is temporarily unavailable. Please try again later."}` (bucket unset / boto3 missing — a config problem, not the user's fault).
- The 400 KB path essentially disappears for the body. If a *control* item still somehow exceeds limits (it can't in practice — it's a handful of strings), the generic `500` remains as the final catch-all, but with the item-size failure mode designed out.
- The catch-all `except Exception` stays as a backstop but the common failure now has a real message.

> The SPA already renders `detail` from error responses, so a clearer `detail` string improves UX with zero frontend change. (A dedicated toast copy tweak is optional and out of scope here.)

---

## 6. Infrastructure changes

### 6.1 Construct

`SharedConversationsConstruct` gains a `public readonly bucket: s3.Bucket` (§4.1) and an SSM publication for symmetry with the table:

```ts
new ssm.StringParameter(this, 'SharedConversationsBucketNameParameter', {
  parameterName: `/${config.projectPrefix}/shares/shared-conversations-bucket-name`,
  stringValue: this.bucket.bucketName,
  ...
});
```

### 6.2 Platform wiring

- `platform-stack.ts`: `this.sharedConversationsBucket = sharedConversations.bucket;` and add to the `PlatformComputeRefs` bundle passed to app-api (alongside the existing `sharedConversationsTable`).
- `platform-compute-refs.ts`: add `sharedConversationsBucket: s3.IBucket;`.
- `app-api-environment.ts`: thread `sharedConversationsBucketName` and set env `SHARED_CONVERSATIONS_BUCKET_NAME: params.sharedConversationsBucketName`.

### 6.3 IAM grant (app-api task role)

Add to `app-api-iam-grants.ts`, mirroring `MemorySpacesBucketReadWrite`:

```ts
taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
  sid: 'SharedConversationsBucketReadWrite',
  effect: iam.Effect.ALLOW,
  actions: ['s3:GetObject', 's3:PutObject', 's3:DeleteObject', 's3:ListBucket'],
  resources: [
    props.refs.sharedConversationsBucket.bucketArn,
    `${props.refs.sharedConversationsBucket.bucketArn}/*`,
  ],
}));
```

app-api is the only reader/writer (create writes; view/export read; revoke deletes). Inference-api is **not** granted — it never touches shares. This respects the service boundary (`feedback_service_boundaries`).

### 6.4 Local dev

Add `SHARED_CONVERSATIONS_BUCKET_NAME=` to `backend/src/.env.example` next to the existing `SHARED_CONVERSATIONS_TABLE_NAME`. Locally unset → `store.enabled == False` → create returns the `503` "temporarily unavailable" message rather than a confusing 500, which is the honest state of a machine with no bucket.

---

## 7. Backward-compat & rollout

1. **Deploy order:** `platform.yml` (CDK — creates bucket, grant, env) **before** the `backend.yml` app-api image that writes `body_ref`. Because reads fall back to inline, an app-api that predates the bucket keeps working; an app-api that has the code but no bucket yet returns the `503` on *create* only (reads unaffected). So the CDK deploy must land first, but there is no hard coupling that breaks reads.
2. **Existing inline shares** keep resolving via the legacy read path indefinitely. No migration.
3. **Rollback:** reverting the app-api image returns to inline writes (large shares fail again, as before) but every S3-backed share created in the interim becomes unreadable by the old code (it doesn't know `body_ref`). This is the one rollback caveat — **note it in the PR.** Mitigation if that matters: land the *read* support (fallback + `body_ref` handling) in an earlier, separately-deployed change than the *write* switch, so a rollback target already understands `body_ref`. See §9 phasing.

---

## 8. Testing

Existing suites to extend (all present): `tests/routes/test_shares.py`, `test_share_export.py`, `test_share_properties.py`, `tests/apis/app_api/shares/`.

- **Store unit tests** (moto S3): put→get round-trip, dedupe on identical body, `NoSuchKey`→typed error, `enabled` False when bucket unset.
- **create_share** writes `body_ref` and **no** inline `messages`; body object in S3 decodes to the snapshot.
- **Large conversation:** a snapshot > 400 KB now succeeds (regression test for the actual bug — build a message list that exceeds 400 KB inline and assert `create_share` returns 201).
- **Legacy read:** an item with inline `messages` and no `body_ref` still resolves via `get_shared_conversation` and `export_shared_conversation`.
- **Revoke** deletes the S3 object; `delete_shares_for_session` cleans up objects.
- **Storage-unavailable:** bucket unset → `create_share` → `503` with the friendly detail.
- **Float handling:** a message with a float (e.g. a cost/score) round-trips through S3 JSON without the `Decimal` dance and validates back into `MessageResponse`.
- **Infra:** `infrastructure` jest snapshot updated for the new bucket + grant; `npx tsc --noEmit` + `npx cdk synth` clean.

---

## 9. Phasing (PRs into `develop`)

Small, reviewable, rollback-aware:

- **PR-1 — Read support + infra (no behavior change to writes).**
  CDK: bucket, SSM, compute-ref, env, IAM grant. Backend: `ShareSnapshotStore`, `_load_snapshot_body` with legacy fallback wired into read/export paths. **Writes still inline.** Deploying this makes every running app-api `body_ref`-aware *before* anything writes one — this is the rollback-safety anchor (§7.3).
- **PR-2 — Switch writes to S3.**
  `create_share` serializes + `store.put` + `body_ref` item, drops inline body; revoke/session-cleanup delete objects; `ShareStorageUnavailableError` + friendly `503`. Tests: large-conversation regression, storage-unavailable.
- **PR-3 (optional) — SPA copy polish.**
  Nicer toast for the `503`/failure states. Pure frontend; can be skipped if the `detail` passthrough is deemed sufficient.

Each PR branches from `develop`, targets `develop`, conventional-commit titled (`feat(shares): ...`).

---

## 10. Alternatives considered

- **Size-gated inline vs. S3.** Rejected — two code paths, and the gate itself has to model DynamoDB item overhead correctly or it just relocates the cliff (§3).
- **Compress inline (gzip the body attribute).** Buys a ~3–5× headroom but does not remove the ceiling; a big enough conversation with images still exceeds 400 KB compressed, and it adds an opaque binary blob to DynamoDB that PITR/console can't inspect. Rejected as a stopgap, not a fix.
- **Split across multiple DynamoDB items (chunking).** Reintroduces multi-item transactional complexity (partial writes, ordering) that S3 avoids entirely. Rejected.
- **Reference the live session instead of snapshotting.** Would eliminate the copy, but breaks the share's point-in-time semantics and its independence from later edits/deletes of the source session (a share deliberately survives the owner continuing or deleting the conversation — see `delete_shares_for_session`'s "exported conversations unaffected" note). Rejected — changes product behavior.

---

## 11. Open questions

1. **Dedicated bucket vs. reuse** (§4.1 decision point). Spec assumes dedicated, per sibling-feature precedent. Confirm.
2. **Backfill of legacy inline shares** — spec says skip (they're already small). Confirm no desire to normalize storage.
3. **SPA toast copy** — is the `detail` passthrough enough, or do we want PR-3?
4. **Object encryption** — S3-managed (SSE-S3/AES256) matches every sibling bucket. A share body is the same data-class as the session it came from (behind the same auth), so no case for KMS/CMK here (`feedback_governance_via_identity_claims`). Confirm.
