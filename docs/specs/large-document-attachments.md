# Large document attachments: attach by reference, search through a managed KB

**Status:** Tier 1 shipped to dev (PR-0 #1560, PR-1 #1573, PR-2 #1577, dev-validated
2026-10-13). Tier 2: PR-3 (the attachments KB, folded into the PR-1 pipeline, §4E′;
#1580 + #1581) merged and dev-validated 2026-10-13, flag on in dev; PR-4 (`query`
mode + chip, #1583) in review. Decisions recorded in §9 (size limit, ingest source,
page numbers, retention, per-user cap) and D7.
**Motivating gap:** a user cannot attach a document over 4 MB to a conversation.
The SPA rejects it on the client, so it never reaches the server and no metric
anywhere records the demand.
**Related:** `docs/specs/document-context-offload.md` (the digest and
`document_read` this builds on), `docs/specs/conversation-search.md` §4 and §7
(the shared managed KB and isolation pattern this copies),
`docs/specs/bedrock-managed-kb-evaluation.md` (every managed-KB latency, cost and
quality number quoted here).

---

## 1. Problem

Three gates enforce one number, and the user hits the first one:

| Gate | Where | What it does |
|---|---|---|
| SPA | `MAX_FILE_SIZE_BYTES = 4 MB`, [file-upload.service.ts:51](../../frontend/ai.client/src/app/services/file-upload/file-upload.service.ts:51), selected by `maxFileSizeFor()` (:73) | Throws `FileTooLargeError` before any request is made |
| Presign | `FILE_UPLOAD_MAX_SIZE_BYTES`, `max_size_for()`, [files/service.py:204](../../backend/src/apis/app_api/files/service.py:204) | Refuses a presign whose *declared* size is over the limit |
| Turn path | `INLINE_DOCUMENT_MAX_BYTES`, [models.py:164](../../backend/src/apis/shared/files/models.py:164); `_partition_attachments`, [routes.py:1420](../../backend/src/apis/inference_api/chat/routes.py:1420) | Drops an oversized document from the turn and adds "exceed the inline document size limit and were skipped" |

Simply raising the number does not work, for three independent reasons:

1. **Bedrock** rejects a `document` content block whose internal representation
   is over 4.5 MB (`/ConverseStream`). The 4 MB gate exists to give a friendly
   message instead of a mid-stream `ValidationException`.
2. **AgentCore Memory** stores a turn's inline attachments as one base64 event,
   capped at 10 MB, which is about 7 MB raw (`INLINE_ATTACHMENTS_MAX_TOTAL_BYTES`,
   [models.py:190](../../backend/src/apis/shared/files/models.py:190)). A larger turn
   leaves a hole in history.
3. **Cost.** A large document inline would sit in the cacheable prefix for the
   life of the session. That is the exact defect the document-offload epic exists
   to bound. A 300-page PDF at 1,500–3,000 tokens/page also does not fit most
   context windows.

So a large document must never ride inline. The model needs a way to work with
it **without** having it in the prompt.

### A pre-existing gap this change makes worse

The presigned PUT does not sign `Content-Length`
([files/service.py:286](../../backend/src/apis/app_api/files/service.py:286), on
purpose: browsers set it and signing it breaks uploads). `complete_upload`
already calls `head_object` but never compares the stored object's
`ContentLength` to the declared `size_bytes` or the limit. **Today the size cap is
enforced on a number the client declares.** The storage quota is incremented by
the same declared number. Once the limit rises and a large object can be
processed by a Lambda and indexed into a billed KB, that gap matters. PR-0 closes it.

---

## 2. What already exists

Most of this design is reuse:

