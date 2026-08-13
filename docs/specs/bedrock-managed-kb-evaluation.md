# Bedrock Managed Knowledge Base — evaluation and target topology

**Status:** Evaluation complete, target topology proposed. Benchmark and
implementation-readiness gates defined below; no product implementation.
**Question asked:** Can Bedrock Managed Knowledge Base replace our custom RAG
pipeline? Given usage, pricing and quotas, should a user have *multiple* KBs per
agent, or one KB filtered by agent id? And is it a fit for impromptu document
uploads in a conversation?
**Evidence:** AWS Price List API query + a live 3-KB probe in dev-ai
(490617140655, us-west-2), both 2026-08-11. Every number below is either quoted
from official AWS docs, extracted from the Price List API, or measured. Blog and
unverified claims are marked as such.
**Supersedes:** the KB-per-assistant decision and the `CUSTOM`/`WEB` data-source
shapes in the earlier `bedrock-managed-knowledge-base` draft (that draft reasoned
from *classic* KB quotas, which no longer bind).
**Related:** `docs/specs/assistant-kb-sync.md` (the re-index feature that must be
rehosted), `docs/specs/document-context-offload.md` (the correct home for
conversation attachments), `docs/specs/RAG_KEEP_WARM_SPEC.md`

---

## 1. Current state — what a replacement would displace

| Piece | Where | Notes |
|---|---|---|
| Ingestion trigger | S3 `ObjectCreated` on `assistants/` prefix, wired in `infrastructure/lib/platform-stack.ts:434` | Notification lives in the stack, not the construct, to avoid a circular dependency |
| Parse + chunk | `backend/src/apis/app_api/documents/ingestion/handler.py:224` | Docling `HybridChunker`, `max_tokens=1024`; CSV/XLSX chunkers at 900 tokens; hard 20 MB reject at line 243 |
| Embed | `backend/src/apis/shared/embeddings/bedrock_embeddings.py:24` | `amazon.titan-embed-text-v2:0`, 1024-dim, hardcoded in Python; CDK carries a *separate* `config.ragIngestion.embeddingModel` used only for the IAM ARN |
| Vector store | `infrastructure/lib/constructs/rag/rag-data-construct.ts:97` | **Amazon S3 Vectors**, raw `CfnResource`. **One global index for the whole deployment**; isolation is a metadata filter only |
| Retrieval | `backend/src/apis/shared/assistants/rag_service.py:18` | Pre-turn prompt augmentation, `top_k=5`, `filter={"assistant_id": ...}`. No agentic retrieval, **no reranking**, no hybrid search |
| Context cap | `rag_service.py:139`, `max_context_length=2000` | **2,000 characters (~500 tokens) reach the model** regardless of what was retrieved |
| Doc-status post-filter | `rag_service.py:71` | Up to 5 *serial* DynamoDB `get_item` calls on the critical path of every RAG turn |
| Re-index | `backend/src/apis/app_api/kb_sync/` + `infrastructure/lib/constructs/kb-sync/` | Shipped. Re-stages bytes to the **same S3 key** to re-fire the pipeline |
| Ingestion Lambda | ARM64, 3008 MB, 900 s, `backend/Dockerfile.rag-ingestion` | **~175 s cold / ~35 s warm**; ~140 s of that is Docling/PyTorch model load. The keep-warm rule in `RAG_KEEP_WARM_SPEC.md` was never implemented |

**KB is not a first-class entity.** It *is* the assistant. `KNOWN_BINDING_KINDS`
includes `knowledge_base`, but `bindable_catalog.py:72` returns `[]` for it and
`binding_validation.py:166` rejects an explicit binding — the relation is
structurally locked 1:1 in three places, all of which anticipate an F4 that turns
`ref` into a real KB id "with no shape change here" (`compat.py:17`).

**Conversation attachments never touch RAG.** Separate bucket, separate table, no
chunking, no vectors — inline `document`/`image` blocks on the user message via
`agents/main_agent/multimodal/prompt_builder.py:23`. 4 MB/file, 5 files/message.

---

## 2. What changed on 2026-06-17

Bedrock **Managed** Knowledge Base went GA. It is a distinct SKU from the classic
customer-managed KB, with its own quota table and no vector store to provision.
The quotas that made "many KBs" unworkable are gone.

| | Classic | **Managed** |
|---|---|---|
| KBs per account/region | 100, **not adjustable** | **10,000, adjustable** |
| Data sources per KB | 5 | 200 |
| Retrieve throughput | 20 rps **account-wide** | **600/min + 25 rps burst, per KB** |
| Concurrent ingestion jobs | 1/KB, 5/account | 50/KB |
| Raw storage per KB | — | 10 TB |
| Connectors | S3, Custom | S3, SharePoint, Confluence, Web Crawler, **Google Drive**, OneDrive, Custom |
| Reranking | you build it | included, managed |
| Agentic retrieval | not supported | supported |

Two of these decide the topology question: KB count stopped being a hard ceiling,
and **Retrieve throughput became per-KB rather than pooled**.

---

## 3. Verified pricing — there is no per-KB floor

Queried from the AWS Price List API (public; any profile, `--region us-east-1`).

