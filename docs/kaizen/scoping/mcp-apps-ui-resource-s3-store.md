# Scoping — MCP Apps UI-Resource Store: S3 + Content-Hash Dedupe

> Status: Scoping (no code yet)
> Owner: Phil Merrell
> Source: follow-up to PR [#413](https://github.com/Boise-State-Development/agentcore-public-stack/pull/413) (`feat(mcp-apps): persist UI resources so the mcp-app-frame survives a refresh`, merged to develop as `90bf4cf3`; CSP follow-up `#414`/`a8d82baf`). Reference pattern: the Artifacts feature (`docs/kaizen/scoping/` ▸ artifacts; `backend/src/agents/builtin_tools/artifacts/service.py` + `infrastructure/lib/artifacts-stack.ts`).
> Spec context: MCP Apps (SEP-1865), `docs/kaizen/scoping/mcp-apps-host-renderer.md`. The `ui_resource` SSE event contract is **unchanged** by this work.

## TL;DR

PR #413 added reload persistence for model-initiated MCP App UI resources by **gzipping the App HTML into a DynamoDB Binary attribute** (`htmlGz`) on the existing `sessions-metadata` table, keyed `SK=UIRES#<toolUseId>`. That shipped and works for today's Apps, but it inherits DynamoDB's hard **400 KB item limit** and stores one **full copy of a largely static, server-owned shell per `toolUseId`**.

This doc scopes moving the HTML blob out of DynamoDB and into **S3 with content-hash (sha256) dedupe**, leaving DynamoDB holding only a small pointer row. The frontend contract (`uiResources` sidecar carrying inline `html`) does **not change** — only the backend storage substrate does, so **there is no frontend work**.

**Headline recommendations:**

1. **No new CDK stack, no new Lambda (for v1).** The content store is a single *private* S3 bucket read **server-side** by app-api — no CloudFront / cert / Route53 / render-Lambda (unlike Artifacts, whose bucket is browser-facing). Fold the bucket into the existing **Tier-0 `infrastructure-stack.ts`**, next to the `sessions-metadata` table whose rows point at it. This sidesteps the heavyweight "New CDK stack" checklist entirely.
2. **Lifecycle = never delete deduped objects in v1.** Content-addressing bounds the object set to *the number of distinct App shells/versions* (kilobytes-to-low-megabytes total), not the number of conversations. The trap to avoid is deleting a shared object on a single pointer row's TTL expiry. True garbage collection, if ever justified, is a **reference-based mark-and-sweep** (deferred), never an age-based S3 lifecycle rule.
3. **Dual-read migration is now mandatory.** #413 has merged to develop, so live `htmlGz` Binary rows exist. The new read path must read `htmlHash`→S3 when present and fall back to legacy `htmlGz`→inline otherwise, for one 90-day TTL window, after which a cleanup PR removes the legacy branch. **No backfill** — TTL drains the old rows.

## Why replace the interim implementation

From `ui_resource_store.py` (current, on develop):

- **400 KB ceiling.** DynamoDB caps an item (attribute names + values) at 400 KB. The interim impl gzips the HTML and applies `_MAX_HTML_GZ_BYTES = 380_000` to the *compressed* size; anything over it is silently **not persisted** (degrades to a plain tool card on reload). A real Excalidraw App is ~443 KB raw / ~131 KB gzipped — fits today, but the gzip headroom tops out around ~1.3 MB of source HTML. Bigger or less-compressible Apps (charts with inlined data, WASM-heavy shells, base64 assets) blow the cap and lose reload survival. S3's per-object limit is 5 TB — the ceiling disappears.
- **Redundant copies.** The App HTML is a **server-owned, invocation-independent shell** fetched via `resources/read` against a `ui://` URI — per-invocation data flows over the postMessage channel (`ui/notifications/tool-input` / `tool-result`), never baked into the HTML. So N invocations of the same diagram tool persist **N identical ~131 KB blobs**. Content-hash dedupe collapses those to **one** S3 object plus N tiny pointer rows.

Net: S3 removes the size ceiling *and* the duplication in one move.

## Target design

```
                 inference-api (write)                    app-api (read)
                 ─────────────────────                    ──────────────
  ui_resource → sha256(raw_html)                  pointer row → htmlHash
  payload       PutObject(if-absent)  ┌── S3 ──┐  GetObject ─────┘
                mcp-apps/<hash>.html.gz│ deduped│  gunzip → inline `html`
                       │               │ blobs  │        │
                       ▼               └────────┘        ▼
            DynamoDB pointer row  ◀── htmlHash ──▶  uiResources sidecar
            (sessions-metadata,                    (shape == ui_resource
             SK=UIRES#<toolUseId>)                  SSE event, unchanged)
```

### S3 layout + dedup keying

- **Bucket:** one private bucket, e.g. `getResourceName(config, 'mcp-apps-content')`. `BLOCK_ALL` public access, `enforceSSL`, `S3_MANAGED` encryption — same hardening as the artifacts content bucket, **minus** all the browser-serving machinery (no CloudFront, no OAC, no cert, no Route53, no render Lambda). app-api reads it directly with a SigV4 GetObject.
- **Key:** `mcp-apps/<sha256-hex>.html.gz`. The `mcp-apps/` prefix scopes the dedupe namespace (and any future lifecycle rule) to this feature within the bucket.
- **Hash over RAW HTML, not the gzip bytes.** `htmlHash = sha256(html.encode("utf-8")).hexdigest()`. gzip output is **not** byte-deterministic across zlib versions / compression levels / platforms, so hashing the compressed bytes would fragment dedupe (same HTML → different gzip → different key). Hashing the raw UTF-8 gives a stable content identity; the object *body* is `gzip.compress(raw)`.
- **Object body + metadata:** gzipped HTML. Set `ContentType` to the App's `mimeType` (e.g. `text/html;profile=mcp-app`) and `ContentEncoding: gzip` for correctness, though app-api gunzips server-side rather than relying on transfer decoding. Optionally stamp `x-amz-meta-raw-bytes` / `x-amz-meta-gz-bytes` for observability.
- **Global (cross-user) namespace is safe.** The deduped object holds **only server-owned App shell HTML — never user data** (per the invocation-independence above), so a single global content-addressed namespace is correct and leak-free. Per-user access control stays entirely on the **pointer rows** (the read path re-checks `userId` against the session-GSI row, exactly as the interim store and the Artifacts/card stores already do). The bucket is never reachable by browsers. **Adversarial note:** even if a server *did* return per-invocation-customized HTML embedding user data, content-hash still partitions it correctly (different content → different hash → different object) and access still gates on the pointer row — so the worst case is a *lower dedupe ratio*, not a leak.

### Pointer-row schema

The `UIRES#` row drops `htmlGz` and gains `htmlHash`. Everything else (keys, GSI, ownership, TTL) is unchanged from the interim impl:

| Attribute | Value | Notes |
|---|---|---|
| `PK` | `USER#<user_id>` | unchanged |
| `SK` | `UIRES#<tool_use_id>` | unchanged — last-write-wins per invocation (matches `recordLive`) |
| `GSI_PK` | `SESSION#<session_id>` | `SessionLookupIndex` (projection ALL) — unchanged |
| `GSI_SK` | `UIRES#<created_at>` | unchanged |
| `htmlHash` | `<sha256-hex>` | **new** — replaces `htmlGz` Binary blob |
| `resourceUri` | `ui://…` | unchanged |
| `mimeType` | `text/html;profile=mcp-app` | unchanged |
| `csp` | object | unchanged |
| `permissions` | object | unchanged |
| `sandboxOrigin` | string | unchanged — read path still prefers the fresh env origin over the stored value |
| `producedByMessageIndex` | int or null | unchanged (currently always null; stamping it post-turn like Artifacts is **out of scope** here) |
| `userId`, `sessionId`, `createdAt`, `ttl` | — | unchanged; `ttl` = 90 days |

The pointer row is now ~1 KB regardless of App size — no risk of nearing the 400 KB item limit.

### Write path (inference-api → S3 PutObject, idempotent put-if-absent)

In `ui_resource_store.store(...)` (called from `stream_coordinator._extract_ui_resource_events` via `asyncio.to_thread`, unchanged call site):

1. `raw = html.encode("utf-8")`; `htmlHash = sha256(raw).hexdigest()`; `gz = gzip.compress(raw)`.
2. **Put-if-absent** to `mcp-apps/<htmlHash>.html.gz` using S3 conditional writes — `put_object(..., IfNoneMatch="*")`. A `412 PreconditionFailed` means the object already exists → **skip the upload** (this is the dedupe win: N identical Apps = 1 upload). Treat 412 as success.
   - *Compatibility fallback:* if the deployment's S3 endpoint lacks conditional-write support, fall back to `HEAD`-then-`PUT`. The race window is harmless because the writes are content-addressed — two concurrent PUTs of identical bytes are idempotent.
3. **Then** write the pointer row with `htmlHash` (object-first ordering — see below).
4. **Remove `_MAX_HTML_GZ_BYTES`.** S3 has no 400 KB limit. Keep only a generous sanity guard (e.g. a few MB of *raw* HTML) to reject pathological payloads; it is no longer the binding constraint.

**Ordering — object first, pointer row second.** This guarantees a committed pointer row never references a missing object. If the pointer write fails *after* a successful PutObject, the result is a **harmless orphan object** (covered by the lifecycle section), never a dangling pointer. (Reverse ordering would risk a pointer row that 404s on read.)

**Still best-effort.** A PutObject failure logs and skips the pointer row (App still rendered live via the SSE event; only reload survival is lost — identical to today's contract). Never raises into the stream.

### Read path (app-api → S3 GetObject → gunzip → inline)

In `ui_resource_store.list_for_session(...)` (called from `messages.get_messages_from_cloud` → `fetch_ui_resources`, unchanged call site):

1. Query `UIRES#` rows off `SessionLookupIndex` (unchanged), re-check `userId` ownership (unchanged).
2. For each row, resolve `html`:
   - **`htmlHash` present** → GetObject `mcp-apps/<hash>.html.gz` → `gzip.decompress` → `.decode("utf-8")` → set `resource["html"]`.
   - **legacy `htmlGz` present** (no `htmlHash`) → gunzip inline, exactly as today (migration fallback — see Back-compat).
3. **Per-hash fetch cache within the call.** Several rows in one session can share a `htmlHash` (same App invoked N times — distinct `toolUseId`s, identical content). Fetch each *distinct* hash from S3 **once** and reuse the gunzipped HTML across rows, so a session that called Excalidraw five times does one GetObject, not five. The sidecar still emits one entry per `toolUseId` (the frontend keys by `toolUseId`).
4. **Graceful degrade on miss.** A 404 (object swept/never-written) or a corrupt body **drops that one resource from the sidecar** rather than erroring the messages endpoint — the App falls back to a plain tool card, the same non-fatal degrade the interim store already applies to corrupt rows. This is the property that makes *any* lifecycle/GC race non-fatal (see below).

The sidecar each row produces is byte-for-byte the shape the SPA already consumes (`{type, toolUseId, resourceUri, html, mimeType, csp, permissions, sandboxOrigin}`), so `McpAppStateService.seedFromHydration` and `UiResourceEvent` are untouched. **Zero frontend changes.**

## Lifecycle of shared / deduped S3 objects

This is the subtle part: **a single content-hash object may be referenced by many pointer rows** — across turns, sessions, and users. The pointer rows expire independently (90-day DynamoDB TTL); the object must outlive **all** of them.

### The trap (do NOT do these)

- ❌ **DeleteObject when a pointer row's TTL fires.** A row's expiry says nothing about whether *other* rows still reference the same hash. Per-row deletes would yank a shared shell out from under live conversations. DynamoDB TTL deletion is also silent (no callback) — you'd need a Stream consumer just to attempt this wrong thing.
- ❌ **Age-based S3 lifecycle expiration (`expiration: Duration.days(N)`).** Content-addressing + put-if-absent means an object's S3 `LastModified` reflects its **first** write, not its **most recent reference**. A two-year-old Excalidraw shell may be referenced by a row written this morning (put-if-absent no-op'd because the bytes already existed). An age-based rule would delete still-referenced objects. **Object age is decoupled from reference recency** — this is the single most important gotcha in this design.

### v1: never delete (recommended)

The whole point of content-hash dedupe is that **distinct content is bounded by the App catalog, not by traffic.** The set of objects that ever exist ≈ the number of distinct App shells × versions ever rendered — realistically dozens to low hundreds of objects, kilobytes-to-low-megabytes total. So the simplest *correct* lifecycle is to **let them accumulate**:

- Zero GC code, zero risk of deleting a live object.
- Storage cost is rounding-error (pennies/year).
- Orphans (an App retired, all referencing conversations aged out) linger, but they're tiny and few.

Keep the one cheap, always-safe S3 lifecycle rule the artifacts bucket also uses — **abort incomplete multipart uploads after 7 days** — to avoid stranded partial uploads. That rule is age-on-*upload-parts*, not on completed objects, so it does not interact with the reference problem.

### Deferred: reference-based mark-and-sweep (only if storage ever justifies it)

If telemetry ever shows the bucket growing meaningfully (e.g. servers emitting high-cardinality per-invocation HTML, defeating dedupe), add a **scheduled mark-and-sweep**, never an age rule:

1. **Mark:** scan live `UIRES#` pointer rows (table Scan or the GSI), collect the set of referenced `htmlHash` values → `live_hashes`.
2. **Sweep:** list objects under `mcp-apps/`; for each object whose hash ∉ `live_hashes` **and** whose `LastModified` is older than a grace period (e.g. 7 days), `DeleteObject`.

- **Reference-based, not age-based** — deletion is driven by "no live pointer references this hash," with the age check only as a guard against racing a just-written object whose pointer row write is mid-retry.
- **The residual mark→sweep race** (a new turn references a previously-orphaned hash between the snapshot and the delete; put-if-absent no-ops because the object still exists; the sweep then deletes it) is **non-fatal** precisely because the read path degrades a 404 to a plain tool card. The user sees, at worst, a card instead of a live iframe for one resource — never an error. Re-rendering that App re-PUTs the object.
- This is the **"New Lambda"** path (see checklist) — an EventBridge-scheduled Lambda in `backend/src/lambdas/mcp_apps_content_gc/`, granted `s3:ListBucket` + `s3:DeleteObject` on `mcp-apps/*` and `dynamodb:Scan` on the table + GSI. Carry it **only** when justified; v1 ships without it.

## Infra / IAM changes

**Recommended placement — extend the Tier-0 `infrastructure-stack.ts` (no new stack):**

- **New S3 bucket** `mcp-apps-content` in `infrastructure-stack.ts`, alongside the `sessions-metadata` table whose `UIRES#` rows point at it. Private, `BLOCK_ALL`, `enforceSSL`, `S3_MANAGED` encryption, `getRemovalPolicy(config)` / `getAutoDeleteObjects(config)`, and the single `abort-stale-multipart` lifecycle rule.
- **New SSM params** (the outward contract), written by infrastructure-stack:
  - `/{prefix}/mcp-apps/content-bucket-name`
  - `/{prefix}/mcp-apps/content-bucket-arn`
- **inference-api-stack.ts (Tier 2, write):** read the SSM params, inject env `S3_MCP_APPS_CONTENT_BUCKET_NAME`, and grant `s3:PutObject` (and `s3:PutObjectTagging` if metadata tagging is used) on `arn:…:mcp-apps-content/mcp-apps/*`. **No DeleteObject grant** (write-once content store, mirroring the artifacts "no delete in inference-api" stance).
- **app-api-stack.ts (Tier 3, read):** read the SSM params, inject env `S3_MCP_APPS_CONTENT_BUCKET_NAME`, and grant `s3:GetObject` on `arn:…:mcp-apps-content/mcp-apps/*`.
- **Resolution helper** in the store mirrors `artifacts/service.py._resolve`: env var first, then SSM `/{prefix}/mcp-apps/content-bucket-name` (inference-api and app-api both expose `PROJECT_PREFIX` and hold `ssm:GetParameter` on `/{prefix}/*`). The interim store already resolves the *table* name from an env var injected by **both** stacks (`DYNAMODB_SESSIONS_METADATA_TABLE_NAME`), so injecting one more env var into both is the established pattern.

**DAG safety:** infrastructure-stack (tier 0) *writes* the new SSM params; inference-api (tier 2) and app-api (tier 3) *read* them — both strictly later tiers. `infrastructure/test/stack-dependencies.test.ts` passes with **no tier change** because no new stack is introduced.

**Gating:** the host-renderer surface is gated by the backend env flag `AGENTCORE_MCP_APPS_HOST_ENABLED` (default true), *not* a CDK flag. The bucket + grants are cheap and harmless when unused, so create them **unconditionally** (matching how `sessions-metadata` access is unconditional) — no new `CDK_MCP_APPS_*` flag, no new `config.ts` field. If a reviewer prefers strict gating, a `config.mcpAppsContent.enabled` flag wired like `config.artifacts.enabled` is the drop-in, but it is not recommended for a single private bucket.

### CLAUDE.md "New CDK stack / New Lambda" checklist

Per `CLAUDE.md` ▸ File Creation Rules. Walked explicitly, since the deliverable calls for it:

- **"New CDK stack"** (register in `test/stack-dependencies.test.ts` with a tier; add `scripts/stack-<name>/`; add a `.github/workflows/` workflow; update `step-04-deploy.md`): **Not triggered by the recommendation.** We add a resource to the existing Tier-0 `infrastructure-stack.ts`, not a new stack — so none of these apply. The deploy-order note in `CLAUDE.md` is unaffected (no new parallel-safe stack).
  - *If Option B (dedicated `mcp-apps-content-stack.ts`) were chosen instead* — e.g. for independent gating/deploy — then all four items **would** apply: tier 1 in `stack-dependencies.test.ts` (reads no cross-stack SSM, writes only `/mcp-apps/content-*`, parallel-safe with the other tier-1 stacks like `ArtifactsStack`/`McpSandboxStack`); `infrastructure/scripts/stack-mcp-apps-content/`; `.github/workflows/mcp-apps-content.yml` (model after `artifacts.yml`); and a `step-04-deploy.md` entry. **Option B is not recommended** — it is disproportionate machinery for one private, server-read bucket with no browser-serving surface.
- **"New Lambda for an infra stack"** (`backend/src/lambdas/<lambda-name>/`, one folder per Lambda, outside the `apis/` import boundary): **Not triggered in v1** (read/write happen inside the existing inference-api and app-api containers, not a Lambda). **Only** triggered by the deferred mark-and-sweep GC, which would live in `backend/src/lambdas/mcp_apps_content_gc/` and be wired to infrastructure-stack with an EventBridge schedule.
- **"Shared backend code"** (`apis/shared/<domain>/`): the store stays at `apis/shared/mcp_apps/ui_resource_store.py`. Still read by inference-api (write) and app-api (read), neither importing the other — `tests/architecture/test_import_boundaries.py` is unaffected.

## Back-compat / migration

**#413 has merged to develop** (`90bf4cf3`), so deployed environments will accumulate legacy `UIRES#` rows carrying the `htmlGz` Binary blob and **no** `htmlHash`. The "land the S3 design before #413 merges" option is therefore no longer available; **dual-read is required.**

- **Read path dual-reads** for one TTL window: `htmlHash` present → S3; else `htmlGz` present → gunzip inline (today's code). New writes are **always** S3 + `htmlHash`.
- **No backfill.** Legacy rows carry the 90-day `ttl`; they drain on their own. After ≥ 90 days from the refactor's deploy, no `htmlGz`-only rows remain.
- **Cleanup PR** removes the legacy `htmlGz` branch (and the `_to_bytes` Binary helper) once the TTL window has elapsed.
- **Mixed-state safety:** the dual-read covers any row written in the gap between deploying the new write path and old rows expiring; the graceful-404 degrade covers any pointer whose object is somehow absent. No coordinated cutover, no downtime.

## Recommended PR sequence

Targets `develop`. The write path degrades gracefully if the bucket/grants aren't present yet (best-effort skip → tool card), so the ordering below is *preferred* but not a hard barrier.

| PR | Scope | Notes |
|---|---|---|
| **PR 1 — infra** | Add the `mcp-apps-content` bucket + `/{prefix}/mcp-apps/content-*` SSM params to `infrastructure-stack.ts`; add the `s3:PutObject` grant + env to `inference-api-stack.ts`; add the `s3:GetObject` grant + env to `app-api-stack.ts`. | Tier-0 resource + later-tier grants; `stack-dependencies.test.ts` unchanged (no new stack). Deployable on its own; inert until PR 2. |
| **PR 2 — backend store refactor** | Rewrite `ui_resource_store.py`: sha256(raw) hashing, S3 put-if-absent write, drop `_MAX_HTML_GZ_BYTES`, pointer row `htmlHash`, read path GetObject + gunzip + per-hash cache + graceful-404, **dual-read** legacy `htmlGz`. Update the module docstring and the `CLAUDE.md` SSE-event-table note (the `ui_resource` *payload* is unchanged; only the persistence substrate moves). | No call-site changes in `stream_coordinator` or `messages` (signatures unchanged). No frontend change. Backend pytest is the gate. |
| **PR 3 — legacy cleanup** (≥ 90 days after PR 2 deploys) | Remove the `htmlGz` dual-read branch + `_to_bytes` helper. | Trivial; gated on the TTL window having elapsed in all deployed envs. |
| **PR 4 — GC Lambda** (deferred / optional) | `backend/src/lambdas/mcp_apps_content_gc/` + EventBridge schedule + ListBucket/DeleteObject/Scan grants. | Carry **only** if bucket-size telemetry ever justifies it. Reference-based mark-and-sweep, never age-based. Triggers the full "New Lambda" checklist. |

## Testing

Backend pytest is the only correctness gate (not run in CI), so the suite must be the proof:

- **Hash determinism:** same HTML → same key; different HTML → different key.
- **Put-if-absent / dedupe:** two `store()` calls with identical HTML → exactly one PutObject body (second 412s and is skipped); two pointer rows.
- **Read round-trip:** `store()` → `list_for_session()` returns the inline `html`, byte-identical to the input.
- **Per-hash read cache:** a session with N rows sharing a hash issues one GetObject.
- **Graceful degrade:** GetObject 404 / corrupt gzip → that resource is dropped from the sidecar, others survive, no exception.
- **Dual-read:** a legacy `htmlGz`-only row still hydrates; a `htmlHash` row reads S3; a session mixing both returns both.
- **Disabled/dev:** no bucket env + no table → no-op write, empty read (matches the interim "Apps are live-only in dev" behavior).
- Use `moto` (or a hand fake matching the existing in-memory DynamoDB fake) for S3; assert the real `IfNoneMatch`/412 contract rather than a stub that mirrors our own code.
- `tests/architecture/test_import_boundaries.py` still passes (module stays in `apis.shared`).

## Risks & open questions

- **S3 conditional writes availability.** `IfNoneMatch="*"` is supported by AWS S3; confirm the boto3 pin in `backend/pyproject.toml` is new enough, and keep the HEAD-then-PUT fallback for any S3-compatible local/dev endpoint that lacks it. (Content-addressing makes the fallback's race harmless.)
- **`producedByMessageIndex` stays null.** The interim write path doesn't stamp it (Artifacts does, post-turn, via `set_produced_by_message_index`). Wiring an equivalent stamp for UI resources is an orthogonal nicety — **out of scope** here to keep the refactor to "change the storage substrate only."
- **Telemetry for the GC decision.** Add a CloudWatch metric/log on bucket object count (or rely on S3 Storage Lens) so the "do we ever need mark-and-sweep?" call is data-driven, not guessed. Default expectation: we never need it.
- **Cross-region.** The store resolves region the same way `artifacts/service.py` does (`AWS_REGION` → `AWS_DEFAULT_REGION` → `us-west-2`); the bucket is single-region, co-located with the runtime. No cross-region read path.