| Piece | Where | What it gives us |
|---|---|---|
| Per-class upload limits | `maxFileSizeFor()` / `max_size_for()`, PPTX at 25 MB | The pattern for a document class with its own ceiling |
| `DocumentDigest` | [document_digest.py](../../backend/src/apis/shared/files/document_digest.py), scheduled from `complete_upload` | Outline plus a Nova Micro abstract, rendered as `<document-digest name=… upload_id=…>`, ≤1,500 tokens |
| `document_read` | [document_read_tool.py](../../backend/src/agents/builtin_tools/document_read_tool.py), [document_read.py](../../backend/src/apis/shared/files/document_read.py) | List, PDF page-range (native document block, hard cap 20 pages), regex pattern, bounded text, page index. Gated on "session has a document", not on RBAC |
| Document gate | `_document_tools_gate`, `turn_has_document` (PR #1218) | Injects `document_read` on the turn a document is attached, classified by class rather than by presence |
| Managed KB backend | [managed_backend.py](../../backend/src/apis/shared/kb_backend/managed_backend.py) | Direct ingestion from an `s3Location` (`document_payload`), delete by document id, the 10-documents-per-call batching and the account-wide operation semaphore |
| Shared KB with per-user isolation | `conversations` KB: [reserved.py](../../backend/src/apis/shared/kb_backend/reserved.py), [index_search.py](../../backend/src/apis/shared/conversation_search/index_search.py), [conversation-index-construct.ts](../../infrastructure/lib/constructs/conversation-index/conversation-index-construct.ts) | One KB per environment, lazy `provision_managed_kb` saga, S3 event → SQS → consumer with `maxConcurrency: 2`, filter built in one module, architecture test forbidding other `Retrieve` callers |
| Per-owner byte cap | [byte_cap.py](../../backend/src/apis/shared/kb_backend/byte_cap.py) | Atomic `totalBytes` accumulator that cannot be overshot by concurrent writers |
| File key layout | `user-files/{user}/{session}/{upload_id}/{filename}` ([files/service.py:270](../../backend/src/apis/app_api/files/service.py:270)), thumbnail as `_thumb.png` sibling | A per-upload prefix for derived objects |

---

## 3. Options considered

| Option | Verdict | Why |
|---|---|---|
| Raise `INLINE_DOCUMENT_MAX_BYTES` | **Rejected** | Bedrock's 4.5 MB block limit and the Memory event limit both still apply (§1) |
| Split a large PDF into several ≤4 MB inline blocks | **Rejected** | Puts hundreds of thousands of tokens in the cacheable prefix on every turn, and breaks the 7 MB per-turn Memory budget |
| Converse `document` source by `s3Location` | **Not relied on** | Unverified that it lifts the 4.5 MB block limit, and it would still put the whole document in context. One probe can settle it (§9) |
| A managed KB per user or per session | **Rejected** | Creating a KB takes 47–124 s and a cold KB pays a fixed ~68 s first-ingest warm-up. Both would land on a user's first large upload. It would also multiply KB count |
| Managed KB as the *only* way to read the document | **Rejected** | Warm-KB ingest of a 50 KiB PDF measured 68–264 s. A user asks about a document the moment it is attached |
| **Attach by reference + `document_read`** (Tier 1) | **Chosen** | Ready as soon as preparation finishes, no new paid infrastructure, prompt cost bounded by the digest and the read caps |
| **Shared `attachments` managed KB for semantic search** (Tier 2) | **Chosen, gated on Tier 1 and on an eval** | Covers what pattern search cannot: scanned PDFs (eval: 0/3 → 3/3), layout-heavy PDFs (1/3 → 3/3), and fuzzy questions over hundreds of pages |
| "Add to a knowledge base": promote an attachment into an agent's or project's KB | **Follow-up** (§8) | A product choice for documents a user wants beyond one conversation. Reuses the existing upload pipeline |

---

## 4. Design

### Principle

**A large document is attached by reference, never by bytes. The managed KB adds
semantic search on top; it is never the only way to read the document.**

The turn carries a digest and an `upload_id`. The model reads what it needs
through `document_read`, the same way it already does after restore or offload.
This is the document-offload state from the first turn onwards.

### Size classes

| Class | Size | Formats | Delivery |
|---|---|---|---|
| Inline | ≤ `INLINE_DOCUMENT_MAX_BYTES` (4 MB) | everything inline today | Unchanged |
| **Reference** | > 4 MB and ≤ `LARGE_DOCUMENT_MAX_BYTES` (50 MB, §9 D1) | `DOCUMENT_CLASS_FORMATS`: pdf, docx, txt, md, html | Digest + handle; `document_read`; optionally KB search |
| Too large | > `LARGE_DOCUMENT_MAX_BYTES` | — | Refused at the SPA and at presign, as today |

Images, spreadsheets and presentations are out of scope (§7). The rule for the
class is one predicate, `is_reference_document(meta)` in
`apis/shared/files/models.py`. The SPA mirrors only the two thresholds, to choose
an error message and a chip state.

### Lifecycle

```
SPA ──presign (declared ≤ 50 MB, document class)──▶ app-api
SPA ──PUT──▶ S3 user-files/{user}/{session}/{upload_id}/{filename}
SPA ──complete──▶ app-api: verify ContentLength (PR-0) → READY → enqueue "prepare"
                                                           │
                         large-document-prepare (Lambda) ◀─┘
                         ├─ _derived/text.jsonl     per-page / per-paragraph text layer
                         ├─ _derived/pages-0001-0010.pdf …   page shards (PDF only)
                         ├─ digest → FileMetadata.digest
                         └─ row.preparation = ready → enqueue "index" (Tier 2 only)
                                                           │
                         attachment-index (Lambda, maxConcurrency 2) ◀─┘
                         └─ IngestKnowledgeBaseDocuments(s3Location of the original)
                            row.kbIndex = indexed

Turn: _partition_attachments → reference list (no S3 fetch)
      user message += <document-digest … upload_id="…"> or a <document-reference … status="preparing"> stub
      document_read injected (turn_has_document = True)
```

### Components

#### A. Upload limits and size verification (app-api, SPA)

- `max_size_for()` returns `LARGE_DOCUMENT_MAX_BYTES` for a document-class file
  when `LARGE_DOCUMENT_ATTACHMENTS_ENABLED` is on; `maxFileSizeFor()` does the same
  behind `features.largeDocumentAttachments`.
- **`complete_upload` verifies the stored object** (PR-0): compare
  `head_object.ContentLength` with the declared `size_bytes` and with
  `max_size_for()`. On a mismatch or an overage, mark the row `FAILED`, delete the
  object, and do not increment quota. The `head_object` call already exists, so this
  adds no request.
- `complete_upload` must **not** run `_schedule_digest` in-process for a
  reference-class file. Today it reads the whole object into app-api memory to
  build the digest. For a 50 MB file that work moves to the preparation Lambda (C).
  Instead it sends one SQS message, `{user_id, upload_id}`, to the prepare queue.

#### B. Turn path (inference-api)

1. **Partition on metadata before the bytes are fetched.**
   `FileResolver.resolve_files` currently downloads and base64-encodes every
   attachment, and `_partition_attachments` then estimates size from the base64
   ([routes.py:1406](../../backend/src/apis/inference_api/chat/routes.py:1406)). A
   50 MB GET plus 67 MB of base64 on the turn path would be a TTFT regression. The
   resolver reads the row first; a reference-class row returns metadata only, with
   no `get_object` call.
2. `_partition_attachments` gains a fifth list, `reference`. Reference files:
   - count toward `MAX_FILES_PER_MESSAGE`, but contribute **0 bytes** to
     `INLINE_ATTACHMENTS_MAX_TOTAL_BYTES`;
   - set `turn_has_document = True`, so `document_read` is injected;
   - stay in `_attachment_marker_names` ([routes.py:1576](../../backend/src/apis/inference_api/chat/routes.py:1576)),
     so the SPA shows the file on the user's bubble after reload.
3. **What goes into the user message:** the stored digest rendered with its
   handle (`render_digest(..., include_handle=True)`) when `preparation == ready`.
   Otherwise a fixed stub:
   `<document-reference name="…" upload_id="…" size_bytes="…" status="preparing"/>`.
   `status` reads the row's `preparation`: `preparing` (pending, or not yet
   written), `failed`, or `ready` when the row is prepared but has no digest
   (`DOCUMENT_DIGEST_ENABLED=false`). As with the digest, `upload_id` is omitted
   when the turn has no `document_read`. The rendering lives in
   `apis/shared/files/reference_attachments.py`.
   **The turn path never builds a digest.** A lazy digest of a 50 MB file is seconds
   of CPU before the first token.