**Managed KB bills under service code `AmazonBedrockAgentCore`, not
`AmazonBedrock`.** Searching the Bedrock service code returns nothing. Exactly
three SKUs per region, all `Consumption-based`, all `beginRange=0 → endRange=Inf`
(no tier-0 minimum block):

| usagetype (us-west-2) | rate |
|---|---|
| `USW2-Knowledge-Base:Consumption-based:Storage` | $5.00 / GB-Month |
| `USW2-Knowledge-Base:Consumption-based:Retrieval` | $0.001 / query |
| `USW2-Knowledge-Base:Consumption-based:AgenticRetrieval` | $0.004 / query (stacks on Retrieval) |

**Structural proof of no floor:** of 6,467 AgentCore usagetypes, 6,124 contain
`Hours` — Runtime carries `Instance-based:<type>:Management-Hours`. **Zero of the
21 Knowledge-Base usagetypes do.** AWS demonstrably models hourly floors in this
exact service code and deliberately did not for KB. An idle, empty KB stores
0 GB-Month and bills $0.

*Limit of the evidence:* the Price List is a rate card, not a rounding policy. It
cannot rule out a minimum rounding unit on GB-Month. The dev-ai probe (§5) closes
that empirically. Small residual risk, not a design blocker.

KB SKUs exist in 7 regions only: USW2, USE1, EU, EUC1, EUW2, APN1, APS2. Both our
accounts are us-west-2.

### 3.1 Cost delta vs today

| | Today | Managed KB |
|---|---|---|
| Storage | S3 $0.023/GB + S3 Vectors $0.06/GB (~2× raw) ≈ **$0.15/GB-mo** | **$5.00/GB-mo of raw data** |
| Retrieval | $2.50/M queries ≈ $0.0000025 | $0.001 |
| Parse | Docling Lambda ≈ $0.005/doc | included |
| Embed | Titan v2 per chunk | included |
| Rerank | **none today** | included |

Retrieval is noise (~$17.50 at 5 RAG turns × 3,500 sessions; ~$52 with 3-way
fan-out) against $0.02–0.05 turn costs. **Storage is the whole story: ~33× more
expensive per GB.** 10 GB corpus = $50/mo; 50 GB = $250/mo. Under ~10 GB the ops
win dominates; above that it needs a deliberate decision.

⇒ **The thing to garbage-collect is gigabytes, not knowledge bases** (§7).

---

## 4. Corrected API shapes

Two things the earlier draft got wrong, both found by calling the real API.

### 4.1 The SDK prerequisite is now satisfied

At evaluation time the repo pinned `boto3 1.43.9` (released **2026-05-15**),
which predates Managed KB GA (**2026-06-17**) and has no `MANAGED` enum —
`knowledgeBaseConfiguration.type` offers only `VECTOR | KENDRA | SQL`. The
probe therefore side-loaded the newer service model without changing the repo:

```
curl -fsSL https://raw.githubusercontent.com/boto/botocore/1.43.68/botocore/data/bedrock-agent/2023-06-05/service-2.json \
  -o $MODELS/bedrock-agent/2023-06-05/service-2.json
AWS_DATA_PATH=$MODELS python ...
```

The current repo now pins **`boto3==1.43.68`**, the same model version used by
the probe. The dependency bump is no longer an implementation prerequisite.
Before a benchmark or PR-1, run the create/ingest/retrieve smoke probe using the
normal checked-in environment with no `AWS_DATA_PATH`; that is the contract test
that the lock and packaged service model are really sufficient.

`bedrock-agentcore-control` still has no KB operations. Provisioning and direct
ingestion use `bedrock-agent`; retrieval uses the Bedrock agent runtime client.

### 4.2 A MANAGED KB rejects the classic data-source types

`dataSourceConfiguration.type` of `CUSTOM`, `S3`, and `WEB` all return
`ValidationException: Unsupported data source type for MANAGED knowledge base
type`. Everything nests inside a `MANAGED_KNOWLEDGE_BASE_CONNECTOR` envelope,
with the real type in the required, document-typed `connectorParameters`:

```python
dataSourceConfiguration = {
    "type": "MANAGED_KNOWLEDGE_BASE_CONNECTOR",
    "managedKnowledgeBaseConnectorConfiguration": {
        "connectorParameters": {"type": "CUSTOM", "version": "1"},
    },
}
```

`CreateKnowledgeBase` itself needs `type: "MANAGED"`, a `roleArn`, and
`managedKnowledgeBaseConfiguration: {}` — that config has **no required
members**, and `storageConfiguration` is omitted entirely. That absence is the
"no vector store to provision" claim, confirmed.

This invalidates the *shape*, not the intent, of the old draft's "uploads →
CUSTOM data source" and "web crawl → WEB connector" decisions. The native
**Google Drive** connector is newly interesting: it may sidestep the AgentCore
vault principal-binding dead-end that currently blocks KB-sync's Drive path,
at the cost of moving token custody into Secrets Manager.

---

## 5. Measured latency (dev-ai probe, us-west-2, 2026-08-11)

Three real MANAGED KBs. No published benchmark for this existed.

