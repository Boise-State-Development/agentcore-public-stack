# Bedrock Managed Knowledge Base — evaluation and target topology

**Status:** Evaluation complete. **§13 benchmark executed 2026-08-13/14 — the
§13.4 decision gate is CLEARED (4 of 5 conditions; see §13.5).** Recommendation is
to proceed to a product vertical slice, subject to the four requirements in §13.5.
No product implementation yet.
**Question asked:** Can Bedrock Managed Knowledge Base replace our custom RAG
pipeline? Given usage, pricing and quotas, should a user have *multiple* KBs per
agent, or one KB filtered by agent id? And is it a fit for impromptu document
uploads in a conversation?
**Evidence:** AWS Price List API query + a live 3-KB probe in dev-ai
(490617140655, us-west-2), both 2026-08-11 — plus the **§13 benchmark harness**
(2026-08-13/14, same account): 9 questions × 3 backends with every variable held
constant, 8 capability probes, a context-cap sweep, and live quota probes. Every
number below is either quoted from official AWS docs, extracted from the Price List
API, or measured. Blog and unverified claims are marked as such. Where the original
probe and the benchmark disagree, §5.1 and §11 carry the revised figures.
**Production baseline (2026-08-14):** §3.2/§3.3 and §7.4 add a measurement pass
against **prod** (897729136999, us-west-2) — S3 bucket metrics, CloudWatch Logs
Insights over the live AgentCore runtime, and the `ROLLUP#MONTHLY` /
`rag-assistants` tables. These replace the earlier hypothetical corpus and
retrieval-volume figures, and are the only numbers here drawn from prod rather
than dev-ai.
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

Two line items the first draft of this table omitted, both from the same
pricing page:

| | Managed KB |
|---|---|
| Agentic retrieval | **$4.00/1k `AgenticRetrieve` + $1.00/1k underlying `Retrieve`** ≈ $0.006/turn at 2 underlying calls |
| Gateway invocation | **not included** — standard AgentCore Gateway tool-invocation charges apply if the KB is reached through Gateway |

The $5.00/GB meters **raw source bytes, not post-parse text.** AWS's own example
prices 50 GB as "approximately 100,000 documents including PDFs, presentations,
Word files, and images" — 500 KB/document, which is a file size, not an extracted-text
size. For a PDF-heavy corpus this is the *less* favourable of the two readings, so
the number here is already conservative. Our 20 MB per-file ingest cap
(`documents/ingestion/handler.py`, `MAX_FILE_SIZE_MB`) bounds the worst single
document at **$0.10/mo**.

⇒ **The thing to garbage-collect is gigabytes, not knowledge bases** (§7).

### 3.2 Measured production baseline (2026-08-14)

§3.1's original 10 GB / 50 GB illustrations were hypotheticals. Replaced with
measurement.

| Metric | Measured | Source |
|---|---|---|
| Corpus, all versions + icons | 525 MB / 3,110 objects | `boisestateai-v2-rag-documents-*` CloudWatch `BucketSizeBytes` |
| Corpus growth | ~1.75× in the 12 days to 08/13 | same metric, daily series |
| RAG retrievals, trailing 30 d | **3,886** | Logs Insights, runtime `h4MSyY7YSh` |
| Total chat requests, same window | ~15,150 | `ROLLUP#MONTHLY`, Jul + Aug-to-date |
| **RAG attach rate** | **~26% of turns** | derived |
| Cost per turn | $0.043–0.045 | `ROLLUP#MONTHLY` |
| Active users, peak month | 469 (2026-07) | `ROLLUP#MONTHLY` |

**Cost today, like-for-like:** 0.53 GB × $5.00 = $2.65 storage + 3,886 × $0.001 =
$3.89 retrieval ≈ **$6.54/mo**, against ~$0.09/mo on the current stack. A ~$6.50
delta. With agentic retrieval instead, ≈ $26/mo.

So the original framing — "storage is the whole story, and above 10 GB it needs a
deliberate decision" — is **wrong at present scale in both halves.** Nothing here
is a decision. But it is wrong for a reason worth stating: we are at **~1.5% of
the 30,000-user target**, so every absolute number above is a 1.5% number.

Scaling the measured per-active-user figures (1.13 MB and 8.3 retrievals/user-month):

| Scale | Users | Storage | Retrieval (std) | Retrieval (agentic) | Total/mo |
|---|---|---|---|---|---|
| Today | ~470 | $2.65 | $3.89 | $23 | **$6.50 – $26** |
| 10× | ~4,700 | $27 | $39 | $233 | **$66 – $260** |
| Full adoption | 30,000 | **$169** | **$249** | **$1,494** | **$418 – $1,663** |

Retrieval scales with *users*; storage scales with *corpus*. At full adoption
retrieval overtakes storage, and the standard-vs-agentic choice becomes a
four-figure monthly line item. Frame it in per-turn terms: standard `Retrieve`
adds **2.3%** to a $0.044 turn; agentic adds **~14%**.

**These figures are an expected-value trajectory, not a ceiling — do not read them
as a substitute for §13.5's condition 1.** The $169 above is what 30,000 users cost
*if today's measured 1.13 MB/user behaviour holds*. The existing 1 GB-per-user
allowance **permits** 30,000 GB, i.e. **$150,000/month**. Both numbers are correct
and they answer different questions: this table forecasts likely spend, §13.5
bounds the policy exposure. The per-owner byte cap is required precisely because
nothing but that cap separates the two.