4. `_build_attachment_guidance` ([routes.py:1602](../../backend/src/apis/inference_api/chat/routes.py:1602))
   gets a reference sentence, in the style of the spreadsheet one:
   *"`policy-manual.pdf` (38 MB) is too large to read in full. It is available
   through `document_read`: use `pattern` to find the relevant pages, then
   `page_range` to read them."* As shipped it is an instruction that names the
   call and the id ("To answer from it, call `document_read` with `upload_id` …"):
   on dev, Haiku 4.5 told the user it saw no file when the tool was merely
   available, and called it only when told to.
5. **Prompt-cache contract.** The digest text is rendered once, at attach time,
   into the persisted user message. Restore reads the persisted text back and
   finds no `document` block to strip, so history stays byte-stable. A digest that
   becomes ready later does **not** rewrite the earlier message; the model gets it
   from `document_read`'s index mode instead.
6. **The direct `files` request path** (bytes in the request body) cannot become a
   reference, because there is no upload row. It keeps today's oversized note.
7. **No Runtime flag is needed for Tier 1.** A reference-class row can only exist if
   app-api's flag admitted it at presign. The turn path branches on the row, not on
   a switch, so no bytes are spent from the Runtime's 2,560-byte environment budget.
   Turning the app-api flag off stops *new* large uploads; files already in a
   conversation keep working.

#### C. Preparation Lambda (`large-document-prepare`)

New Lambda, `backend/src/lambdas/large_document_prepare/`. It reuses
`apis.shared.files` (`extract_outline`, `text_sample`, `build_digest`) and runs on
the same image family as the existing Lambdas.

- **Trigger:** the prepare queue, fed by `complete_upload` (B). A redrive or
  reconciler sweep re-enqueues any row stuck in `preparation = pending`.
- **Work:**
  - `_derived/text.jsonl`: one line per PDF page (`{page, text}`), or per paragraph
    for DOCX and text formats. This is what pattern, text and index modes read.
  - PDF only: `_derived/pages-{start:04d}-{end:04d}.pdf`, shards of
    `LARGE_DOCUMENT_SHARD_PAGES` (proposed 10). Page-range mode fetches only the
    covering shards.
  - The digest, built from the original as `build_digest` does today, stored on
    `FileMetadata.digest`.
  - Row field `preparation: {status, pageCount, shardPages, textBytes, error?}`.
- **Sizing:** pypdfium2 on a 50 MB PDF needs real memory. Start at 3,008 MB with a
  5-minute timeout, and measure on the fixture set (§6) before settling.
- **Fail-open:** a failed preparation leaves `status = failed`. `document_read`
  then falls back to reading the original when it is under the old
  `DOCUMENT_READ_MAX_SOURCE_BYTES`; otherwise it says the document could not be
  prepared. The turn itself never fails.
- **Deletion:** `delete_file` and `delete_session_files` already delete the
  original and `_thumb.png`. They must also delete the `_derived/` prefix.
  Lifecycle expiry at 365 days covers the prefix as well.

#### D. `document_read` over prepared documents

- `read_document` checks `preparation`. When it is `ready`:
  - pattern, text and index modes stream `text.jsonl` and never parse the PDF;
  - page-range mode fetches only the covering shards and slices them with the same
    `import_pages` code. Each call is bounded by shard size, not by document size.
- When there is no preparation (an ordinary inline-class document), behaviour is
  unchanged, including the 8 MB `DOCUMENT_READ_MAX_SOURCE_BYTES` cap.
- **The tool spec does not change in Tier 1.** Its docstring already covers "pages
  of a long document", and the attachment note tells the model the file is
  available. No `toolConfigHash` rotation.

#### E. The `attachments` managed KB (Tier 2)

A copy of the `conversations` KB design, applied to attachments:

- **Reserved id** `attachments` in `RESERVED_KB_IDS`. One KB per environment,
  provisioned lazily by the consumer's first ingest through `provision_managed_kb`.
  The warm-up is paid once per environment, never per user.
- **Document id** `att#{user_id}#{session_id}#{upload_id}`.
  **Attributes:** `user_id`, `session_id`, `upload_id`, `filename`.
- **Ingest source: the original object, by `s3Location`** (§9 D2). The managed KB
  does its own parsing, OCR and layout extraction, which is the reason Tier 2
  exists. We never send it our extracted text. `imageExtractionStatus: ENABLED` is
  already set by the provisioning saga.