| step | measured |
|---|---|
| `CreateKnowledgeBase` → ACTIVE | **84–97 s** (n=3) |
| 1st ingest into fresh KB, 445 B | **71.4 s** → retrievable 74.8 s |
| 1st ingest into fresh KB, 129 B *(different KB)* | **60.7 s** → retrievable 61.5 s |
| 2nd ingest, warm KB, **51.5 KB** | **4.4 s** → retrievable 5.2 s |
| 3rd ingest, warm KB, 111 B | **6.0 s** → retrievable 8.9 s |
| INDEXED → actually retrievable | **3.4 s** |
| `Retrieve` steady state (n=12) | **p50 672 ms**, min 391, max 847 |

**The dominant effect is a one-time per-KB warm-up of ~60–70 s on the first
ingestion. Subsequent ingests are ~4–6 s and barely size-dependent** (a 51.5 KB
document indexed *faster* than a 445-byte one into a cold KB).

Corrections this forces:
- Creation is 84–97 s, **not** the "2–5 min" in the earlier draft.
- AWS's documented *"few minutes for embeddings to become available"* does **not**
  apply to Managed KB — measured 3.4 s.
- Retrieve p50 672 ms is ~2× the 340 ms figure circulating in blogs (which was a
  classic KB on S3 Vectors). Budget it as real added time-to-first-token.
- `Retrieve` returned `score: 1.0` on an exact hit ⇒ **relevance, higher = better**.
  Legacy code assumes cosine *distance*.

**Untested, and it matters:** whether a KB goes cold again after idleness. If it
does, the dormant tier in §7 pays the warm-up on wake.

---

## 6. Recommended topology

### 6.1 Three levels, three jobs

| Level | Limit | Use it for |
|---|---|---|
| Bedrock KB | 10,000/region (adjustable) | the **shareable knowledge unit** — what a user calls "a knowledge base" |
| Data source | 200/KB | ingestion channels into it: uploads, web crawl, Drive, SharePoint |
| Metadata filter | 5 filters × 5 groups, 1 nesting level | sub-scoping *within* a KB (department, doc type, date) |

### 6.2 Decision: one Managed KB per user-visible knowledge base; agents bind 0..N

Not KB-per-agent, and not one shared KB with an `agent_id` filter.

