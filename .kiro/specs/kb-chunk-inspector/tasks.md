# KB Chunk Inspector — Tasks

**Status:** Not started · **Requirements:** `requirements.md` · **Design:** `design.md`
**Why now:** it is the tooling half of the task-16.2 decision (`managed-kb-migration`
§5.41). That decision was "guidance, not code" — this is what makes the guidance
possible, because a user cannot act on advice about mangled tables if they cannot see
that their table was mangled.

**Effort:** small–medium. One read endpoint, one facade call, one frontend panel
reusing the existing citation card. No new pipeline, no infra, no legacy dependency.

---

- [ ] 1. Backend read endpoint
  - `GET /assistants/{assistant_id}/documents/{document_id}/chunks` in
    `backend/src/apis/app_api/documents/routes.py`
  - `_require_edit_permission` first, exactly as the sibling document endpoints do;
    a non-owner gets the same 403/404 they already get elsewhere (Req 4)
  - Confirm the `DOC#` row exists and is `complete`; 409 while it is still
    processing, 404 when absent. **A born-managed first upload can be
    `provisioning`** — that is a 409 too, not a 404, and its message should say the
    knowledge base is still being created rather than implying the file is missing
  - Resolve the engine with `resolve_engine_for` and get the backend from the
    resolver; never branch on engine in the response shape (Req 2)
  - _Requirements: 1, 2, 4, 5_

- [ ] 2. Chunk enumeration, honest about completeness
  - One `search(...)` call through the facade with the `document_id` `equals`
    filter, `numberOfResults` at the backend ceiling (Bedrock documents 100)
  - Neutral document-anchored query text (the filename, or the row's leading text)
    purely to give the reranker something — the filter is what scopes the result
  - De-duplicate by content hash: a query-ranked API can repeat a chunk
  - Set `capReached=true` when the count returned equals the ceiling
  - Present as an unordered set (or by page where metadata carries one), never as
    "chunk 1..N" — ranking order is not document order, and implying otherwise is a
    lie the UI would be telling
  - **Do not** compute "the header is missing from chunk 2" server-side. Show what
    came back and let the human judge it; that is the 16.2 decision, not laziness
  - _Requirements: 1, 3, 6_

- [ ] 3. Response contract
  - `{ documentId, fileName, engine, chunks: [{ text, page?, order, score? }],
    complete: bool, returned: N, capReached: bool }`
  - Full chunk text, **not** truncated to the 500-character citation limit — that
    truncation is the whole reason the existing citation trace cannot serve this
  - _Requirements: 1, 2, 3_

- [ ] 4. Frontend panel
  - A "View extracted content" action on the document row, enabled at `complete`
  - Panel lists each chunk's full text, monospace/`pre` so a flattened table's
    damage is actually visible, with page/location when present
  - Header carries the actionable nudge: *"This is what the knowledge base
    extracted. If a table or image looks wrong here, the assistant will answer from
    this — consider reformatting your source."*
  - When `capReached`, say "showing the first N; more may exist" rather than
    implying the document has exactly N chunks (Req 3)
  - Reuse the citation card component; the only new data is longer text plus the
    header
  - _Requirements: 1, 3_

- [ ] 5. Tests
  - Route: owner sees chunks on **both** engines; non-owner 403; non-`complete`
    document 409 (cover `provisioning` and `uploading` separately); `capReached`
    set when the count equals the ceiling
  - Assert the **exact** retrieval filter is `equals` on `document_id`, mirroring
    the retrievability-probe test — `ISOLATION_SAFE_FILTER_OPERATORS` exists
    because a prefix operator over-matches, and `DOC-1` admitting `DOC-10` is a
    cross-document leak, not a display bug
  - Fake backend modelling `search(..., retrieval_filter=...)`; copy the one in
    `backend/tests/lambdas/test_kb_ingestion_consumer.py`
  - **Mutation guard:** drop the `document_id` filter and a test that seeds a
    second document's chunks must fail on those chunks appearing
  - _Requirements: 4, 6_

- [ ] 6. Verify against the real §5.41 corpus
  - Open the inspector on the diagram documents that produced §5.41
    (`4-yr-flowchart-v2026.pdf`, `sustainable-farming.pdf` on the dev retain
    assistant) and confirm the flattening is *visible* to a human reader
  - This is the acceptance test for the whole feature: if a user still cannot tell
    from this panel why their per-semester answer was wrong, it has not delivered
    what 16.2 promised
  - _Requirements: 1_

## Out of scope
- Re-parsing or fixing a document — managed owns parsing.
- A per-question retrieval trace — that already ships as citation events.
- Editing or curating chunks.