⇒ **Migrate on operational grounds now; adopt agentic retrieval as a separate,
later decision justified on retrieval quality** (see §6.4 for its quota wall).

### 3.3 No seasonality baseline exists yet

`ROLLUP#MONTHLY` begins 2026-03. Every month on record is adoption ramp, not
season:

| Month | Requests | Active users |
|---|---|---|
| 2026-03 | 366 | 19 |
| 2026-04 | 1,855 | 135 |
| 2026-05 | 1,820 | 89 |
| 2026-06 | 2,455 | 134 |
| 2026-07 | 11,941 | 469 |
| 2026-08 (14 d) | 9,178 → ~20,300 pace | 350 |

~55× in five months, with **no summer trough** — August is pacing 1.7× July. Any
"summer is quiet, multiply by N for fall" adjustment is unsupported by data. First
real seasonal signal arrives October 2026; re-baseline the attach rate and
per-user retrieval figures then.

*(Aside, out of scope: 2026-04 and 2026-05 record zero cache read/write tokens
while 03 and 06+ record heavy use. Prompt caching appears to have been off for two
months. Cache savings currently carry ~46% of token cost, so this is worth a
separate look.)*

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

### 5.1 Revised measurements from the §13 benchmark (2026-08-13/14)

The §13 harness re-measured everything above across **7 knowledge base creations**
and three document classes. Where the two disagree, prefer these numbers — the
sample is larger and the documents are realistic rather than a few hundred bytes.

| step | revised measurement |
|---|---|
| `CreateKnowledgeBase` → ACTIVE | **47–124 s** (n=7, median ≈73 s) |
| cold 1st ingest, 1.4 KiB markdown | **68.2–68.3 s** (n=3, spread <0.15 s) |
| **warm** ingest, small markdown, same KB | **2.5 s** → retrievable 3.2 s |
| INDEXED → actually retrievable | **0.75–1.03 s** |
| ingest, 50 KiB native PDF (warm KB) | **68–264 s** |
| ingest, 260 KiB scanned PDF (warm KB) | **37–58 s** |
| `Retrieve` steady state | **p50 662–695 ms, p95 762–800 ms** |
| current pipeline `Retrieve` for comparison | **p50 257 ms, p95 262 ms** |

Refinements to the original conclusions:

- **Creation is more variable than 84–97 s** — the observed range is 47 s to 124 s,
  a 2.6× spread. Size provisioning timeouts for the tail, not the median.
- **The per-KB warm-up is real and remarkably constant**: 68.296 s, 68.232 s and
  68.334 s on three independent knowledge bases for the same small file. That is a
  fixed cost of the *knowledge base*, not of the document.
- **But real documents do add parsing time on top**, so "subsequent ingests are
  ~4–6 s" is too optimistic for anything substantial: the same 50 KiB PDF took
  68 s, 89 s, 99 s and 264 s across four runs. Managed ingestion has a **long
  tail** and must be treated as background work with generous timeouts.
- **INDEXED → retrievable is ~1 s, not 3.4 s.** Still a distinct event that must be
  measured separately, but smaller than first recorded.
- **Added time-to-first-token is +405 ms at p50 and +538 ms at p95** versus the
  current pipeline. (One current-pipeline sample of 5.7 s was the first query of a
  run paying a cold Bedrock embedding call; it is excluded from the percentiles and
  reported separately, rather than being presented as steady state.)

⚠️ **Two API shapes in §4 are wrong for retrieval and must be corrected:**

1. **`vectorSearchConfiguration` is rejected by a MANAGED knowledge base.**
   ```
   ValidationException: Incompatible configuration: vectorSearchConfiguration is
   not supported for managed knowledge bases. Use managedSearchConfiguration instead.
   ```
   `retrievalConfiguration` has two mutually exclusive branches. Managed uses
   `managedSearchConfiguration`, whose members are `numberOfResults`,
   `rerankingModelType` (`CUSTOM`/`MANAGED`/`NONE`), `rerankingConfiguration`, and
   `filter`.
2. **`clientToken` has a 33-character minimum** (max 256, pattern
   `[a-zA-Z0-9](-*[a-zA-Z0-9]){0,256}`). A natural `{id}-{variant}-kb` token is
   31 characters and fails client-side validation. Build tokens, don't interpolate
   them.

Also: creating a knowledge base with `embeddingModelType: CUSTOM` can fail with
*"Unable to verify the specified embedding model"* purely from **IAM eventual
consistency** — the model was confirmed `ACTIVE` and directly invokable at the
time. Treat that message as retryable, or lazy per-KB provisioning (§7.1) will
fail intermittently while pointing at the wrong cause.

**Answered:** whether a KB goes cold again after idleness — see §11 question 1.


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

**This decision does not prevent an attachment and a knowledge base from being
used together — they already compose in a single turn.** Worth stating explicitly,
because "attachments stay out of Managed KB" reads as though the combination is
excluded, and it is not. The two paths are independent and both reach the model:

- the attachment arrives as an inline `document` block, **whole and unchunked**
  (`agents/main_agent/multimodal/prompt_builder.py`);
- knowledge-base material arrives as retrieved chunks prepended by
  `augment_prompt_with_context`;
- `inference_api/chat/routes.py:2234` merges them (`final_message =
  augmented_message`, then attachment guidance), and the comment at :2215 names
  both mechanisms operating on the same prompt.