- **IAM:** the managed KB service role gets `s3:GetObject` on the user-files
  bucket, scoped to `user-files/*`. Existing managed KBs read only the assistants'
  documents bucket, so this grant is new. The consumer's own role needs ingest and
  delete on the attachments KB, `GetItem`/`UpdateItem` on the user-files table, and
  no S3 read: Bedrock reads the object, not the consumer.
- **Pipeline:**
  - prepare (C) enqueues `index` on success;
  - S3 *Object Deleted* events on `user-files/` originals go through an EventBridge
    rule into the same index queue. The rule excludes sidecar and thumbnail keys
    (`anything-but` wildcards on `*/_derived/*` and `*/_thumb.png`), so deleting a
    500-page PDF queues one message, not ~55; it scopes by bucket rather than by a
    `user-files/` prefix, which EventBridge cannot AND with the exclusion. This needs `eventBridgeEnabled` on the
    user-files bucket, which is an in-place property change. That one rule covers
    user delete, session cascade delete and lifecycle expiry;
  - consumer mapping `BATCH_SIZE 10`, `MAX_CONCURRENCY 2`, for the reason recorded
    in `CONVERSATION_INDEX_ESM`: document quotas are per account and shared with
    agent uploads and the conversations consumer.
- **The consumer is idempotent.** For each upload id it reads the row and the
  object as they are *now*. If the row is READY, reference-class and prepared, and
  the object is present, it ingests (re-ingesting an id replaces the document).
  Otherwise it deletes. Event order and redelivery do not matter.
- **Isolation**, the three layers of `index_search.py`:
  1. one module, `apis/shared/files/attachment_search.py`, builds an
     `andAll(user_id equals, session_id equals)` filter from the caller's
     identity and accepts no filter from its caller;
  2. an architecture test fails the build if any other module calls `Retrieve`
     with the attachments KB id;
  3. a hit is dropped unless its document id and its attributes both name the
     caller and the session, and its file row exists under `USER#{caller}`.

  Scoping is per **session**, which is stricter than `document_read`'s
  `_get_owned_ready_file` (owner only).
- **Bounding storage cost** ($5.00/GB-month of raw source bytes; §9 D4, D5):
  - **Evict after 30 days of inactivity.** A document is *inactive* when neither of
    these has happened for `ATTACHMENT_KB_RETENTION_DAYS` (default 30): a turn in
    its session, or a `document_read` call on it. That is the `idleness.py` rule
    ("idleness is not retrieval"): a conversation in active use whose questions
    don't need the document must not lose its index. The last-use timestamp is
    written conditionally, at most once a day, never per call. A daily reconciler
    deletes inactive documents from the KB, releases their bytes from the cap and
    sets `kbIndex = evicted`. The file, its sidecars and Tier 1 reads are
    unaffected. Without eviction, KB bytes accumulate for the 365-day life of the
    file.
  - **Re-index on demand.** A `query` on an evicted document enqueues it and
    returns "re-indexing; use `pattern` or `page_range` meanwhile". Ingest takes
    minutes (§9 D3), so app-api also enqueues a session's evicted documents when the
    user **opens** that conversation (`GET /sessions/{id}/messages`). By the time the
    user asks a question the index is usually back. That send happens on app-api's
    page load, never on the turn path, so it adds nothing to time-to-first-token.
  - **Per-user cap through `byte_cap`.** Default
    `ATTACHMENT_KB_PER_USER_MAX_BYTES` = 500 MB of *indexed* attachment bytes, the
    same atomic accumulator the managed-KB migration uses per owner, under its own
    record (`AST#attachments`, `OWNER#{user_id}`) so it never shares a budget with
    the user's agents. Ingest reserves bytes; delete and eviction release them. Over
    the cap the document is not indexed (`kbIndex = skipped_cap`), and Tier 1 reads
    still work. At the cap one user's indexed attachments cost at most
    $2.50/month; with 30-day eviction the typical figure is far lower.
- **Row field** `kbIndex: {status: queued|indexing|indexed|failed|skipped_cap|evicted, indexedAt}`,
  written by the consumer and read by the SPA chip.

#### E′. As built in PR-3 (D7)

PR-3 builds §4E without the separate index queue, consumer and reconciler. The
dev PlatformStack had 8 of CloudFormation's 500 resources left after PR-1, and that
pipeline would have cost about 17 (D7). The PR-1 pipeline carries it instead:

| §4E piece | As built |
|---|---|
| Index queue + consumer (`maxConcurrency` 2) | The prepare queue and Lambda. A preparation that ends `ready` ingests in the same invocation. `{"action": "index" \| "check"}` messages re-sync or poll one upload. The prepare event source drops to 2 in flight while the KB is on, which also caps concurrent preparations at 2 |
| EventBridge delete rule on `user-files/` | An S3 *Object Deleted* rule targeting the prepare queue, matching originals only: sidecar and thumbnail keys are excluded in the pattern, and the handler still ignores any key that is not an original (`parse_original_key`). The bucket's EventBridge delivery is set through its own CloudFormation property rather than CDK's notification custom resource, which would cost two more resources |
| Daily reconciler Lambda + schedule | A schedule rule that puts `{"action": "reconcile"}` on the same queue. It walks the KB's documents: it removes orphans and documents no longer eligible, settles statuses the checks missed, and evicts inactive documents |
| Last-use timestamp, written at most once a day | Not written. Inactivity is the session's `lastMessageAt`, which every turn already maintains (a `document_read` call is always inside a turn), or the last (re-)index, whichever is later. Nothing is added to the turn path |
| `byte_cap` record `AST#attachments` / `OWNER#{user_id}` | That record, plus a `BYTES#{user_id}#{upload_id}` marker per document, written in one transaction: reserved once, refused over the cap, released exactly once. `byte_cap`'s own functions are keyed on an assistant's `KB#` record, so they are not reused |
| Ingest status | Ingestion is asynchronous. A delayed `check` message polls `GetKnowledgeBaseDocuments` at 2, 3, 5, 10 and then 15-minute intervals (about 75 minutes in all), then leaves the document to the daily reconcile |

