# KB Chunk Inspector — Design

**Status:** Draft · Reads only. Built entirely on the existing retrieval facade.

## 1. The one hard constraint

Bedrock managed knowledge bases have **no "list the chunks of a document" API**.
The only read primitive is `Retrieve`, which is *query-driven* and *rank-bounded*
by `numberOfResults` (`retrieval_configuration` in
`backend/src/apis/shared/kb_backend/managed_backend.py` sets
`managedSearchConfiguration.numberOfResults`). `GetKnowledgeBaseDocuments`/
`ListKnowledgeBaseDocuments` list *documents*, not chunks.

Consequence: we cannot promise a complete, in-order dump of a document's chunks
on the managed engine. We can get "the top-N chunks `Retrieve` returns for this
document, by relevance to a query, filtered to this document_id." That is more
than enough to answer *"did my columns/headers/image survive?"* — you will see
the vision descriptions and the flattened tables — but the spec (Req 3) requires
we present it honestly as "up to N", not "all".

This is why the feature is framed as an **inspector**, not an **exporter**.

## 2. Reuse, don't invent

Everything needed already exists:

- **Facade search**, both engines:
  `async backend.search(kb_ref, query, top_k, retrieval_filter=None) -> List[Chunk]`
  (`protocol.py`; managed + legacy adapters). `Chunk` carries `text`, `metadata`
  (incl. `document_id`, `filename`/`source`), `s3_key`, `relevance`.
- **Per-document isolation filter**, already proven safe and used by the
  retrievability probe: `{"equals": {"key": "document_id", "value": <id>}}`
  (`equals` is in `ISOLATION_SAFE_FILTER_OPERATORS`).
- **Engine resolution**: `resolve_engine_for(assistant_id, record=...)`.
- **Permission gate**: `_require_edit_permission(assistant_id, current_user)` in
  `backend/src/apis/app_api/documents/routes.py`.
- **Citation display shape** on the frontend already renders
  `{documentId, fileName, text}` — the inspector renders the same shape, longer.

## 3. Backend — one new read endpoint

`GET /assistants/{assistant_id}/documents/{document_id}/chunks`
in `backend/src/apis/app_api/documents/routes.py`.

1. `owner_id = await _require_edit_permission(assistant_id, current_user)`.
2. Confirm the `DOC#` row exists and is `complete` (else 409 "still processing"
   / 404).
3. `engine, record = resolve_engine_for(...)`; get the backend via the resolver.
4. Enumerate chunks (see §4), filtered to `document_id`.
5. Return `{ documentId, fileName, engine, chunks: [{ text, page?, order, score? }],
   complete: bool, returned: N, capReached: bool }`.

Read-only, no writes, no new ingestion path (Req 5). Bounded `Retrieve` calls
(Req 6).

## 4. Enumerating chunks (the honest best-effort)

`Retrieve` needs a query and returns top-N by relevance. To approximate "all
chunks of this document":

- **Query text:** use a neutral, document-anchored query — the filename, or the
  document's own leading text — purely to give the reranker *something*; the
  `document_id` `equals` filter is what actually scopes results. Ranking order is
  not meaningful here, so the UI presents chunks as an unordered set (or by page
  when metadata carries it), not as "chunk 1..N".
- **`numberOfResults`:** request the backend maximum (Bedrock's documented ceiling
  is 100 per call). If the count returned equals the ceiling, set
  `capReached=true` and surface Req 3's "up to N / more may exist" note.
- **De-dup** by chunk content hash across calls (a query-ranked API can repeat).
- **Pagination:** only if we later find a reliable way to page distinct chunks;
  v1 ships single-call, capped, honest.

> Design note: because completeness is not guaranteed, do **not** compute
> "missing header in chunk 2" server-side — just show what came back and let the
> human eyeball it (matches the task-16.2 "guidance not code" decision).

Legacy engine: the same facade call works; we own chunking there so results are
closer to complete, but we keep the same "up to N" contract for a uniform shape.

## 5. Frontend

- On the document row / detail (where status already shows), add a **"View
  extracted content"** action, enabled once status is `complete`.
- Opens a panel listing each chunk's full text (monospace/pre for tables),
  page/location when present, and a small header: *"This is what the knowledge
  base extracted. If a table or image looks wrong here, the assistant will answer
  from this — consider reformatting your source."* (the actionable nudge).
- If `capReached`, show the "showing first N; more may exist" line.
- Reuse the existing citation card component; the only new data is longer text
  and the header/nudge.

## 6. Testing

- Route: owner sees chunks (managed + legacy), non-owner 403, non-`complete`
  document 409, filter is `equals` on `document_id` (assert the exact filter —
  mirrors the retrievability-probe test), `capReached` set when N == ceiling.
- Use moto + a fake backend modeling `search(..., retrieval_filter=...)`
  (the ingestion-consumer tests already have this fake to copy).
- Mutation guard: dropping the `document_id` filter must fail a test that seeds a
  second document's chunks and asserts they never appear.

## 7. Effort

Small–medium. One read endpoint + one resolver/facade call + a frontend panel
reusing the citation card. No new pipeline, no infra, no legacy dependency.