So "here is my essay, compare it against the exemplars in your knowledge base"
works today with no change. **Ingesting the attachment into the KB would be
strictly worse for that shape of task,** for the reason in objection 3 above —
chunking destroys the whole-document structure being compared — and for a second
reason that is not merely a quality concern: a chat attachment ingested into an
agent's shared KB becomes **retrievable by every other user of that agent.** For a
class-assignment agent, one student's essay leaking into another's retrieval is an
incident, not a papercut. Keeping attachments session-scoped prevents it by
default.

⚠️ **What *does* limit this shape of task is the 2,000-character context cap, and
§13.6's result explicitly does not cover it.** The attachment arrives in full while
the corpus arrives as ~2,000 characters of fragments — measured at 1,987 of 2,000
characters and **2 of 5 chunks** on every managed question. §13.6's finding that the
cap costs nothing holds only for single-fact lookups; compare-and-contrast is
precisely the multi-chunk synthesis case it flags as untested. **Use this scenario
as the question set for that experiment** — it is scorable in a way "summarise" is
not: does the answer cite specific exemplars, or generalise vaguely? Sizing from
§13.6: 8,000 characters is where all five chunks fit, at ~966 extra input tokens
per turn.

A related gap: retrieval returns five *fragments*, never "exemplar #3 in full", so
"compare my structure against a strong essay's structure" may have nothing
structural to compare against. Two candidate mechanisms, both unproven here:
`bedrock:GetDocumentContent` (§11.1, §14.0) to fetch a whole KB document after
using retrieval only to *identify* it — API shape, size limits and cost
unverified; or agentic retrieval, which per §13.6 "does its own retrieval and is
**not** subject to this cap at all", making this class of request a natural trigger
for the §6.5 escalation rather than a configuration flag.

---

### 6.4 Quota headroom — verified against measured scale

Quotas re-read from the [managed KB quota page](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-managed-quotas.html)
on 2026-08-14. Projections use §3.2's measured per-user figures and a 5×
peak-to-average factor over ~9,600 business-hour minutes/month.

| Quota | Default | Adjustable | Today | At 30,000 users | Verdict |
|---|---|---|---|---|---|
| Managed KBs / account / Region | 10,000 | Yes | *unmeasured* | scales with KB-creating users | **measure** |
| Data sources / KB | 200 | **No** | ~4 | ~4 | safe — see below |
| Concurrent ingestion jobs / KB | 50 | **No** | 1 | few | safe |
| Raw data storage / KB | 10 TB | **No** | <1 GB | <100 GB | safe |
| Query input chars / `Retrieve` | 10,000 | **No** | unbounded | unbounded | **fix required** |
| `Retrieve` RPM / KB | 600 (25 RPS burst) | Yes | 0.09 | ~26 avg on a hot shared KB | safe; pre-request for hot KBs |
| `AgenticRetrieveStream` RPM / **account** | **60** | Yes | 0.09 | ~130 peak | **hard wall ~30× current** |

**The one blocker: query length.** `search_assistant_knowledgebase`
(`apis/shared/embeddings/bedrock_embeddings.py`) passes `input_data.message`
straight through, with an inline comment stating *"short string, no token
validation needed."* Titan v2 tolerates ~32,000 characters, so nothing fails
today — the §3.2 zero-result accounting closes exactly (1,743 + 287 = 2,030),
which proves no query is currently failing to embed. Managed KB's cap is
**10,000 characters and non-adjustable**, roughly 3× tighter. A pasted essay
would hard-fail the `Retrieve` call. Truncate or summarise the query at the §10.1
seam before promoting any traffic.

**The one ceiling to plan around: agentic RPM is per *account*, not per KB.** 60
RPM is a single contention point shared by every agent, and it is strikingly low
next to the per-KB `Retrieve` allowance of 600 — read that as AWS signalling that
agentic retrieval is a heavy operation. Combined with its 6× price (§3.2), agentic
retrieval should be **selective and opt-in, never the default path.** Headroom is
roughly 30× current volume; request an increase before full adoption, not after.

**Why 200 data sources/KB is safe despite being non-adjustable:** a data source is
an *ingestion channel* (an S3 prefix, a crawl root, a Drive folder), not a
document. One S3 data source carries unbounded documents. This only becomes a wall
if we ever model document-per-data-source — which §6.1 already rules out, and
which must stay ruled out because the limit cannot be raised.

**Bulk upload needs no queueing of our own.** An ingestion job syncs a whole data
source, so 100 simultaneous uploads is one job, not 100. The per-KB limit of 50
concurrent jobs is per KB, so parallel syncs across many KBs don't contend.
*Unverified:* whether an account-level ingestion-concurrency limit exists — the
quota page lists none. Probe this during the §10.3 shadow phase with a
many-KB backfill before assuming a large migration can run wide.

**KB count is the unmeasured risk.** At 30,000 users, KB count depends entirely on
what fraction create one. 5% → 1,500 (fine). 30% → 9,000 (at the default cap).
It is adjustable, but capacity requests take lead time. Establish the current
number and the per-user creation rate:

```bash
aws dynamodb scan --table-name boisestateai-v2-rag-assistants \
  --filter-expression "begins_with(SK, :d)" \
  --expression-attribute-values '{":d":{"S":"DOC#"}}' \
  --projection-expression "PK" --output json \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); pks={i["PK"]["S"] for i in d["Items"]}; print(f"{len(pks)} KBs, {len(d[\"Items\"])} documents")'
```

Track that ratio against `activeUsers` monthly; it is the leading indicator for
both the KB cap and the storage curve.