The prepare function's timeout rises from 5 to 10 minutes while the KB is on, and
the queue's visibility timeout follows it at six times. The first ingest in an
environment provisions the KB inside the function: the saga waits up to 300 s for
ACTIVE.

New resources while on: **2** (the two rules).

**PR-3 readout on dev (2026-10-13).** A 43.8 MB, 500-page PDF uploaded through the
SPA was:
- prepared in 7.3 s;
- the first ingest of the environment, which provisioned the KB in about 10 s, not
  the 47–124 s measured earlier;
- `TEXT_INDEXED` about 80 s after submit, and `INDEXED` at 10 min 47 s, the image
  extraction across 500 pages taking the rest.

Retrieve, filtered on `andAll(user_id, session_id)`, returned the right
`_excerpt_page_number` for three of three questions (288, 137, 412), and nothing
with another user's id. Each hit carried `document_id`, `filename`, `session_id`,
`upload_id` and `user_id`. Deleting the file released its bytes in under 5 s, and
the document reached `NOT_FOUND` about 2 min 17 s later; the listing keeps the
tombstone. A manual `reconcile` ran clean. One delete of a large PDF also sends
about 55 *Object Deleted* events for its sidecars, all ignored by the handler; a
follow-up narrows the rule's pattern. Everything else is statements on
existing roles: the prepare role, and the managed-KB service role (read
`user-files/*`).

#### F. Semantic search mode on `document_read` (Tier 2)

- New argument `query: str`. With an `upload_id` it searches that document; without
  one, it searches every indexed document in the session. Returns top-k 5 chunks,
  each capped at 2,000 characters (the existing managed-KB context cap), with each
  chunk's PDF page from `_excerpt_page_number` (verified, §9 D3), plus a hint to
  read those pages with `page_range` at full fidelity. The search finds the place;
  the page read gives the model the page as it looks, including tables and figures.
- A document that is not indexed returns its `kbIndex` status and the suggestion to
  use `pattern`, rather than an error.
- **Prompt-cache handling:** adding `query` changes the tool spec once. The argument
  is present when the *deployment* has the attachments KB
  (`ATTACHMENT_KB_ENABLED` on the Runtime: one short variable, about 25 bytes of the
  environment budget). That is constant for a deployment, never per session, so it
  causes one rewrite per live session at deploy time and nothing after. It is never
  present or absent depending on whether *this* session has an indexed document;
  that is the per-session flapping PR #1218 removed.
- **TTFT:** `Retrieve` runs only when the model calls the tool (p50 about 670 ms).
  There is no search-ahead before the first token.

#### G. SPA

- `maxFileSizeFor()` admits document-class files up to the new limit behind
  `features.largeDocumentAttachments`. The over-limit error names the real ceiling.
- Chip states for a reference file: *Large document, read on demand* while
  preparing and after. With Tier 2: *Indexing…*, then *Searchable*, read from
  `kbIndex` on the file row. How the SPA refreshes it (poll while indexing, or
  refresh on focus) is a PR-4 detail.
- Upload progress already exists. A 50 MB PUT goes straight to S3, so no API body
  limit applies.
- The chip is classified on size and MIME type alone, the two thresholds
  `is_reference_document` uses, so it needs no request and shows from the moment
  the upload finishes. The composer card reads "38 MB · Read on demand", with the
  full copy as its tooltip and accessible text; the message card shows the full
  chip.
- **Thumbnails.** A reference-class PDF's card thumbnail is rendered from its
  first page shard, never from the original, which would put up to 50 MB in
  app-api's memory per card. Until preparation is ready the card keeps its
  skeleton.

#### F′ and G′. As built in PR-4

