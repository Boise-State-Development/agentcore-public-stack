# KB Chunk Inspector — Requirements

**Status:** Draft · **Related:** `managed-kb-migration` (§5.41 diagram answer quality, task 16.2)

## Problem

The managed backend can extract content a user never sees and cannot verify. A
flowchart or a two-column table is flattened by the vision/parse step at
ingestion, so an answer about "semester 4" or a per-column total is *confidently
wrong* with no signal to the user. The legacy backend failed **silently** (0
chunks, no answer); the managed backend fails **invisibly** (wrong answer, no
trace). We deliberately will not fix the managed parser (it is a managed service,
and this is a self-service platform — see task 16.2's decision). Instead we make
the extraction **visible**, so a user can look at what the knowledge base
actually holds for their document and decide to change their input (convert a
flowchart to a text table, re-export a scanned PDF, etc.).

We already ship a *retrieval trace*: the chunks sent to the LLM for a given
answer are streamed to the UI as `citation` events (assistantId, documentId,
fileName, text — capped at 500 chars) from
`backend/src/apis/inference_api/chat/routes.py`, and this already works for both
engines via the facade. What is missing is an **upload-time, per-document,
full-chunk view** that does not require asking a question and is not truncated to
the retrieved top-k.

## Requirements

### Requirement 1 — Inspect a document's extracted chunks
**User story:** As an assistant owner, I want to see the content the knowledge
base extracted from a document I uploaded, so I can tell whether my file was
parsed usefully before I rely on it.

- WHEN an owner or editor opens a document that has reached `complete`, THEN the
  system SHALL provide the chunks the knowledge base holds for that document,
  each with its full extracted text and its source metadata (document id,
  filename, and page/location when the backend supplies it).
- WHEN a chunk was produced from a non-text element (an image or a diagram), THEN
  its extracted text (the vision model's description) SHALL be shown verbatim, so
  the user sees exactly what the model "read".
- The excerpt SHALL NOT be truncated to the 500-character citation limit; the
  inspector shows the full chunk text.

### Requirement 2 — Engine-agnostic
- WHEN the document belongs to a managed knowledge base, THEN chunks SHALL be
  read through the managed backend; WHEN it belongs to a legacy knowledge base,
  THEN through the legacy backend. The caller SHALL resolve the engine via
  `resolve_engine_for` and never branch on it in the UI.
- The response shape SHALL be identical across engines.

> **AMENDED BY IMPLEMENTATION 2026-09-11 — managed only.** The clause above assumed
> `backend.search(..., retrieval_filter=...)` was part of the protocol. It is not:
> `retrieval_filter` exists **only** on `ManagedKbBackend.search`. The legacy
> adapter's signature is `search(kb_ref, query, top_k)`, it accepts no filter, and it
> ignores `top_k` — it always asks its index for a fixed five results across the
> **whole** knowledge base.
>
> So on legacy there is no way to scope a retrieval to one document. Running it anyway
> would return five whole-knowledge-base chunks, most or all belonging to *other*
> documents, rendered under this document's filename. That is a cross-document leak —
> exactly what Requirement 4 exists to prevent — not a cosmetic defect to tidy later.
>
> Teaching the legacy adapter to filter was rejected on Requirement 5's own grounds:
> the inspector must not depend on the legacy pipeline continuing to exist, and that
> pipeline is being deprecated. New capability there has a negative lifespan.
>
> **As built:** a legacy document returns `available=false` with an owner-facing
> `reason`, as a **200 rather than an error** — the owner asked a reasonable question
> and "your knowledge base is on the classic engine, which cannot show this" is an
> answer. The response shape is identical either way, so the second clause above holds
> and the UI still never branches on engine, which is what this requirement was
> actually protecting.

### Requirement 3 — Honest completeness
- Bedrock managed knowledge bases expose **no chunk-enumeration API**; `Retrieve`
  is query-ranked and bounded by `numberOfResults`. WHERE the full set of chunks
  cannot be guaranteed, the system SHALL label the view as "chunks the knowledge
  base returned for this document (up to N)", and SHALL NOT claim to be a
  complete or ordered dump.
- WHEN more chunks may exist than were returned, THEN the UI SHALL say so rather
  than imply the document has only N chunks.

### Requirement 4 — Isolation and permission
- The chunk query SHALL be filtered to the requested `document_id` using only an
  isolation-safe operator (`equals`) — never a prefix/substring operator, which
  over-matches (a filter for `DOC-1` must not admit `DOC-10`).
- Access SHALL require owner or editor permission on the parent assistant
  (reuse `_require_edit_permission`); a non-owner SHALL receive 403/404 exactly
  as the other document endpoints do.

### Requirement 5 — No new ingestion path, no legacy dependency
- The inspector SHALL be read-only and SHALL reuse the existing retrieval facade
  (`backend.search(...)`). It SHALL NOT introduce a new chunking, parsing, or
  ingestion code path, and SHALL NOT depend on the legacy pipeline continuing to
  exist (so it survives v1 deprecation).

### Requirement 6 — Cost and latency are bounded
- A single inspect request SHALL issue a bounded number of `Retrieve` calls
  (one, plus pagination up to a hard cap), so opening the view cannot fan out
  into an unbounded or expensive scan.

## Out of scope
- Fixing or re-parsing the document (managed owns parsing).
- A per-question retrieval trace — that already exists (citations).
- Editing/curating chunks.