---

### 6.5 Proposed: agentic retrieval as a user-triggered escalation

Rather than a per-agent config flag, expose agentic retrieval as an explicit
**per-answer escalation**. The cheap `Retrieve` path runs by default; when an
answer reads as thin or incomplete, the user re-drives that turn with a deeper
search via a single control on the message.

**This dissolves §6.4's quota wall.** At full adoption the standard path projects
to ~25.9 RPM average / ~130 RPM peak. If escalation is opt-in, only the escalated
fraction consumes the 60 RPM account budget:

| Escalation rate | Agentic peak RPM at 30,000 users | Against the 60 RPM cap |
|---|---|---|
| 10% | ~13 | comfortable |
| 30% | ~39 | comfortable |
| 100% (default-on) | ~130 | **exceeds** |

The constraint stops being "30× current volume" and becomes "keep escalation under
roughly a third of turns" — which a deliberate user action will trivially satisfy.

**It also reprices the feature.** An escalation regenerates the whole turn, so it
costs ~$0.044 (LLM) + ~$0.006 (agentic retrieval) ≈ **$0.05**, of which the
retrieval meter is only ~12%. The $4/1k headline stops being the thing to reason
about; the escalation is really "pay for one more turn," which is a far easier
budget conversation and already flows through the existing quota tiers.

**The best side effect is the data.** Every escalation is a human-labelled
*"cheap retrieval was insufficient here"* example, complete with the query and the
KB. That is the retrieval-quality dataset we do not currently collect — today the
signal is discarded. Log the pair regardless of whether the escalation succeeds.

**Preconditions — do not ship this before them:**

1. **Understand the 45% no-vector rate first.** 1,743 of 3,886 retrievals found no
   vectors at all (§3.2). Agentic retrieval over a corpus that lacks the content
   returns nothing either — the user pays 6×, waits longer, and receives the same
   answer. The control would read as broken through no fault of its own. *(The
   separate 7% emptied by the doc-status filter is **correct** behaviour over
   deleted and failed documents — see §7.4 — and needs no fix for this feature.)*
2. **Gate the control on retrieval having found something.** Where the KB
   genuinely had no vectors, the honest affordance is *"this agent has no material
   on that topic"*, not *"search harder"*. Escalation is for **thin** results, not
   **absent** ones.
3. **One escalation per turn.** Disable the control after use so it cannot be
   spammed at ~$0.05 a click, and confirm the escalated turn meters through the
   normal quota path.
4. **Measure agentic latency before exposing it.** §5's ~672 ms is for standard
   `Retrieve`. Multi-hop planning plus N retrievals plus regeneration is unmeasured
   and will need a progress affordance — acceptable because the user opted in,
   unacceptable if it looks hung.

**Decision to lock now, so it is not relitigated: escalation stays
human-triggered.** Auto-escalating on low retrieval scores is the obvious next
proposal and it is a trap — it reintroduces default-on agentic (the 130 RPM row
above) and, given the 45%-empty rate, would fire constantly on exactly the queries
it cannot help.

**Set expectations on where it can help.** Agentic retrieval plans multi-hop
queries, so it wins when an answer must combine facts across documents. It does
not fix a corpus that lacks the content, a model that ignored a chunk it was
given, or whole-document tasks such as summarise/reformat (§6.3). Track
escalation → *satisfaction*, not escalation rate alone; a high escalation rate
that does not improve answers means the retrieval problem is upstream.

**Naming:** avoid "search harder" in the UI — it implies the first attempt was
lazy and indicts the default path. Prefer framing it as a different strategy:
"Search more deeply", "Research this further".

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

**Document-level orphans — measured 2026-08-14.** Status distribution across all
1,692 `DOC#` records:

| `status` | Count | Share |
|---|---|---|
| `complete` | 1,492 | 88.2% |
| `deleting` | 101 | 6.0% |
| `failed` | 95 | 5.6% |
| `uploading` | 4 | 0.2% |

200 documents (11.8%) are non-`complete`, and vectors for many of them remain in
the S3 Vectors index. `_filter_vectors_by_document_status` masks them at query
time — 936 retrievals in the trailing 30 days had chunks dropped this way, 287 of
them reduced to zero results. **The filter is behaving correctly.** The defect is
upstream, in two places:

1. **101 stuck `deleting` records mean deleted content is still indexed.** Only a
   query-time filter separates a user-deleted document from retrieval, and that
   filter **fails open**: `rag_service._filter_vectors_by_document_status` catches
   DynamoDB errors and sets `valid_doc_ids = doc_ids`, returning results
   *unfiltered*. A DynamoDB blip therefore serves content from documents users
   deleted. Fail closed — drop chunks whose status cannot be confirmed. Scope note:
   the *inner* per-document handler already fails closed (a single failed `get_item`
   leaves that `doc_id` out of `valid_doc_ids`, dropping its chunks); only the
   *outer* table-level handler fails open, so the window is table unavailability
   rather than ordinary throttling. Low probability, but the consequence is a
   privacy incident rather than a bad answer, and the fix is one line. Separately,
   `documents/services/cleanup_service.py` is evidently not finishing these
   deletes; the same tombstone-saga pattern proposed above for KBs applies at the
   document level.

   *This is the canonical description of the fail-open defect. §11.1 and §14.4
   reference it rather than restate it — keep it that way.*