| Spec | As built |
|---|---|
| `query` argument, present when the deployment has the KB | A second decorated variant of the tool, chosen by `ATTACHMENT_KB_ENABLED` on the Runtime (spread into its environment only while on, so 0 bytes of the V2 budget when off). Strands builds the spec from the signature and docstring at decoration time, so the non-searchable variant is the unchanged original function: its spec hash is pinned in `test_attachment_search.py`. The searchable spec is about 700 characters longer. `query` takes precedence over `page_range` and `pattern` |
| Top-k 5, 2,000 characters a chunk, page from `_excerpt_page_number` | As specified, in `apis/shared/files/attachment_search.py`. A hit also carries its filename from the joined row. With pages, the hint says to read them with `page_range` before answering; without (Word, text), to use `pattern` |
| Isolation, three layers | As specified. Layer 3 reads the row under `USER#{caller}` once per document in the results and requires it READY and in the same session. `tests/architecture/test_attachment_kb_retrieve_boundary.py` also catches `attachment_index.KB_ID` and the literal `"attachments"` |
| A document that is not indexed returns its status | `search_status()` (`attachment_index.py`) maps the row to `off`, `preparing`, `indexing`, `searchable`, `evicted` or `unavailable`. The session-wide search lists every non-searchable document with its status beside any hits. A missing KB or a `Retrieve` error degrades to the same "use `pattern`" answer, with no error text from Bedrock |
| A `query` on an evicted document enqueues it | **Not built.** Opening a conversation already re-queues its evicted documents (PR-3), and every SPA turn happens in a conversation the user opened, so the Runtime would need the queue URL in its environment and `sqs:SendMessage` for a case that only arises off the SPA (API keys, scheduled runs). The tool says the document is re-indexed when the conversation is opened |
| SPA refresh: poll or refresh on focus | Poll. `GET /files/{id}/search-status` (one row read, no Bedrock call), shared by the composer and message cards through `AttachmentSearchStatusService`: one poll per upload, every 10, 15, 20, then 30 s while the status is `preparing`, `indexing` or `evicted`, stopping when it settles, on an error, or after 30 minutes. The chip is *Read on demand* until the first answer and for `off` / `unavailable` |
| When a document counts as searchable | From Bedrock's `TEXT_INDEXED`, not only `INDEXED`. Measured on dev 2026-10-13 on a 43.8 MB, 500-page PDF: `TEXT_INDEXED` at +80 s, `INDEXED` at +10 m 47 s (image extraction), and with PR-3's check back-off the row only reached `indexed` at about +20 min. `kbIndex.status` keeps meaning the whole document (`indexed` waits for `INDEXED`, as the KB-migration consumer does), but a check or the reconcile that sees `TEXT_INDEXED` stamps `textIndexedAt` on the `indexing` row, and `search_status()` treats that as `searchable`. So the chip and `query` work from the first check, about +2 min. Until `INDEXED`, a query notes per document that images and scanned pages are still being indexed (`images_pending`); `page_range` reads are full-fidelity throughout. A re-index clears the stamp |
| The guidance sentence (§4B.4) | Names search first when the turn's `document_read` has `query`: "first with `query` (a plain-language search, which also covers scanned pages) or `pattern`", from the same flag the tool reads (`_reference_search_available`). With "first with `pattern`" the model opened with `pattern` in every turn of the quality harness and reached scanned pages by paging through images; naming search cut cost per turn by 38% at the same accuracy (§6, second readout). Fixed per deployment, so it never varies between turns of one deployment; off, the sentence is unchanged |
| SPA flag | `features.attachmentKb`, so a deployment without the KB never polls. The status service is only injected while it is on |
| `DocumentRead` gains `mode=query` and `hit_count` | As specified. `hit_count` is a property on query records only |

### Feature flags

| Flag | Side | Kind | Default |
|---|---|---|---|
| `LARGE_DOCUMENT_ATTACHMENTS_ENABLED` / `CDK_LARGE_DOCUMENT_ATTACHMENTS_ENABLED`, `features.largeDocumentAttachments` | app-api + SPA | Feature switch (widens what users can do, costs Lambda and storage) | **Off** (in development) |
| `ATTACHMENT_KB_ENABLED` / `CDK_ATTACHMENT_KB_ENABLED`, `features.attachmentKb` | Prepare Lambda and app-api (PR-3); Runtime and SPA from PR-4 | Feature switch (provisions a billed KB) | **Off** (in development) |