**Against KB-per-agent** (the old draft's choice, reasoned from classic quotas):
at $5/GB-mo, binding one corpus to three agents costs 3×. Under S3 Vectors at
$0.06/GB duplication was nearly free; it no longer is. It also fights the Agent
Designer model already stubbed at `bindable_catalog.py:72`,
`binding_validation.py:166`, `compat.py:42`.

**Against one shared KB + `agent_id` filter** (today's model):
- forfeits the per-KB Retrieve throughput isolation that is the main quota win
- the 10 TB per-KB cap becomes a global ceiling
- AWS's [June 2026 architecture guidance](https://aws.amazon.com/blogs/architecture/secure-multi-tenant-rag-with-amazon-bedrock-and-verified-permissions/)
  is explicit: metadata filtering is *"filter-level (logical) isolation, not
  IAM-enforced (infrastructure) isolation"*, and recommends a dedicated KB where
  a compliance boundary exists between organizations
- Managed KB **does not support `startsWith` or `stringContains`**. One blog
  claims unsupported operators are *silently ignored*; unverified, but a silently
  dropped tenant filter fails **open**. Keep any boundary on `equals`/`in`, or
  better, on the KB itself

**Fan-out mechanics.** `Retrieve` takes one `knowledgeBaseId`, so an agent bound
to N KBs issues N parallel calls. Cost N × $0.001/turn; latency ≈ one call
(~672 ms) if genuinely parallel. Cross-KB score merging needs our own rerank
(managed reranking is free *within* a KB, not across them) — Cohere Rerank 3.5 at
$2/1k queries ≈ $0.002/turn. **Cap bound KBs per agent at 5**, which bounds both
sprawl and per-turn fan-out cost.

### 6.3 Decision: conversation attachments stay out of Managed KB

A per-conversation KB is arithmetically dead: **84 s create + ~65 s first ingest
≈ 150 s** before the first question is answerable.

Ingesting into an already-warm, long-lived KB is ~5–9 s — better than assumed,
but still too slow to block a chat turn. More importantly the latency is not the
durable objection:

1. **Citations require the `document` block inline.** Offload it and
   `citationsContent` stops working. (Note: citations are not enabled in prod
   today — see `document-context-offload-validation.md`.)
2. **PDFs are dual-encoded** (page image + text layer). A chunked text index
   throws away tables, charts, and layout.
3. **Chunk retrieval structurally cannot serve whole-document tasks** —
   summarize, reformat, "what's the tone".
4. It solves the wrong problem. Attachment sessions are 11% of sessions / 31% of
   spend, and uncached input tokens are *lower* than average. The driver is
   `cacheWrite` at 47.9% — the document re-entering the cacheable prefix on every
   cold turn — not the document's own tokens.

The correct home is `docs/specs/document-context-offload.md`: session-scoped
offload of native blocks, rehydrated on demand, never on the introducing turn.

**Where Managed KB *is* right for attachments:** an explicit, opt-in "add this to
the agent's knowledge base" action. Non-blocking, ~5–9 s into a warm KB, and the
user has asked for persistence.

---

## 7. Lifecycle — bounding growth and reclaiming cost

Because there is no per-KB floor and the 10,000 cap is adjustable, **count
pressure is near zero**. Rank reclamation by `storageGB × idleDays`.

### 7.1 Prevention

- **Never create eagerly.** Lazy `CreateKnowledgeBase` on first successful
  ingest. Most agents never get a document, and `compat.py` already models
  "binding exists, no store yet". Also keeps the 84–97 s `CREATING` window off
  the agent-create path.
- **Tag at create**: `prefix`, DDB `kbId`, `ownerUserId`, `env`. Without tags,
  per-owner cost allocation is impossible and §7.4 has nothing to filter on.
- **Layered caps**, in the `kb_sync/dispatcher.py` style: KBs per user (RBAC-tier
  scoped), GB per user (precedent: 1 GB/user in `files/service.py:157`), GB per
  KB, ≤5 bound KBs per agent.

### 7.2 Tiered reclaim — evict the index, keep the data

Decouple deleting the *expensive* thing from deleting the *user's* thing. The
$5/GB is the Bedrock index; source bytes in S3 are $0.023/GB — 200× cheaper.

| State | Trigger | Action | Cost |
|---|---|---|---|
| `active` | retrieved recently | — | $5/GB |
| `idle` | no retrieve, no bound-agent use, N days | warn owner | $5/GB |
| `dormant` | idle + grace expired | **`DeleteKnowledgeBase`**; keep S3 objects + `DOC#` records | $0.023/GB |
| `purged` | dormant + M days, or explicit | delete S3 + DDB | $0 |

Rehydration from `dormant` is a re-ingest of bytes we still hold — exactly what
the KB-sync worker already does. The expensive step is reversible, so N can be
aggressive (60–90 days) without destroying uploads. ⚠️ Rehydration pays the
~60–70 s cold-ingest penalty (§5), so warn before evicting a KB with a live
binding.

Idleness = `max(own lastRetrievedAt, max(lastUsedAt) over bound agents)`. Never
retrieve-only, or an actively-used agent's KB gets evicted because its queries
didn't match.

### 7.3 Sweeper — reuse kb-sync wholesale

Sparse GSI (attributes written only while eligible, so pinned KBs are invisible
*by physics*) → `rate()` EventBridge → dispatcher → worker. Reuse the throttled
`bump_last_used_at` conditional write (one winner per 24 h) for `lastRetrievedAt`
— never write per retrieve. Reuse `KB_SYNC_DISPATCH_LIMIT`-style per-tick caps so
a bug caps out instead of nuking the fleet.

**Invert the usual flag convention:** ship in **report-only** mode emitting "what
I would have deleted", run it for weeks, then arm. Watch the empty-string
workflow-var case.

### 7.4 Orphans — the gap our existing patterns don't cover

kb-sync's liveness check walks DynamoDB → resource. That can never find resources
DynamoDB doesn't know about, and a crash between `CreateKnowledgeBase` and the
DDB write strands a paying resource invisible to every sweeper.

**Delete saga.** Today's deletes are DDB-first. Invert: write a `PENDING_DELETE`
tombstone → call `DeleteKnowledgeBase` → clear tombstone. A surviving tombstone is
a retryable work item, not a silent leak.

**Daily reconciler.** `ListKnowledgeBases` (paginated, tag-filtered) ⋈ DDB:
- *AWS only* → orphan. **Age-gate on the AWS-reported `createdAt`, not on
  discovery time**, or a reconciler that was down for a week deletes in-flight
  creates. Delete if >24 h old.
- *DDB only* → stale pointer. Mark `vectorState: missing`, re-create on next
  ingest. Do **not** delete the DDB record; the documents are still valid.
- *Both* → refresh stored GB for quota accounting.

Emit EMF alongside the PromptCache metrics: `KbCount`, `KbStorageGB`, `KbIdleGB`,
`KbReclaimedGBPerDay`, `KbOrphansFound`. A sustained non-zero orphan count is the
only signal that the delete saga is leaking.

### 7.5 Exemptions, day one

Marketplace-published KBs exempt while listed; `taken_down` needs an explicit
transition rather than falling through to reclaim. Owner-pinnable retention,
capped per user, admin override.

---

## 8. Cost attribution

**KB bills under `AmazonBedrockAgentCore`.** Anything keyed on `AmazonBedrock`
misses it entirely; anything keyed on the service code alone blends KB into the
Runtime-memory line that the 2026-08 AICC report found is 73% of the AgentCore
bill. **Filter on `usagetype`.**

```bash
aws ce get-cost-and-usage --profile dev-ai --region us-east-1 \
  --time-period Start=2026-08-11,End=2026-08-13 --granularity DAILY \
  --metrics UnblendedCost UsageQuantity \
  --filter '{"Dimensions":{"Key":"SERVICE","Values":["Amazon Bedrock AgentCore"]}}' \
  --group-by Type=DIMENSION,Key=USAGE_TYPE
```

---

## 9. Migration surface

**Target-state displaced:** `RagIngestionLambdaConstruct`; the
`AWS::S3Vectors::*` resources in `RagDataConstruct`;
`backend/Dockerfile.rag-ingestion` (~1.5 GB of baked Docling/PyTorch);
`apis/app_api/documents/ingestion/**`;
`apis/shared/embeddings/bedrock_embeddings.py`;
`apis/shared/assistants/rag_service.py`; the two
`search_assistant_knowledgebase_with_formatting` call sites
(`inference_api/chat/routes.py:1620`, `app_api/assistants/routes.py:524`).

None of these resources may be removed when the managed backend first ships.
They remain required for legacy writes, dual reads, migration, and rollback.
Removal is a separate final phase after every legacy KB has migrated, the
retention window has expired, and managed traffic has completed a no-rollback
observation period.

**Not displaced:** `DOC#` records and provenance fields; the documents bucket;
the tabular bypass (`list_spreadsheets`/`analyze_spreadsheet`, which reads S3
objects directly); the attachment/`files` path.

**Changed, not deleted — KB-sync.** Today the worker re-stages bytes to the same
S3 key to re-fire the S3-event pipeline. With Managed KB there is no such trigger.
⚠️ **`StartIngestionJob` is 0.1 RPS — one job per 10 s, account-wide, not
adjustable.** A 20-policy dispatcher tick would spend 200 s purely on job-start
throttling. **Use direct ingestion into a `CUSTOM` connector instead**, which
bypasses the job quota entirely. That, not latency, is the strongest argument for
direct ingest.

**One-way doors:** embedding type is immutable after `CreateKnowledgeBase`.

**Confounder for any A/B:** hold the 2,000-character
`max_context_length=2000` cap constant. Today only ~500 tokens reach the model;
raise it at the same time as switching and the managed reranker gets credit for
"we finally sent more than 500 tokens". **Test that cap on the current pipeline
first** — it may be the cheapest quality win available and it costs nothing to
try.

---

## 10. Coexistence and migration

The transition must satisfy three constraints simultaneously: legacy KBs keep
working untouched, new KBs are managed, and an owner can move an existing KB
across without downtime or a perceived quality change.

### 10.1 The seam

There are exactly **two** retrieval call sites
(`inference_api/chat/routes.py:1620`, `app_api/assistants/routes.py:524`), both
through `search_assistant_knowledgebase_with_formatting`. That is the strangler
seam. Introduce a backend protocol in `apis/shared/assistants/`:

```python
class KnowledgeBaseBackend(Protocol):
    async def search(self, kb_ref: str, query: str, top_k: int) -> list[Chunk]: ...
    async def ingest(self, kb_ref: str, document_id: str, ...) -> None: ...
    async def delete_document(self, kb_ref: str, document_id: str) -> None: ...
```

Two implementations: `S3VectorsBackend` (today's code, moved verbatim) and
`ManagedKbBackend`. Nothing above the seam learns which one it got.

**Discriminator: `retrievalEngine: "s3vectors" | "managed"`, and absence means
`s3vectors`.** Defaulting by *absence* is the whole backwards-compatibility
story — every existing assistant record reads correctly with **zero backfill
writes**, and a half-completed backfill cannot half-break the fleet. Never write
`"s3vectors"` explicitly to legacy records; let the reader default.

### 10.2 Parity contract — the correctness core

A "smooth" transition means the user perceives *nothing* from the plumbing swap.
Any deliberate quality improvement (agentic retrieval, raising the context cap)
ships **separately and later**, or the two effects are unattributable.

| Property | Rule |
|---|---|
| **Score direction** | ⚠️ Managed returns **relevance** (higher = better; measured `1.0` on an exact hit). S3 Vectors returns cosine **distance** (lower = better). Canonicalize on relevance and convert in the `S3VectorsBackend` adapter. Get this wrong and ranking silently inverts — no error, just bad answers |
| `top_k` | 5 on both |
| Context cap | `max_context_length=2000` characters on both, unchanged |
| Doc-status filter | Keep the `status == "complete"` post-filter on both during parity, even though managed makes it redundant (§10.3) — removing it in the same change confounds the comparison |
| Citations | Built from the same `context_chunks`; excerpt clip stays 500 chars |

**Dual-read pilot.** Before flipping anyone, run both backends on the same query
for opted-in assistants, **serve legacy**, and log the delta (overlap in returned
`document_id`s, rank correlation, latency). That produces real quality evidence
instead of a leap of faith, and it is the same measure-first pattern used for the
prompt-cache and offload work.

### 10.3 Migration = shadow → verify → promote → retain

Never mutate a live KB in place. Build the new one alongside, prove it, then flip
a pointer.

| Phase | What happens | User sees |
|---|---|---|
| `shadow` | Create managed KB + `CUSTOM` connector; re-ingest every `complete` doc | "Upgrading…", KB fully usable on legacy |
| `verify` | Doc-count parity + a canary retrieve per doc-set; optional dual-read sample | same |
| `promote` | Flip `retrievalEngine` → `"managed"` (single conditional write) | brief success note |
| `retain` | Legacy S3 Vectors data kept for a rollback window (30d suggested) | nothing |
| `reclaim` | Delete legacy vectors for that assistant | nothing |

**The source bytes are already in S3** at
`assistants/{assistant_id}/documents/{document_id}/{filename}`, so migration is a
re-ingest, not a re-upload. Users are never asked to re-supply anything.

**Use the `CUSTOM` connector with `customDocumentIdentifier = document_id`**, not
the native S3 connector pointed at the prefix. Three reasons: the `DOC#` record
is the source of truth for what belongs in the KB (an S3-prefix sync would also
ingest docs DDB considers `failed`/`deleting`, creating drift); a 1:1 doc-id
mapping makes delete/update trivial and **retires the whole
`{doc_id}#{chunk_index}` vector-key bookkeeping including `delete_vector_tail`
and the chunk-shrinkage stash**; and direct ingestion bypasses the 0.1 RPS
`StartIngestionJob` quota entirely. The S3 connector remains the better option
for a single very large corpus.

**Timing** (from §5): `85s create + ~65s first ingest + ~5s × (N−1)`. A 20-doc
assistant ≈ **4 min**; 100 docs ≈ **9.5 min**. Background work, never interactive.

**Fleet throughput ceiling:** concurrent `Ingest`+`Delete KnowledgeBaseDocuments`
is **10 per account**. At ~5 s each that is ~2 docs/sec fleet-wide — ~85 min for
10,000 documents. Batching helps, but AWS's own documentation currently
disagrees: the user guide says **25 documents per
`IngestKnowledgeBaseDocuments` call**, while the API reference declares a
maximum array size of **10**. Treat 10 as the safe limit and probe the real API
before sizing the migrator; do not assume 25.

Reuse the kb-sync topology: sparse GSI on migration state → EventBridge →
dispatcher → worker, with a bounded per-tick dispatch so a bug caps out.

### 10.4 Writes and deletes during migration

A user who uploads or deletes mid-migration must not corrupt the result.

- **Uploads:** the S3-event pipeline keeps writing to legacy as today. The
  migration worker snapshots the doc-id set, migrates it, then runs a **catch-up
  pass** for anything created since. Loop until a pass finds nothing new (same
  converge-on-quiet shape as the crawler's 2-consecutive-miss rule). Ingest is
  ~5 s, so convergence is fast. Prefer this over dual-write: one write path stays
  authoritative until promotion.
- **Deletes:** re-read the `DOC#` record immediately before ingesting each doc and
  skip if it is gone or not `complete`. Without this a doc deleted mid-migration
  resurrects in the new KB.
- **Promotion is conditional** on a converged catch-up pass, written with a
  DynamoDB condition expression so two workers cannot both promote.

### 10.5 UX

Principles: the KB is never unusable; the user is never asked to re-upload; the
word "vector" never appears; and the upgrade is reversible.

| State | Surface |
|---|---|
| legacy, no action | **nothing** — no badge, no nag. A KB that works needs no UI |
| upgrade available | Inline card on the KB page: only improvements proven by the §13 benchmark, how long it takes, and "your knowledge base keeps working during the upgrade". Do not promise better image/table understanding before the corpus comparison proves it |
| `shadow`/`verify` | Non-blocking progress ("Upgrading — 12 of 40 documents"), KB fully usable, safe to navigate away |
| `promoted` | One-time dismissible success note; no permanent badge |
| failed | Plain-language reason + Retry; **stays on legacy**, which keeps working. Never a dead end |

Owner/editor-gated via the existing `_require_edit_permission`; viewers never see
the control. Admin surface: a KB list filterable by engine with GB and doc counts,
bulk-migrate, and per-KB retry.

For an org-wide rollout, prefer **opt-in banner → nudge → admin bulk-migrate**
over silent auto-migration. A silent flip that changes answer quality is the one
failure mode users cannot diagnose or undo themselves.

### 10.6 Flags and sequencing

Two **independent** switches, so new-managed can ship without starting a fleet
migration:

| Flag | Controls |
|---|---|
| `MANAGED_KB_NEW_DEFAULT` | new KBs are created managed |
| `MANAGED_KB_MIGRATION_ENABLED` | the background migrator runs at all |

⚠️ **Do not couple the engine swap with the 1:1 → 0..N binding change (F4).** They
are separately risky and a joint failure is unattributable. Land the `KnowledgeBase`
entity record in the engine-swap phase **while preserving the 1:1 binding**, so
the *shape* changes without the *semantics*; allow N bindings only afterwards.

### 10.7 Rollback

Because promotion is a single pointer write and the legacy vectors are retained,
rollback is flipping `retrievalEngine` back — instant, no data movement, both
stores already populated. That is what makes the retention window worth its cost:
S3 Vectors at $0.06/GB alongside managed at $5/GB adds ~1% for 30 days of
insurance. Reclaim legacy data only after the window expires **and** the KB has
served traffic on managed without a rollback.

---

## 11. Open questions

1. **Does a KB go cold again after idleness?** Untestable in one session; decides
   whether §7.2's dormant tier is cheap or expensive to reverse.
2. **Empty-KB billing rounding.** Price List says no floor structurally; the
   dev-ai control KB (`kb-probe-empty-2`, zero data sources) confirms or refutes
   on the next CE refresh.
3. **Do unsupported filter operators fail open?** Blog claim only, and
   security-relevant. Test before any metadata-based boundary.
4. **Native Google Drive connector vs our AgentCore-Identity adapter** — could
   retire the vault principal-binding blocker, but moves token custody.
5. **Managed parser vs Docling** on our actual corpus (tables, scanned PDFs). No
   quality comparison has been run; §9's storage delta is only justified if the
   managed parser plus reranking measurably beats what we have.

## 12. Probe resources

Live in dev-ai until torn down: KBs `kb-probe-empty-1`/`VZKNLS9T1F`,
`kb-probe-empty-2`/`0EKHSBWBOA` (zero data sources — the empty control),
`kb-probe-loaded`/`DAK4HL3JU7`; IAM role `kb-billing-probe-role`. Keep until the
§11 question 2 CE read, then delete all four.

---

## 13. Required pre-build benchmark — current vs managed, 1:1

The three small API probes in §5 answer service-shape questions, not whether a
replacement improves this product. Before building a product vertical slice,
run one disposable comparison harness against **the current pipeline and a
temporary Managed KB with the same documents, questions, model, and context
cap**. This is a decision gate, not production code.

### 13.1 Minimal scope

One script with two adapters is enough:

| Adapter | Path |
|---|---|
| `current` | Create a clearly named temporary assistant in dev through the existing service layer → create normal `DOC#` rows → PUT to the existing documents bucket → let the existing S3-event/Docling/Titan pipeline run → poll `DOC#` → query S3 Vectors → delete the temporary assistant and its documents |
| `managed` | Create a temporary MANAGED KB using `kb-billing-probe-role` → create one `CUSTOM` connector → direct-ingest the same S3 objects → poll document state and then a canary `Retrieve` → delete the data source and KB |

The current adapter creates **test data only** in the dev table; it changes no
schema or configuration and cleans up afterward. The managed adapter writes no
product DynamoDB data. Both use a run id in every resource name, local result
files, and an explicit cleanup command so an interrupted process is recoverable.

Start with exactly three controlled documents:

1. a small plain-text file;
2. a native layout-heavy PDF with columns/tables/charts;
3. an image-only scanned PDF that requires OCR.

Put a unique canary fact in each document and define three questions with known
answers per file. Keep this small until Managed KB clears the decision gate.

### 13.2 Measurements

For each backend and document, record raw timestamps for:

- upload/direct-ingest start → API accepted;
- start → pipeline reports complete/indexed;
- start → the canary is actually returned by retrieval;
- first retrieval latency and 10 immediate retrievals (p50/p95);
- expected `document_id` present in top 5, its rank, and whether the retrieved
  text contains the expected answer;
- the retrieved chunks themselves for human inspection.

`INDEXED` and retrievable are separate timestamps. Use a fresh KB for each
first-document comparison, then ingest the remaining documents into a warm KB.
This separates one-time KB warm-up from parser/OCR cost. Run at least five
samples per document class before treating a p50/p95 as meaningful.

Add one user-level comparison after raw retrieval: send both result sets through
the same answer model, system prompt, `top_k=5`, and **2,000-character** context
cap. Raising the cap, enabling agentic retrieval, or changing the model is a
separate experiment.

The report is one CSV/Markdown table:

| Backend | File | Complete/indexed | Retrievable | Retrieve p50/p95 | Top-5 hit | Expected answer |
|---|---|---|---|---|---|---|

### 13.3 Idle follow-up, not a scheduler

Do not build a 48-hour harness first. Leave one probe KB alive after the main
run, record its id locally, and rerun retrieval plus one tiny follow-up ingest
the next morning. If that shows a cold penalty, only then expand to controlled
1 h / 6 h / 24 h / 48 h probes using separate KBs so one check cannot warm the
next.

### 13.4 Decision gate

Proceed to a product vertical slice only if the comparison shows:

- no answer-quality regression on plain text;
- a measurable benefit on layout-heavy or OCR documents, or another quality
  gain large enough to justify the storage premium;
- acceptable first-document delay when treated as background work;
- subsequent-ingest improvement over the current warm path;
- acceptable added retrieval latency at p95.

If Managed KB does not clear this gate, keep S3 Vectors and test the current
2,000-character cap independently before taking on a migration.

---

## 14. Implementation-readiness gates

The topology decision is approved for evaluation, not implementation. The
following details must be written into this spec or a linked design before
PR-1. They are blocking because each one otherwise creates a leak, lockout, or
irreversible rollout failure.

### 14.1 Durable ingestion control plane

The browser currently creates an `uploading` `DOC#` row, receives a presigned
S3 PUT, and relies on the bucket's `ObjectCreated` notification. There is no
upload-complete API call. Managed direct ingestion therefore still needs a
durable S3-event consumer (a much smaller replacement Lambda is the simplest
shape) that:

1. resolves the document's logical KB and engine;
2. conditionally provisions/polls the Managed KB and `CUSTOM` data source;
3. calls `IngestKnowledgeBaseDocuments` with a stable client token;
4. polls the document until indexed and actually retrievable;
5. updates `DOC#` to complete/failed with bounded retries and a durable retry
   anchor.

Do not move this work into an app-process `asyncio.ensure_future` task. During
coexistence the same consumer must route legacy documents to the existing
pipeline and managed documents to direct ingest without double-indexing them.

### 14.2 Stable KB identity and concrete data model

Bindings reference a stable **application `kbId`**, never the replaceable AWS
`knowledgeBaseId`. Dormancy/rehydration can create a new AWS id without changing
an Agent binding. Define exact keys, GSIs, conditional transitions, and API
models for at least:

- `kbId`, owner and ACL/visibility;
- `retrievalEngine`, lifecycle/provisioning state, AWS KB id and data-source id;
- embedding/parser configuration (immutable choices included);
- source-byte/storage accounting and `lastRetrievedAt`;
- migration generation, progress, lease, error and rollback timestamps;
- pin/retention/listing exemptions and delete tombstones.

Documents and sync policies are currently children of `AST#{assistant_id}`.
Before 0..N bindings, decide whether they move under `KB#{kbId}` or how a KB
shared by multiple agents owns them. A compatible phase-1 option is
`kbId == assistantId`, an absent KB record meaning virtual legacy S3 Vectors,
and promotion changing the KB record's engine while the binding ref stays put.

### 14.3 Authorization and publication semantics

A shareable KB needs design-time and invocation-time access checks comparable
to Memory Spaces. Define owner/editor/viewer behavior, whether an Agent grants
invoke-through access to its KB, and whether one inaccessible KB blocks the
whole turn. The runtime must resolve access for the invoking user before
retrieval.

Marketplace versions freeze a KB ref, not its changing contents. Decide whether
published agents pin a corpus revision, require re-review after KB changes, or
may bind only immutable/publisher-managed KBs. Exemption from lifecycle cleanup
alone does not close this review bypass.

### 14.4 Provisioning and deletion sagas

Create the DDB KB record first in `provisioning`, then call AWS with an
idempotency token and conditionally attach the returned ids. This prevents two
simultaneous first uploads from creating two KBs and leaves a retry anchor if
the process dies.

Use durable tombstones for whole-KB, data-source, and individual-document
deletes. Do not erase the last DDB record or let TTL remove it until AWS confirms
deletion. Keep the document-status filter during migration and make lookup
failure **fail closed**; the current legacy filter's unfiltered fallback is not
safe for deleting or access-controlled content.

### 14.5 IAM, encryption, audit, and teardown

Define a dedicated Bedrock KB service role with `aws:SourceAccount` and
`aws:SourceArn` confused-deputy guards, least-privilege S3/KMS access, and a
caller `iam:PassRole` grant constrained by `iam:PassedToService`. Separately
scope provisioner/migrator CRUD, direct-ingestion, and inference
`bedrock:Retrieve` permissions. Synchronous boto3 calls from async request paths
must run through `asyncio.to_thread` or an async client.

Managed KBs are runtime-created and are not CloudFormation children.
`scripts/teardown/destroy.sh` must list and delete only resources tagged for the
project/environment **before** deleting their service role and PlatformStack.
The daily reconciler is still required for ordinary crash orphans.

### 14.6 Enforceable cost and quota controls

The existing 1 GB user-files precedent is not a safe default: at the managed
rate, 1 GB for each of 30,000 users is a $150,000/month exposure. Define a lower
role-tier default, per-KB and per-owner byte caps, account-wide budget and KB
count alarms, and an atomic reserve/commit/release flow based on S3 `HEAD` size
rather than the client-reported size. Define whether the owner or invoker pays
retrieval/reranking quota. Cost-allocation tags are delayed reporting, not a
real-time enforcement mechanism; owner tags must be opaque, never email/PII.

### 14.7 Additive deployment choreography

Ship in explicit, reversible phases:

1. additive schema, service role, IAM, worker resources and cleanup support;
2. dual backends dark, with mixed-version compatibility;
3. §13 benchmark and opted-in dual-read pilot, serving legacy;
4. opt-in migration and rollback observation;
5. managed default for new KBs;
6. stop legacy writes after the fleet is migrated;
7. reclaim legacy vectors after the retention window;
8. remove the old Lambda, image/workflow jobs, S3 Vectors resources, env vars,
   IAM grants and tests in a final target-state cleanup.

Backend code must never deploy before the IAM/resources it requires, and
Platform cleanup must never deploy before all running code has stopped using
legacy resources.

### 14.8 Minimum test matrix

Before promotion, cover adapter parity and score direction; managed API stubs;
create/ingest/delete idempotency; crash after AWS create but before DDB update;
DDB-only and AWS-only reconciliation; uploads/deletes during migration;
fail-closed access and document status; published-agent corpus behavior; quota
reservation races; mixed old/new deployment; teardown of tagged dynamic
resources; and CDK IAM assertions. Promotion verification uses an exact source
manifest (`document_id` + content hash/generation), not doc-count parity alone.