2. **95 `failed` records are invisible to their owners.** Users believe those
   uploads worked. §10.3 ingests only `complete` docs, so migration will silently
   drop all 95 — correct for the index, wrong for the user. Surface them and offer
   retry before or during migration rather than omitting them quietly. Same for
   any `uploading` record that is stuck rather than in flight.

Migration incidentally resolves the orphaned-vector *exposure* by not carrying
non-`complete` documents across — but only once §10.3's `reclaim` deletes the
legacy vectors. Until then the fail-open path is live, so fix that independently
of the migration schedule.

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

Status after the §13 benchmark (2026-08-14). Raw evidence for every answer is in
the harness findings log; the harness itself is disposable and not committed.

1. **Does a KB go cold again after idleness?** ⏳ **In progress.** A knowledge base
   was deliberately left alive with a recorded warm baseline (2nd-ingest time and
   retrieval p50). Re-checking it after an extended idle period compares the same
   two numbers. Deliberately not a scheduler, per §13.3. If a cold penalty appears,
   §7.2's dormant tier is expensive to reverse and owners must be warned before
   eviction; if not, it is cheap.
2. **Empty-KB billing rounding.** ✅ **Answered: there is no floor, confirmed
   empirically.** Cost Explorer for 2026-08-01 → 2026-08-15, filtered to
   `Amazon Bedrock AgentCore` and grouped by usage type:

   | usagetype | quantity | cost |
   |---|---|---|
   | `USW2-Knowledge-Base:Consumption-based:Storage` | **0.000000406 GB-Mo** | **$0.00000203** |
   | `USW2-Knowledge-Base:Consumption-based:Retrieval` | 32 queries | $0.032 |

   Storage billed a **fractional** GB-month with no minimum rounding unit, across
   an account holding three probe knowledge bases (two with zero data sources)
   since 2026-08-11. §3's structural argument — that AWS models hourly floors in
   this service code for Runtime and deliberately did not for Knowledge Base — now
   has matching billing evidence. **An idle or empty KB is effectively free; the
   cost driver is gigabytes, exactly as §7 assumes.**

   The retrieval line also confirms the rate: 32 queries at $0.032 is $0.001 each.
   No `AgenticRetrieval` usage type had appeared yet at the time of reading, so
   that rate remains Price-List-only rather than invoice-confirmed.

   ⚠️ Separately unresolved: the `RawDataSize` CloudWatch metric — which §7.3 wants
   for storage accounting — returned **0 datapoints** for a knowledge base with one
   successfully indexed document over a 60-minute lookback. Possible causes, none
   confirmed: the corpus was too small (0.0003 GB), the metric may only publish
   after a *sync job* rather than direct ingestion, or it lags by more than an hour.
   The service role already carried the required `cloudwatch:PutMetricData` grant
   scoped to `AWS/Bedrock/KnowledgeBases`, so a missing permission is **not** the
   explanation. Until confirmed for direct ingestion, compute per-owner bytes from
   S3 `HEAD` sizes as §14.6 recommends.
3. **Do unsupported filter operators fail open?** ✅ **Answered: they fail CLOSED.**
   Against a live managed knowledge base, an unfiltered query returned 5 chunks;
   `equals`, `startsWith` and `stringContains` on a key that cannot exist each
   returned **0**. All three operators were *accepted*, not rejected. The blog claim
   of silent-ignore, and therefore of a tenant filter failing open, is **not
   reproduced**. Note this also corrects §6.2 — see below.
4. **Native Google Drive connector vs our AgentCore-Identity adapter** — ❌ **not
   investigated.** Out of scope for the benchmark; still open.
5. **Managed parser vs Docling on our actual corpus.** ✅ **Answered decisively, and
   this is the finding that clears the §13.4 gate.** On a 9-question set with every
   variable held constant, the current pipeline answered **4/9** and managed
   answered **9/9**. By document class: plain text 3/3 both (no regression); native
   layout PDF 1/3 current versus 3/3 managed; scanned image PDF **0/3** current
   versus 3/3 managed.

   Two specifics worth carrying into the design:
   - The current pipeline **discards most of a machine-readable PDF**. For a
     two-page PDF with two-column prose, a five-row table and a chart, Docling
     produced **one chunk** containing only the title and first sentence. The table
     was never extracted, even though its text *is* present in the PDF text layer
     (verified independently with PyMuPDF). This is not an OCR gap; it is a parsing
     gap on documents we can already read.
   - The current pipeline **cannot ingest a scanned PDF at all**:
     `ValueError: Docling produced zero chunks`, surfaced to the user as the generic
     *"Processing failed — please try again or contact support"*. 6 s of Lambda time,
     1.4 GB of 3 GB memory used — a capability gap, not a resource limit.

### 11.1 Corrections to earlier sections, from measurement

- **§6.2 is wrong that Managed KB lacks `startsWith`/`stringContains`.** Both are
  present in `managedSearchConfiguration.filter`, alongside `equals`, `notEquals`,
  `in`, `notIn`, `greaterThan(OrEquals)`, `lessThan(OrEquals)`, `listContains`,
  `andAll` and `orAll` — and they fail closed (question 3).
- **§6.2's cross-KB fan-out economics are wrong.** It assumes N parallel `Retrieve`
  calls plus our own reranker at ~$0.002/turn because "managed reranking is free
  *within* a KB, not across them". `AgenticRetrieveStream` accepts a **`retrievers`
  list** — each entry a `knowledgeBaseId` plus a natural-language `description` —
  and applies managed reranking across all of them in one call. The Cohere line
  item disappears. What replaces it is a quota, not a price: see below.