With the CDK flag off, none of the attachments-KB wiring is synthesized (the two
rules, the grants, the bucket's EventBridge delivery), so a deployment that never
opts in gets no rules and no KB. Tier 2 requires
Tier 1. The CDK config refuses `ATTACHMENT_KB_ENABLED` without
`LARGE_DOCUMENT_ATTACHMENTS_ENABLED`.

If the SPA and app-api flags drift, the effect is cosmetic: an SPA that admits
50 MB in front of an app-api that does not gets a presign 413 with the existing
too-large message.

### Failure modes, all fail-open

| Failure | Effect |
|---|---|
| Preparation not finished when the user sends | Stub with `status="preparing"`; `document_read` says it is still preparing; the model tells the user |
| Preparation failed | `document_read` falls back to the original if it is ≤ 8 MB; otherwise a conversational "couldn't be prepared" |
| KB not provisioned, ingest throttled or failed | `kbIndex` shows it; search mode returns status and suggests `pattern`; Tier 1 unaffected |
| Over the per-user KB cap | `skipped_cap`; Tier 1 unaffected |
| Upload's real size differs from the declared size | `FAILED` at `complete_upload`, object deleted (PR-0) |
| KB eviction | Search re-enqueues and returns "re-indexing"; pattern and pages unaffected |

---

## 5. PR sequence

| PR | Scope | User-visible |
|---|---|---|
| **PR-0** | `complete_upload` verifies `ContentLength` against the declared size and limit (D6). Being done as a standalone change; listed here because PR-2 depends on it | No |
| **PR-1** | `is_reference_document`; preparation Lambda + prepare queue + CDK construct; `_derived/` sidecars; `document_read` reads sidecars; `_derived/` deletion. Behind `CDK_LARGE_DOCUMENT_ATTACHMENTS_ENABLED` (construct) | No: nothing is reference-class until PR-2 raises the limit |
| **PR-2** | Raise the limits (SPA, presign); metadata-first resolver; `reference` partition; digest or stub in the user message; guidance sentence; marker names. Flag-gated | Yes, when the flag is on |
| **PR-3** | Attachments KB: reserved id, EventBridge delete rule, KB role grant on `user-files/*`, daily reconcile (30-day eviction, orphans, stuck statuses), per-user cap, `kbIndex` field, re-index on conversation open. Folded into the PR-1 queue and Lambda (§4E′, D7): 2 new resources. Acceptance: one ~45 MB PDF ingested by `s3Location` (D1). Behind `CDK_ATTACHMENT_KB_ENABLED` | No |
| **PR-4** | `document_read` `query` mode; `attachment_search.py` and its architecture test; SPA *Indexing* / *Searchable* chip, polled from `GET /files/{id}/search-status` (§4F′/G′). No new resources | Yes, when the flag is on |
| **PR-5** | Readout (§6) and the decision to promote either flag | — |

PR-1 lands before PR-2 so that the first large file a user can attach is already
readable at bounded cost. Shipping them the other way round would put a 50 MB
original through `document_read`'s whole-object fetch.

---

## 6. How we'll know

**Content-free metrics**, in `AgentCoreStack/Compaction` beside the existing
`DocumentRead` records:

- `AttachmentReference` (count, size bucket, format), recorded per turn on the
  inference side;
- `LargeDocumentPrepared` (ms, pages, outcome);
- `AttachmentKbIndexed` (ms from READY to `indexed`, outcome);
- `DocumentRead` gains `mode=query` and `hit_count`.

**Demand, before PR-2:** the SPA's client-side rejection is invisible today. Count
it with a small client telemetry record beside `POST /telemetry/turn-timing`,
carrying `{class, sizeBucket}` only, so the readout can say how many users this unblocks.

**Quality, before promoting Tier 2:** a small fixture harness of the documents that
motivate it. Suggested set: a 300-page text PDF policy manual, a scanned 40-page
PDF, a layout-heavy brochure and a 15 MB DOCX, each with 5–10 questions that have
known answers. Run each arm on the same model:

1. Tier 1 only (digest, pattern, pages);
2. Tier 1 + `query`.

Ship Tier 2 if arm 2 measurably beats arm 1 on the scanned and layout classes;
§13 of the managed-KB evaluation predicts it will. If it doesn't, Tier 1 is enough
and the paid infrastructure is not justified.

**The harness** is `backend/scripts/large_document_quality_harness.py`
(`large_document_quality/`, test `tests/test_large_document_quality_harness.py`).
Its fixtures are synthetic and deterministic, one per class, each over 4 MB:

| Class | Fixture | What makes it that class |
|---|---|---|
| text | 300-page policy manual, 7.3 MB | A real text layer; two facts wrap across a line, so a literal `pattern` can miss them |
| scanned | 40-page inspection report, 6.8 MB | Image-only pages: our text layer is empty |
| layout | 24-page brochure, 5.3 MB | Tables and a sidebar drawn out of reading order: the text layer lists every label before any value |
| docx | Member handbook, 15.1 MB | Mostly one embedded image; facts in paragraphs |

21 questions, each with an authored answer on a known page, distractor values
planted elsewhere (`forbidden`), and a `literal` / `paraphrase` tag. The fixtures
are seeded into a deployed environment as a synthetic user's uploads, so the real
prepare Lambda and the real knowledge base process them. Then `probe` checks,
free, what each tier can reach: the real `pattern` read against the real
`Retrieve`. `ask` runs one fresh agent turn per (arm, question, sample), with the
production `document_read` and the user message the chat route builds; the arms
differ only in `searchable`. `score` reports per class: majority of k, exact
McNemar, `n` on every row, plus tool calls, `query` use, tokens and dollars per
turn.

**Cost**, per session through `GET /admin/costs/sessions/{id}/calls`: a reference
session's prefix should look like an offloaded-document session (digest ≤ 1,500
tokens), not like an inline one.

---

## 7. Non-goals

- Spreadsheets and presentations over their current limits (25 MB PPTX, 4 MB
  spreadsheets). They have their own tools and their own memory and parsing limits.
- Images over 4 MB.
- Audio and video attachments. Managed KBs can index them, but this spec does not;
  §7a records what a probe found, for a follow-up spec.
- Search across sessions. That is conversation search, or promotion into an agent's
  or project's KB.
- Raising the 4 MB inline limit. Small documents keep full-fidelity inline delivery.
- Search-ahead retrieval before the first token.

## 7a. Audio and video: what a managed KB does with them (probed 2026-10-12)

Managed KBs index audio (.mp3, .wav, .m4a, .flac, .ogg) and video (.mp4, .mov,
.m4v) through the default managed parser (`SMART_PARSING`). Each is a toggle on
the data source: `mediaExtractionConfiguration.audioExtractionConfiguration` and
`videoExtractionConfiguration`, siblings of the image toggle our provisioning
already sets. A separate mode sends raw media to TwelveLabs Marengo Embed 3.0
(`MULTI_MODAL_EMBEDDINGS`). It is chosen at KB creation, needs a custom embedding
model (which forfeits the managed reranker) and a separate S3 staging bucket, and
supports `Retrieve` only.

**Probe:** a 36-second spoken WAV, with two phrases separated by 20 s of silence,
ingested inline through the CUSTOM connector into `kbbench`, with audio extraction
switched on temporarily (the data source config was restored afterwards and the
document deleted).
- **It works on our ingestion path.** INDEXED in **110 s**.
- **Hits come back as text**, `content.type = TEXT`, `_file_type: AUDIO`,
  `_media_type: audio`. Three kinds of chunk: the verbatim transcript; an
  AWS-generated summary with speaker labels (`<spk_0>`); and a chunk that begins
  with a literal `<reasoning>` block from the parser's own model. **Strip that
  block before showing it to a model or a user.**
- **No timestamp field.** Unlike `_excerpt_page_number` for PDFs, nothing says
  where in the recording a hit is, so a result cannot be cited as "at 12:40".
- **No separate processing charge in the Price List.** The `AmazonBedrockAgentCore`
  managed-KB offer has three rows only: storage $5.00/GB-month, `Retrieve` $0.001,
  agentic retrieval $0.004. Storage is billed on raw bytes, so media is the
  expensive case: a 500 MB lecture recording is $2.50/month while indexed.