- ⚠️ **`AgenticRetrieveStream` is limited to 60 requests per minute per ACCOUNT**
  (adjustable). That is roughly one request per second platform-wide. Agentic
  retrieval is the mechanism behind query decomposition and multi-hop reasoning, so
  **this quota must be raised before anything depends on it**, and it cannot be the
  default retrieval path until then. This limit appears nowhere else in this
  document.
- **Hybrid search cannot be toggled.** There is no `overrideSearchType` for managed
  knowledge bases, and the configuration branch that carries it is rejected
  outright. Hybrid is simply how managed retrieval works — so it can be compared
  against today's dense-only pipeline, but not A/B tested against itself.
- **`IngestKnowledgeBaseDocuments` is capped at 10 documents per call, server-side.**
  §10.3 was right to treat 10 as the safe limit. Confirmed by sending 11 with SDK
  validation disabled: *"The number of documents (11) exceeds the maximum allowed
  (10) for MANAGED knowledge base type."* AWS's user-guide claim of 25 does not
  apply to managed knowledge bases.
- **One service role can serve many knowledge bases.** AWS's note that "a policy
  cannot be shared between multiple roles" does not prevent role reuse — a second
  knowledge base created against the first one's role reached ACTIVE normally.
  10,000 knowledge bases do not require 10,000 roles.
- **Custom embeddings work and cost nothing extra in time.** Pinning
  `amazon.titan-embed-text-v2:0` (`embeddingModelType: CUSTOM`) produced identical
  cold-ingest time and identical answer quality to the built-in embedding — 9/9
  either way. AWS constrains custom embeddings to **float32 with 1024 dimensions**,
  which is exactly Titan v2's shape. A migration can therefore keep today's
  embedding model for continuity, which matters because the choice is immutable
  after creation.
- **Image extraction is opt-in.** Multimodal parsing requires
  `mediaExtractionConfiguration.imageExtractionConfiguration.imageExtractionStatus
  = ENABLED` on the data source; audio and video have sibling toggles. Left at its
  default, chart and image content is never described and never indexed — a silent
  loss of the capability being paid for. The mechanism is worth knowing: the parser
  runs a vision model and indexes a **generated textual description**, e.g.
  `<analysis> <image_type> Bar Chart / Column Graph </image_type> <title> Figure 1.
  … </title>`.
- **Managed reranking measurably does the work.** With reranking, the five returned
  scores separate sharply (0.89, 0.38, 0.25, 0.21, 0.19); with
  `rerankingModelType: NONE` they are nearly flat (1.00, 0.84, 0.78, 0.77, 0.77) and
  ordering changed on two of three queries. The flat case matters for the context
  cap: when scores are undiscriminating, truncating to the top two chunks is close
  to arbitrary. **The reranker is what makes a small context cap defensible.**
- **ACL-aware retrieval exists and fails closed**, which is *better* than today's
  document-status filter — ours fails open on a table-level DynamoDB error (§7.4).
  AWS is explicit that ACL awareness "is not
  authorization" and does not authenticate users, so app-side authorization is still
  required. Identity is **email only**, with no alias resolution; mismatches fail
  silently.
- **Resource policies are MANAGED-only** and give genuine IAM-enforced sharing for
  `bedrock:Retrieve` and `bedrock:GetDocumentContent` — the infrastructure-level
  isolation §6.2 said metadata filtering could not provide. ⚠️ They attach to the
  **AWS knowledge base ARN**, so any dormancy/rehydration cycle that produces a new
  AWS id silently drops sharing and must re-apply it.
- **`DELETE_UNSUCCESSFUL` is a real terminal state, and the orphan risk in §7.4 is
  already live.** The dev account contains a knowledge base
  (`derrick-rag-test-delete-me`) stuck in `DELETE_UNSUCCESSFUL` since 2025-11-24,
  with the failure naming its own remedy: set the data source's
  `dataDeletionPolicy` to `RETAIN` and retry. Set that policy deliberately at
  `CreateDataSource` time, and never treat "delete call accepted" as "resource
  gone" — deletion is asynchronous and knowledge bases sat in `DELETING` for
  minutes.

### 11.2 Unrelated production bugs found while benchmarking

Both are independent of this decision and worth fixing on their own.

1. **The pipeline cannot ingest `.txt` at all.** The deployed Docling build has no
   plain-text input format: *"File format not allowed: tmp….txt"*. The repo claims
   support in three places (`documents/ingestion/handler.py`'s extension map, and
   `docling_processor.py`'s `SUPPORTED_MIME_TYPES` and
   `DOCLING_SUPPORTED_EXTENSIONS`) and the frontend's
   `file-upload.service.ts` lists `.txt` as allowed. A user can upload one, wait
   56 s, and get *"Processing failed — please try again or contact support"*.
   Cheapest fix: convert `text/plain` to markdown before handing it to Docling
   (markdown *is* supported), or reject the format at upload with a clear message.
2. **Scanned PDFs fail with the same opaque message** (zero chunks, see question 5).
   Whatever happens with Managed KB, the user-facing error should distinguish
   "this format isn't supported" from "something broke".


## 12. Probe resources

Live in dev-ai until torn down: KBs `kb-probe-empty-1`/`VZKNLS9T1F`,
`kb-probe-empty-2`/`0EKHSBWBOA` (zero data sources — the empty control),
`kb-probe-loaded`/`DAK4HL3JU7`; IAM role `kb-billing-probe-role`. Keep until the
§11 question 2 CE read, then delete all four.

**Update 2026-08-14: the §11 question 2 Cost Explorer read is done** (see §11), and
**all four probe resources have been deleted** — knowledge bases `VZKNLS9T1F`,
`0EKHSBWBOA` and `DAK4HL3JU7`, then IAM role `kb-billing-probe-role`. Total
Knowledge-Base storage charge they accrued for the month was **$0.00000203**.

Two operational notes from that teardown, both relevant to §14.5's teardown ordering
and §7.2's reclaim timing:

- **Deletion took 2–6 minutes** per knowledge base and was verified by polling
  `ListKnowledgeBases` until the names disappeared. "Delete call accepted" is not
  "resource gone", and any teardown that assumes otherwise will race.
- **The service role was deleted only after all three knowledge bases were
  confirmed absent.** Deleting it while a knowledge base is still `DELETING` is a
  plausible route into exactly the `DELETE_UNSUCCESSFUL` state described in §12.2.
  A role also cannot be deleted until its inline policies are removed.


### 12.1 Additional resources from the §13 benchmark

- **One knowledge base is deliberately still alive** for the §11 question 1 idle
  test, with a recorded warm baseline (2.533 s ingest, retrieval p50 711 ms). It is
  a paying resource; the harness records its id locally and has an explicit teardown
  command. Delete it once the idle check has been taken.
- Everything else the benchmark created — knowledge bases, data sources, service
  roles, staged S3 objects and `DOC#` rows — was removed and verified absent by
  polling `ListKnowledgeBases` until the names disappeared, rather than trusting
  that the delete call was accepted.

### 12.2 Pre-existing debris found in dev, unrelated to this work

⚠️ A knowledge base named **`derrick-rag-test-delete-me`** (`ZZ13KI12J1`, type
`VECTOR`) has been stuck in **`DELETE_UNSUCCESSFUL`** since 2025-11-24, with a last
update attempt on 2026-08-05. Its failure message names the remedy: *"consider
updating the dataDeletionPolicy of the data source to RETAIN and retry your
request."*

This is a live instance of exactly the orphan class §7.4 describes — a resource
someone tried to delete, whose deletion failed, which no reconciler would ever
notice. It is a classic `VECTOR` knowledge base so it does not bill at the managed
$5.00/GB-month rate, but it implies a vector store still exists somewhere. Worth
cleaning up independently of this evaluation.


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

### 13.5 Gate outcome — CLEARED (2026-08-14)

| Gate condition | Result | Evidence |
|---|---|---|
| no regression on plain text | ✅ **met** | 3/3 both backends |
| measurable benefit on layout/OCR documents | ✅ **met, large** | layout PDF 1/3 → 3/3; scanned PDF 0/3 → 3/3 |
| acceptable first-document delay as background work | ✅ **met** | 68 s cold ingest; 47–124 s KB creation; never interactive |
| subsequent-ingest improvement over the current warm path | ⚠️ **mixed** | comparable on small text (**2.5 s** managed vs 2.1–12.8 s current); much slower on PDFs (37–264 s vs 8–13 s) |
| acceptable added retrieval latency at p95 | ✅ **met** | +538 ms at p95 (257 ms → 762–800 ms) |

**Four conditions met, one mixed.** On a comparable small text file the warm
managed ingest measured **2.533 s** — as fast as the current pipeline, and matching
§5's original "~4–6 s warm" observation. The order-of-magnitude gap only appears on
PDFs, where managed spends 37–264 s and the current pipeline spends 8–13 s. That
comparison flatters the current pipeline for the wrong reason: it is fast on the
layout PDF because it extracted a single title-only chunk, and fast on the scanned
PDF because it gave up. Managed is slower there because it is actually doing the
parsing, OCR and image analysis that produce the 1/3 → 3/3 and 0/3 → 3/3 gains.
Both paths are background work that no user waits on.

**Recommendation: proceed to a product vertical slice.** The overall answer rate
goes from 4/9 to 9/9 with every other variable held constant, and two whole
document classes move from unusable to working. That is the "quality gain large
enough to justify the storage premium" the gate asks for.

Four requirements on proceeding, each grounded in a measurement above:

1. **A per-owner byte cap must land before, not after.** Storage is 35× more
   expensive per GB. The existing 1 GB-per-user precedent would be $150,000/month
   at 30,000 users. This is the only finding here that can cause real financial
   damage.
2. **Do not depend on agentic retrieval until the 60-per-minute account quota is
   raised.** Query decomposition works well — it answered a genuine multi-hop
   question correctly with citations — but at one request per second platform-wide
   it cannot be a default path.
3. **Hold the 2,000-character context cap at its current value.** §9 says hold it
   constant during the swap; the cap experiment (below) shows there is no reason to
   change it in either direction yet.
4. **Clamp the query string before it reaches `Retrieve`.** Managed KB caps query
   input at **10,000 characters and the limit is not adjustable** (§6.4).
   `search_assistant_knowledgebase` currently forwards the raw user message with
   an inline comment asserting no validation is needed; Titan v2's ~32,000-character
   tolerance is why nothing fails today. This is the only finding that produces a
   hard API failure rather than a cost or quality effect, and it is a few lines at
   the §10.1 seam.

### 13.6 Context cap experiment — result

Run separately from the parity comparison, as §9 requires. Retrieval happened once
per question and only the cap varied; token counts are the model's own reported
`usage.inputTokens`.