**What a follow-up needs to decide.** Audio and video would be **KB-only**: there is
no `document_read` equivalent, so Tier 1's "readable immediately" does not apply,
and the file is unusable until indexing finishes (minutes). A 30-day eviction
would make an old recording unsearchable until it is re-indexed. Media files would
also count heavily against the 500 MB per-user cap. Turning the audio and video
toggles on for the `attachments` KB is a one-line provisioning change (and an
`UpdateDataSource` for an existing KB). The product questions are the work.

## 8. Follow-up: "Add to a knowledge base"

A reference attachment, or any attachment, offers *Add to an agent's knowledge
base*. That copies it into the agent's or project's KB through the existing
document upload pipeline (20 MB cap there today). This is explicit, user-owned,
lives as long as the agent does, and needs no attachments KB. It is a product
decision, kept out of this spec's PRs so that Tier 1 does not wait on it.

## 9. Decisions (2026-10-12)

**D1. `LARGE_DOCUMENT_MAX_BYTES` = 50 MB, a product choice rather than an AWS
limit.** An earlier draft cited a 50 MB per-file limit from the knowledge-base
prerequisites page. That page describes classic (customer-managed) KBs. For managed
KBs the limit is a connector setting, `filterConfiguration.maxFileSizeInMegaBytes`,
which **defaults to 500 MB**. Every managed CUSTOM data source in dev
(`conversations`, an agent KB, `kbbench`) shows
`{"maxFileSizeInMegaBytes": "500"}`, filled in by AWS although our provisioning
never sets it (checked 2026-10-12). So the KB is not the binding constraint; the
50 MB ceiling comes from our own side:
- preparation-Lambda memory and time on a large PDF;
- shard and text-layer sizes for `document_read`;
- the per-user storage quota.

Raising it later is a configuration change plus a re-measured preparation run. No
connector or KB change is needed until the ceiling passes 500 MB. PR-3's acceptance
run still ingests a real ~45 MB PDF by `s3Location` before the flag is turned on,
and that run sizes PR-1's Lambda.

**D2. The managed KB ingests the original file; we do not index extracted text.**
Managed KBs parse documents themselves, including OCR for scanned pages, layout,
tables and image descriptions. Sending our own text sidecar instead would throw
away exactly what Tier 2 is for. The one argument for sidecars was cost (storage is
billed on raw source bytes, so a 40 MB scanned PDF is billed as 40 MB). D4 and D5
bound that cost directly instead. The `_derived/` sidecars stay, but only for
`document_read`'s pattern, text and page modes; they are never sent to the KB.

**D3. Search results carry PDF page numbers. Verified on dev 2026-10-12.** Probe:
a 3-page PDF with a distinct phrase on each page, ingested inline as bytes into the
`kbbench` test managed KB. One `Retrieve` per phrase, filtered to the probe
document; the probe document was then deleted. Each top hit carried
`metadata._excerpt_page_number` = 1.0, 2.0 and 3.0 respectively, the correct page
every time. Other fields returned: `_file_type: "PDF"`, `_source_uri` (the custom
document id), `_chunk_id`, `_data_source_type`, `_data_source_id`,
`_language_code`, plus our own inline attributes; `location` is
`{type: CUSTOM, customDocumentLocation: {id}}`. Managed reserved fields use an
underscore prefix. The `x-amz-bedrock-kb-document-page-number` field in the
classic-KB documentation does **not** apply here, so code must read
`_excerpt_page_number`. The value is a float; cast it to an integer.

The same probe recorded ingest latency: **121 s from ingest to INDEXED for a
3-page, 3 KB PDF on a warm KB.** That is in line with the evaluation's 68–264 s for
a 50 KiB PDF, and it confirms that the KB can never be the first way a user reads
their document.

**D4. Retention: evict after 30 days of inactivity, re-index on demand.** As
specified in §4E. Inactivity means no turn in the session and no read of the
document. Re-indexing is triggered by a search or by opening the conversation.

**D5. Per-user cap through `byte_cap`, 500 MB of indexed bytes by default.** As
specified in §4E. Configurable per deployment.

**D6. Uploaded size is verified against the stored object.** This gap (§1) is being
fixed as a standalone change, separately from this spec's PRs, and is a
prerequisite for PR-2.

**D7. Tier 2 rides the Tier 1 pipeline (decided 2026-10-13).** The dev
PlatformStack sat at 492 of 500 resources after PR-1. The §4E pipeline (index queue,
DLQ, consumer, reconciler, roles, log groups, schedule, SSM parameters, alarms) would
have cost about 17, so it would first have needed a budget-freeing change. Instead,
the prepare queue and Lambda carry index, check, delete and reconcile messages (§4E′),
for 2 new resources. Costs: preparations share the 2-in-flight bound the KB quotas
need, and the prepare alarms cover both jobs. If Tier 2's volume ever needs its own
concurrency, splitting it back out is a CDK change plus a handler move. The code
already lives in `apis/shared/files/attachment_index.py`, not in the handler.

### Still open

1. **Converse `s3Location` document source.** Does it accept a document block over
   4.5 MB? It is not on this plan's path either way (§3), but the answer belongs in
   this log. One probe.
2. **Glacier Instant Retrieval at 90 days.** `document_read` on an old reference
   document pays per-GB retrieval fees. Shard reads keep this small; confirm that
   it is negligible.
3. **Ingest time at 50 MB.** Measured in PR-3's acceptance run (D1). If it runs to
   tens of minutes, the *Indexing…* chip copy should say so.

## Sources

- `docs/specs/bedrock-managed-kb-evaluation.md` §3.1 (pricing), §5 and §5.1
  (creation, warm-up and ingest latency), §13.5 (quality by document class)
- `docs/specs/document-context-offload.md` §4A (digest), §4B (`document_read`)
- `docs/specs/conversation-search.md` §4 (consumer shape), §7 (isolation)