| Cap | current: correct | current: chunks to model | managed: correct | managed: chunks to model | managed: input tokens |
|---|---|---|---|---|---|
| **2000** (today) | 4/9 | 2 of 2 | 9/9 | **2 of 5** | 550 |
| 4000 | 4/9 | 2 of 2 | 9/9 | 3.8 of 5 | 1047 |
| 8000 | 4/9 | 2 of 2 | 9/9 | **5 of 5** | 1516 |
| 12000 / 20000 | 4/9 | 2 of 2 | 9/9 | 5 of 5 | 1516 |

**No answer changed correctness at any cap, on either backend.**

⚠️ **This corrects §9's suggestion that the cap "may be the cheapest quality win
available".** It is not, for two different reasons:

- **On the current pipeline the cap is not the constraint — the parser is.** Quality
  is flat at 4/9 from 2,000 to 20,000 characters, and chunks reaching the model stay
  at **2 of 2** throughout: the cap never truncates anything, because retrieval only
  ever produced one or two chunks.
- **On managed the cap binds on every single question** — each used 1,987 of 2,000
  characters and sent exactly 2 of 5 chunks — **but quality is already saturated at
  9/9**. Raising it to 8,000 admits all five chunks and costs **+966 input tokens
  per turn** for no measured gain.

**Limitation that must travel with this result:** the corpus asks single-fact
lookup questions, where one good chunk suffices by construction — precisely the case
where extra chunks cannot help. This shows the cap is not costing *these* answers;
it does **not** show the cap is harmless in general. Questions needing multi-chunk
synthesis (summarise, compare across sections, list-everything, multi-document)
could still benefit, and should be tested with a question set built for that. Note
also that agentic retrieval does its own retrieval and is **not** subject to this
cap at all.

Sizing note if it is ever revisited: **8,000 characters** is the point where all
five chunks fit; beyond that nothing changes.


---

## 14. Implementation-readiness gates

### 14.0 What AWS already closes (2026-08-14)

Reviewed against `kb-managed-acl`, `kb-managed-cross-account`,
`kb-managed-observability`, `kb-managed-quotas`, `kb-managed-prereqs` and
`kb-managed-permissions`. Several gates below are smaller than written.

| Gate | Status | Why |
|---|---|---|
| 14.1 durable ingestion control plane | still open | AWS provides nothing for the S3-event consumer. Eased: per-document ingestion logs can be delivered to CloudWatch Logs, S3 or Firehose |
| 14.2 stable KB identity and data model | still open, **plus a new risk** | Resource policies attach to the AWS KB ARN, so replacing an id during dormancy/rehydration silently drops sharing |
| 14.3 authorization and publication | **partially closed** | ACL-aware retrieval exists and fails closed; resource policies give IAM-enforced `Retrieve`/`GetDocumentContent` sharing. But AWS states ACL awareness "is not authorization" and does not authenticate users, so app-side authz remains ours |
| 14.4 provisioning and deletion sagas | still open, eased | `clientToken` on create operations; native `deletionProtectionConfiguration` (status + threshold) on the connector |
| 14.5 IAM and encryption | **closed** | AWS documents the exact confused-deputy trust policy this gate asks for: `aws:SourceAccount` plus `ArnLike AWS:SourceArn` on `knowledge-base/*`, `iam:PassRole` conditioned on `iam:PassedToService`, S3 conditioned on `aws:ResourceAccount`, and KMS via `serverSideEncryptionConfiguration.kmsKeyArn`. Teardown of runtime-created resources remains ours |
| 14.6 cost and quota controls | **partially closed, plus a hard new ceiling** | Quotas confirmed (10,000 KBs adjustable; 200 data sources, 50 concurrent ingestion jobs, 10 TB storage, 10,000 query characters all **not** adjustable; `Retrieve` 600/min per KB). New: `AgenticRetrieveStream` **60/min per account** |
| 14.7 deployment choreography | unchanged | entirely ours |
| 14.8 test matrix | unchanged, **plus three additions** | resource-policy re-application after rehydration; CloudWatch metric-permission presence; ACL fail-closed behaviour |
| §7.3 metrics | **largely closed** | `AWS/Bedrock/KnowledgeBases` publishes `Invocations`, `ClientErrors`, `ServerErrors`, `Throttles`, `TotalIterationCount` and `RawDataSize` (GB per `KnowledgeBaseId`) at no charge. `Invocations` per KB is a cheaper idleness signal than the throttled conditional write §7.3 proposes |

⚠️ Two things to encode rather than discover later:

- **Metric publishing is best effort and permission-gated.** It needs
  `cloudwatch:PutMetricData` scoped to the `AWS/Bedrock/KnowledgeBases` namespace on
  **both** the KB service role (for `Retrieve`) and the *calling identity* (for other
  operations, via a forward access session). Omit it and metrics silently vanish
  while requests keep succeeding. Worth a CDK IAM assertion.
- **`RawDataSize` has not yet been observed for a directly-ingested document** —
  see §11 question 2. Do not build quota enforcement on it until confirmed.

Also: **managed embedding and managed reranking require no Bedrock model access at
all.** Model access is only needed for `embeddingModelType: CUSTOM` or
`rerankingModelType: CUSTOM`.


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
deletion. Keep the document-status filter during migration and make lookup failure
**fail closed** — §7.4 carries the measured defect and the exact code path. It is
not safe for deleting or access-controlled content, and because it is live today it
should be fixed ahead of migration rather than as part of it.

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
