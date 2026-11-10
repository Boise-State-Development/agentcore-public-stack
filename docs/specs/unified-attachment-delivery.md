# Unified attachment delivery: one planner, decided in tokens, measured by evals

**Status:** Proposed 2026-11-09. Nothing built. Phase 0 (measurement) is the only
phase that should start before the open questions in §11 are answered.
**Motivating gap:** users report that they cannot attach documents that are large
enough, or enough of them, to a conversation. The limits they hit (4 MB per file,
5 files per message, 7 MB per message) are mostly ours, not the model's, and they
are expressed in bytes while the thing that actually runs out is tokens.
**Related:** `docs/specs/large-document-attachments.md` (the by-reference class and
the attachments KB this generalises), `docs/specs/document-context-offload.md`
(digest, `document_read`, offload and rehydration), `docs/specs/drive-folder-attachments.md`
(the manifest pattern), `docs/specs/eval-harness.md` §9 and §15 (the harness the
measurement plan extends), `docs/specs/turn-path-ttft.md` (the budget every change
here is held to), `docs/specs/release-ledger.md` (where the cost and timing numbers
are read from).

File and line references are from `develop` at c802eb667 (2026-11-09).

---

## 1. Problem

### 1.1 The limits users hit, and what causes each

| Limit | Value | Where | Real cause |
|---|---|---|---|
| Per-file inline size | 4 MB | `INLINE_DOCUMENT_MAX_BYTES`, `apis/shared/files/models.py:165`; SPA `MAX_FILE_SIZE_BYTES` | Bedrock Converse's 4.5 MB document cap. AWS documents that it does not apply to PDFs on Claude 4 and later, and it never applies to text sent as text |
| Files per message | 5 | `MAX_FILES_PER_MESSAGE`, `models.py:258`; `_apply_message_file_cap`, `inference_api/chat/routes.py:1804` | Our choice. Converse's five-document cap covers only non-PDF document blocks, and `apis/shared/models/document_cap.py` already projects the overflow to text |
| Combined bytes per message | 7,000,000 | `INLINE_ATTACHMENTS_MAX_TOTAL_BYTES`, `models.py:235` | AgentCore Memory's 10 MB event quota. The persisted user message carries the raw bytes, base64-encoded |
| Per-conversation | none | — | Only the 1 GB per-user storage quota |

So one of the three is a provider constraint, and that one has narrowed since the
limit was set.

### 1.2 Seven delivery paths, selected in three places, by bytes

| Path | File types | Selected by |
|---|---|---|
| Native `document` block | pdf, docx, txt, md, html ≤ 4 MB | Not tabular, not pptx, not reference-class, inside the 7 MB budget |
| Text projection | Non-PDF documents on Converse | Sixth and later non-PDF document; or all of them with `CONVERSE_DOCUMENTS_AS_TEXT_ENABLED` (on in our dev and prod, off by default) |
| Text extraction | Any document, text-only model | Catalog `inputModalities == ["TEXT"]`; 60,000 characters per turn |
| Read on demand | Documents 4–50 MB, folder files, over-budget documents | `is_reference_document`, `models.py:187`; digest or stub in the message, `document_read` for the content |
| Search | Prepared reference documents | `ATTACHMENT_KB_ENABLED`; never offered for over-budget references |
| Code Interpreter tools | csv, xls, xlsx, pptx | `is_tabular_file` / `is_presentation_file`; never inline |
| `image` block | png, jpeg, gif, webp | `content_type`; no downscale, no image-specific limit, dropped rather than referenced when over budget |

The rules live in the SPA (hard-coded mirrors; `file-upload.service.ts:199-205`
states that no endpoint serves them), in app-api admission (`max_size_for`,
`app_api/files/service.py:225`) and on the turn path (`_resolve_turn_attachments`,
`routes.py:2973`). Every selector reads bytes and MIME type. None reads a token
count, a page count or the model's context window.

### 1.3 Inconsistencies this produces today

- `.sql` is admitted as `text/plain` and then dropped inline: `DocumentHandler.SUPPORTED_EXTENSIONS`
  (`agents/main_agent/multimodal/document_handler.py:14`) does not list it. The
  filename still appears in the attached-files marker.
- Spreadsheets are capped at 4 MB on upload while `analyze_spreadsheet` accepts 25 MB.
- `handleApiError` maps any backend "too large" to `FileTooLargeError(0, MAX_FILE_SIZE_BYTES)`,
  so a rejected pptx or large document reads "File too large: 0 B. Maximum: 4 MB"
  (`file-upload.service.ts:990`).
- The composer's count check reads `pendingUploads()` only (`chat-input.component.ts:2877`),
  while `remainingFileSlots` (:710) also counts restored, imported and folder attachments.
- Two token estimators for a document: bytes/4 in `compaction_policy.py:160`,
  pages × 1,500 in `apis/shared/files/document_tokens.py:79`. The first under-counted
  a `document_read` slice by about 3× on dev on 2026-11-07 (22k estimated, ~62k real).
- Non-Bedrock providers get raw document and image blocks: `document_cap` and the
  text projection are installed only for `ModelProvider.BEDROCK` (`agent_factory.py:257`).
- The TUI's MIME map lacks `.sql` and it checks nothing client-side.

### 1.4 What a bigger context window does not fix

A 1M-token window does not make a large inline document free:

- **Cache re-writes.** The 2026-08-03 prod scan found attachment sessions were 11% of
  sessions and 31% of spend; 48% of that cohort's cost was cache writes, because the
  document enters the cacheable prefix and is re-written in full on every cold turn.
- **Compaction ceilings sit far below the window.** On dev, Haiku 5.5 is pinned at a
  50k ceiling and 70k hard ceiling; a 24-page PDF took one turn to 97.8k on 2026-11-07,
  and the document was cut two turns later.
- **Long-context pricing.** Haiku 5.5 costs more above 100,000 prompt tokens.

The budget for what goes in context is therefore a cost and quality decision per
model, not the window size (§5).

## 2. Reference behaviour: the Claude apps

Read from Anthropic's help centre and API documentation on 2026-11-09.

| | Claude apps | This stack today |
|---|---|---|
| Per file | 500 MB | 4 MB inline; 50 MB by reference; 25 MB pptx |
| Count | 20 files per chat; 20 images per message | 5 per message; no per-chat cap |
| PDF ≤ 100 pages | Text and visuals | Native block up to 4 MB, no page rule |
| PDF 101–1,000 pages | Text only | By reference if over 4 MB |
| PDF > 1,000 pages | Rejected | Accepted up to 50 MB |
| Other documents | Text extraction only; embedded images not read | Native block or text |
| xlsx | Requires code execution | Code Interpreter tools |
| Images | 8000×8000 px, 10 MB; downscaled to the model's maximum | 4 MB, sent as uploaded |
| Overflow | Projects switch to retrieval near the context window (no threshold published) | Only documents of 4–50 MB |

What to adopt is the **logic**: the governing unit is tokens in the context window,
the file count is generous, PDFs change mode by page count, and retrieval is the
overflow. What not to adopt yet is the 500 MB figure (§11, Q2).

### 2.1 Provider constraints that remain on Bedrock

| Constraint | Value | Status |
|---|---|---|
| Document and image sources for Claude | `bytes` only; no S3 location, no Files API | Documented by AWS and Anthropic |
| Non-PDF document blocks per request | 5, 4.5 MB each | Documented; avoided entirely by sending text |
| PDF pages per request | 100 (AWS) vs 600 for 1M-context models (Anthropic) | **Conflicting; needs a probe** |
| Request payload | 32 MB (AWS) vs 20 MB (Anthropic's Bedrock page) | **Conflicting; needs a probe** |
| Image | 5 MB base64 on Bedrock; 8000×8000 px; stricter per-image limit above 20 image and PDF blocks | Documented by Anthropic |
| AgentCore Memory event | 10 MB | Ours to avoid (§6.2) |

## 3. Goals and non-goals

**Goals**

1. One function decides how every attached file reaches the model, for every file type
   the platform accepts and every provider.
2. Limits a user can reason about: a file count and a per-file size, with everything
   else handled by the platform choosing a mode, never by rejecting the file.
3. Parity with the Claude apps on count and PDF rules.
4. Every delivery decision is recorded and measurable for speed, cost and answer quality,
   and no limit is raised without a measured readout.

**Non-goals**

- Audio and video.
- Files over 50 MB (§11, Q2).
- Changing Project files or assistant knowledge bases. They keep their own full-context
  and retrieval rules; the planner's fact model (§4.1) is designed so they could adopt
  it later.
- Enabling Bedrock citations.

## 4. Design

### 4.1 File facts, computed once at upload

Every file row gains a small, immutable fact set written by preparation:

| Fact | Meaning | Source |
|---|---|---|
| `class` | `document`, `tabular`, `presentation`, `image` | MIME and extension, one table in `apis/shared/files/` |
| `tokens.text` | Token count of the extracted text layer | Counted at prepare time |
| `tokens.native` | Estimated cost as a native block (PDF: text plus per-page image; image: the vision formula on the downscaled size) | Computed at prepare time |
| `pages` | PDF page count; `None` otherwise | Prepare |
| `hasTextLayer` | False for a scanned PDF | Prepare |
| `units` | `page`, `paragraph` or `line` | Existing sidecar field |

Today only documents over 4 MB are prepared (`_after_upload_ready`, `service.py:618`).
This spec prepares **every** document-class file, so the text layer, the digest and
the facts exist for all of them. A file whose facts are not ready when a turn arrives
falls back to today's byte rules for that turn; the turn never waits for preparation.

One estimator replaces the two in §1.3, and both compaction and offload read
`tokens.native` from the row.

### 4.2 The planner

A pure function in `apis/shared/files/attachment_plan.py`:

```
plan_attachments(files: list[FileFacts], model: ModelFacts, held: ConversationFacts) -> AttachmentPlan
```

- `ModelFacts`: context window, input modalities, provider, the in-context attachment
  budget (§5).
- `ConversationFacts`: attachment tokens already in context for this conversation.
- `AttachmentPlan`: one entry per file with `mode`, `tokens`, and a `reason` code.

It does no I/O. It reads rows the turn path has already resolved, so it adds no read
and no parse before the first token.

### 4.3 The ladder

Each file takes the first mode it qualifies for, in attachment order, against the
remaining budget.

| Mode | What the model receives | Qualifies when |
|---|---|---|
| `native` | PDF `document` block, or `image` block | PDF within the page limit and the budget, on a model with image input; any image |
| `text` | The extracted text, wrapped as `<document>` | Any non-PDF document within the budget; a PDF with a text layer that is over the visual page limit but within the budget; any document on a text-only model |
| `on_demand` | Digest plus `document_read` | Over the remaining budget, or facts not yet ready and over today's byte limits |
| `search` | Digest plus `document_read` with `query` | `on_demand` and indexed in the attachments KB; preferred by guidance once the conversation's on-demand set passes a size the eval sets |
| `tool` | An inventory line; content through the spreadsheet or deck tools | Tabular and presentation files |

Rules that hold across the ladder:

- A non-PDF document is never sent as a native block. Text is what the Claude apps
  send, it removes the 4.5 MB and five-document constraints, and it makes behaviour
  the same on every provider. This promotes the existing text projection from an
  option to the rule.
- A Word table is rendered through `docx_markdown`, never `docx_paragraphs`
  (the wrong-column finding recorded in `CLAUDE.md`).
- A PDF in `text` mode keeps `page_range` available, so a figure can still be read
  natively on demand.
- A scanned PDF over the visual page limit has no usable text layer and goes
  `on_demand`.
- Images are downscaled server-side at upload to the largest size any curated model
  uses, so no image is rejected for size and none exceeds 5 MB encoded.
- A file is never dropped. Today's "skipped" and "send it in a follow-up" outcomes
  become `on_demand`.

### 4.4 Limits a user sees

| Limit | Proposed | Today |
|---|---|---|
| Files per message | 20 | 5 |
| Images per message | 20 | 5 |
| Per file, documents | 50 MB | 4 MB, or 50 MB with the large-document flag |
| Per file, spreadsheets and decks | 25 MB | 4 MB and 25 MB |
| Per file, images | 20 MB before downscale | 4 MB |
| PDF pages | 1,000 | none |
| Per user | Storage quota, unchanged | 1 GB |

Served by `GET /files/limits` on app-api (session-cookie auth), computed from the
same constants the planner and admission use. The SPA and the TUI read it and drop
their mirrors.

## 5. The in-context budget

The planner needs one number per model: how many attachment tokens a conversation
may hold in context.

### 5.1 What governs it today

Nothing names this number, so the compaction ceiling sets it by default.
`CompactionPolicy.resolve` (`agents/main_agent/session/compaction_policy.py`) gives
every model `ceiling = min(0.5 × window, 100k)`, a floor of 25% of the ceiling and a
hard ceiling of `min(0.7 × window, 1.5 × ceiling)`. Every model with a window of 200k
or more therefore gets 100k, and a 1M window adds nothing.

The ceiling is compared with the whole input: `toolConfig`, system prompt, history
and documents together. Four consequences for documents:

- **Room for documents is about 50–70k tokens.** A 24-page PDF put turn 1 at 97.8k
  input on dev (Haiku 5.5, 2026-11-07).
- **A single turn is never cut.** An oversized document goes through at full price on
  the turn it arrives and is cut afterwards. That session reached 159.9k on turn 2
  and was cut to 68.5k at the head of turn 3.
- **Over the ceiling, documents leave.** `_document_offload_reason` returns
  `over_ceiling` when the last input exceeds it, and the live offload replaces every
  unpinned document of 5,000 tokens or more with its digest. A compaction cut that
  passes the document's turn removes it as well, and the summary cannot carry it.
- **The estimate is wrong.** `_block_tokens` counts a document block as bytes/4. The
  same session estimated 22k retained against about 62k measured, and a second forced
  cut followed two turns later. Phase 2 replaces it with the row facts.

The 100k cap rests on a replay of 20 heavy Sonnet 5 sessions (2026-09-15): $73.70 at
a 100k ceiling against $105.30 at 200k, because a prefix is re-written whole after a
5-minute pause. That evidence is cost only, from chat sessions, where history
compresses into a summary. A document does not. It says nothing about answer quality
with a document held or dropped.

### 5.2 A separate budget for documents

Documents get their own number, and the ceiling goes back to governing conversation.

```
document_budget   = min(document_share × context_window, price_tier − chat_ceiling)
chat_ceiling      applies to: last input − held document tokens
hard_ceiling      applies to: last input, unchanged
```

- **`document_budget`** is what the planner spends in `native` and `text` modes across
  the conversation. A file that does not fit goes to `on_demand`.
- **`price_tier`** is the input size above which the model's rate rises, a catalog
  field. A model with no tier omits the term.
- **The chat ceiling stops counting held documents.** Compaction and the
  `over_ceiling` offload trigger subtract the held document tokens (from row facts)
  before comparing. A document inside its budget no longer triggers the cut that
  removes it.
- **The hard ceiling still counts everything.** It is the bound against the window
  and against a turn that grows across tool rounds.
- **The cold-cache offload is unchanged.** After a pause longer than the cache TTL
  the re-write is already being paid, and unpinned documents become digests then, as
  today. So the budget's cost is warm-turn cache reads plus the first write, never a
  repeated cold re-write of the document.
- **A cut that passes a held document's turn** drops it to its digest, as today.
  Carrying held documents across a cut is not proposed: it would enlarge the one
  re-write a cut causes, and `on_demand` already covers the case.
- A catalog field can override the budget per model.

The values are not chosen here. §8.4's ceiling arms measure them, and the budget is
set from that readout (§11, Q3). Until then the planner runs with a budget that
reproduces today's byte rules, and the chat ceiling counts documents as it does now.

## 6. Persistence and the prompt cache

### 6.1 What stays the same

- `text`, `on_demand`, `search` and `tool` content is plain text rendered once into
  the persisted user message and never rewritten, as reference attachments are today.
- Rendering is a pure function of the file's bytes, so the prefix is byte-stable.
- Offload and rehydration are unchanged: a native PDF is replaced by its digest when
  the rewrite is free, and on every restore.
- Nothing reaches the system prompt or `toolConfig`. `document_read`'s spec is already
  constant per deployment.

### 6.2 Handles instead of bytes in the persisted message

`_strip_document_bytes` (`turn_based_session_manager.py:2485`) runs on every restore
and replaces each document block with its digest. The persisted document bytes are
read back for one purpose only: building a digest lazily when the row has none. Once
every document is prepared at upload (§4.1), that purpose disappears.

So the message written to AgentCore Memory carries a handle (upload id, name, size)
in place of document bytes, while the live `agent.messages` keeps the bytes for the
warm agent. This removes the 10 MB event constraint for documents, and with it the
7 MB budget.

Images differ: they survive restore as bytes today. With a handle, restore fetches
them from S3. That fetch must be byte-identical (the stored, downscaled object),
must overlap the agent build, and must be measured against the TTFT budget before
it merges.

This subsection is the riskiest change in the spec and is verified separately (§11, Q1).

## 7. Flags

| Change | Flag | Why |
|---|---|---|
| Phase 0 telemetry, phase 1 extraction, `GET /files/limits`, the fixes in §1.3, phase 6 removal | none | No behaviour change, or a fix |
| Prepare every document at upload | `LARGE_DOCUMENT_ATTACHMENTS_ENABLED` (existing) | Same pipeline, same resources; more invocations |
| Token-based selection, raised limits, handles in persisted messages | `ATTACHMENT_PLAN_ENABLED`, in development, default off; SPA `features.attachmentPlan` | Widens what users can do and what a turn can cost |

Whether the last row is one flag or two is the driving developer's call. Either way
the flag selects a limits profile and retires in phase 6 (§9.1).

## 8. Measurement

### 8.1 What exists

- `backend/scripts/eval_harness.py` and `apis/shared/evals/`: real turns on dev as the
  eval user, JSON cases, planted-fact scoring, k=3 with an exact McNemar test against
  a per-model baseline, ledger publishing, a nightly track.
- Fixtures register uploads directly (`evals/fixtures.py:74`, `:105`) and wait for
  preparation and indexing.
- `document_qa` has 24 cases: small Markdown and text, large documents (including a
  scanned PDF), Word tables and tracked changes, a native PDF, HTML, two attachments,
  by-reference overflow and a follow-up turn.
- `attachment_tools` has 12 cases: images, spreadsheets, a CSV, decks and mixed sets.
- Neither family varies the delivery mode, the document's size in tokens, or the
  ceiling for the same question.
- Each `C#` cost row already carries `documentsAttached`, `documentTokens`,
  `documentDigests`, `documentReads`, `documentMime` and `cacheStatus`.

### 8.2 What is missing

| Gap | Fix |
|---|---|
| The delivery decision is not recorded | Write `attachmentPlan` on the turn's `C#` row: per file, `mode`, `class`, `tokens`, `reason`. Ids and numbers only. Written after the stream with the row's other fields |
| The eval runner drops timing | `apis/shared/harness/sse.py` keeps `turnDurationMs` and first-token time from the final `metadata` frame |
| No way to hold the question fixed and vary the mode | A fixture-only `delivery` value on the file row forces a mode. Only a direct row write can set it; no request field, no user surface |
| Upload admission is not exercised | An opt-in fixture path through presign, PUT and complete |
| `summarize_calls` omits the document fields | Extend `apis/shared/evals/calls.py` |

### 8.3 Hermetic surface

A table test of `plan_attachments`: file facts × model facts → expected plan, with
the table's digest pinned the way tool-spec digests are. It runs in CI with no model
call and fails on any unintended change to who gets what.

### 8.4 Eval family `attachment_delivery`

The same planted-fact questions run under forced arms, so a mode's effect is
separated from the document's.

| Dimension | Values |
|---|---|
| Type | pdf, docx, html, md, txt, csv, xlsx, pptx, png |
| Size | ~5k, ~30k, ~80k, ~150k, ~300k tokens; a set that exceeds any budget |
| Content | Prose, table, scanned, chart or figure |
| Count | 1, 5, 20 |
| Question | Lookup, cross-document, absent fact, visual, follow-up after the cache expires |
| Arm | `native`, `text`, `on_demand`, `search`, and `auto` (the planner) |
| Ceiling | 100k (today), 200k, and 100k with documents excluded (§5.2) |
| Model | The default Haiku and one larger model |

Not every cell is run. The gating set is the cells where two arms are both legal for
the file; the rest are advisory.

**The ceiling arms.** These answer whether a document held in context beats one read
on demand, and at what size. They run only the cells that can tell the arms apart:

- One document of 30k to 300k tokens, `native` or `text` against `on_demand`.
- Six turns: three lookups, then a question whose answer is in the document and was
  not asked before, after the document's turn has left the protected tail.
- Two paces: every turn inside the cache TTL, and one pause past it before turn 5.
- Each ceiling is set per run through the existing `COMPACTION_CEILING_CAP_TOKENS`,
  on a local stack or a dev session pinned to the run. The third arm needs the
  subtraction in §5.2, built behind the fixture and off everywhere else.
- A model with a price tier at 100k runs the 100k arms only.

The late question is the one that matters. It passes under `on_demand` whatever the
ceiling, and under `native` only if the document is still held or the digest is
enough. The readout is the size at which holding stops paying: where the late
question's pass rate is no better than `on_demand` and the cost per sample is higher.

### 8.5 What is reported

| Measure | Definition | Source |
|---|---|---|
| Quality | Pass rate per class × arm × model, paired against the baseline | Deterministic scoring; judges advisory |
| Cost | Dollars per sample; second-turn and cold-turn `cacheStatus`; attachment tokens in context; cuts and offloads per sample, with their reason | `C#` rows, `compactionEvents` |
| Speed | First token, turn duration, upload-to-usable time | Runner timing; fixture staging |

A limit is raised only when `auto` is not worse than the best legal arm on quality
for the affected cells, and the cost and first-token deltas are stated in the PR.

## 9. Phases

| Phase | Content | Behaviour change |
|---|---|---|
| 0. Measure | §8.2 and §8.4, including the ceiling arms; baseline today's behaviour on dev; the two Bedrock probes (§2.1) | None |
| 1. Extract | `plan_attachments` reproducing today's rules, with §8.3; `GET /files/limits`; SPA and TUI read it; the fixes in §1.3 | Fixes only |
| 2. Facts | Prepare every document at upload; one estimator; compaction and offload read row facts | Estimates change |
| 3. Tokens | The ladder in §4.3 and the document budget in §5.2 behind `ATTACHMENT_PLAN_ENABLED`; values from the phase 0 readout | Yes, flagged |
| 4. Limits | Handles in persisted messages; 20 files; PDF page rules; image downscale | Yes, flagged |
| 5. Overflow | Over-budget sets indexed and steered to `search` | Yes, flagged |
| 6. Retire | Delete the legacy profile, the flags and knobs it absorbed, and their tests and docs (§9.1) | None intended |

Phases 3 to 5 each end with an `attachment_delivery` run and a release-ledger readout.

### 9.1 Removal

A consolidation that only adds leaves two systems. Three rules keep this one from
doing that.

1. **The flag selects a limits profile, never a code path.** From phase 1 the planner
   is the only selector. `ATTACHMENT_PLAN_ENABLED` off means the planner runs the
   `legacy` profile (today's byte rules, expressed as planner inputs); on means the
   `token` profile. There is no second selector to keep alive, and the table test in
   §8.3 covers both profiles.
2. **A phase is not done until its rows below are done.** Removal ships in the same
   PR as the replacement, or in the phase's last PR.
3. **One place decides.** An architecture test, modelled on
   `test_attachment_kb_retrieve_boundary.py`, fails if anything outside
   `apis/shared/files/attachment_plan.py` chooses a delivery mode or reads a limit
   constant directly.

Usage counts are from `develop` at e39c6a9d1.

| What goes | Where it is today | Phase | Condition |
|---|---|---|---|
| `_apply_message_file_cap`, `_apply_inline_byte_budget`, `_partition_attachments` | `inference_api/chat/routes.py`; pinned by 4 test files | 1 | Planner's `legacy` profile passes the table test with the same outcomes |
| Admission rules that restate the turn's rules | `app_api/files/service.py` (`max_size_for`, the count check) | 1 | They call the planner's limits |
| `test_inline_budget_by_reference.py`, `test_attachment_turn_guard.py`, `test_large_document_turn_path.py`, `test_presentation_attachment_carveout.py` | `backend/tests/apis/inference_api/` | 1 | Selection cases move to the table test; what remains tests the route's wiring only |
| SPA limit constants and the per-rule checks | `file-upload.service.ts`, `inline-attachment-budget.ts` and its spec, `chat-input.component.ts`, `composer-add-menu.component.ts` | 1 | `GET /files/limits` is read at load. One built-in fallback object remains, so the composer can judge a drop before the fetch answers |
| The bytes/4 document estimate | `_bytes_tokens` for document blocks, `compaction_policy.py` | 2 | Compaction reads row facts |
| `estimate_document_tokens` as a primary source | `apis/shared/files/document_tokens.py`; 3 callers in `agents/main_agent/session/` | 2 | Callers read row facts. The function stays as the fallback for a file row written before phase 2 |
| The ceiling-subtraction fixture arm | Built in phase 0 behind the eval fixture | 3 | It becomes §5.2's production code, or is deleted if the readout drops §5.2 |
| `CONVERSE_DOCUMENTS_AS_TEXT_ENABLED` and `CDK_CONVERSE_DOCUMENTS_AS_TEXT_ENABLED` | 5 source files, 4 test files, `config.ts`, two constructs, `config.test.ts`, `platform.yml`, `.env.example` | 6 | Text is the `token` profile's rule. The `legacy` profile reads the flag until then |
| `_prerender_documents_as_text` and the per-call projection's own selection | `routes.py`, `apis/shared/models/document_cap.py` | 6 | The planner names the files to render; the projection keeps only the rendering |
| `INLINE_DOCUMENT_MAX_BYTES`, `INLINE_ATTACHMENTS_MAX_TOTAL_BYTES` | `apis/shared/files/models.py`; 4 and 5 source files, 8 and 5 test files | 6 | Their causes are gone: text removes the 4.5 MB limit, handles remove the 10 MB event limit |
| `inline-budget-notice.component.ts` | SPA | 6 | No per-message byte budget remains to explain |
| `MAX_FILES_PER_MESSAGE`, `LARGE_DOCUMENT_MAX_BYTES`, `TEXT_ONLY_DOCUMENTS_MAX_CHARS` as module constants | `models.py`, read in 6, 2 and 3 source files | 6 | They become fields of the limits profile. The environment overrides stay, read in one place |
| The `legacy` profile and `ATTACHMENT_PLAN_ENABLED` itself | Planner, `feature_flags.py`, CDK, `platform.yml`, three environment files | 6 | See below |

**What stays, on purpose.**

- `LARGE_DOCUMENT_ATTACHMENTS_ENABLED`, `ATTACHMENT_KB_ENABLED`, `FOLDER_ATTACHMENTS_ENABLED`
  and `FOLDER_SYNC_ENABLED`. Each gates resources or spend a deployment may not want.
  The planner takes them as inputs (which modes exist), and nothing else branches on them.
- The `DOCUMENT_OFFLOAD_*` values. Offload is unchanged by this spec.
- The fixture-only `delivery` value. It is permanent eval surface. A test pins that no
  request field can set it.
- **Reading document bytes from a persisted message.** Messages written before phase 4
  carry bytes, and no backfill is proposed. The read path can go once AgentCore Memory's
  event retention has passed since phase 4 reached production; confirm the retention
  period before deleting it.

**The flag's kind.** `ATTACHMENT_PLAN_ENABLED` is promoted as a **rollout switch**, not
a feature switch. What a deployment may reasonably want to choose is the limits, and
those are the profile's overridable values. Keeping the flag would keep the `legacy`
profile, its tests and its absorbed knobs alive for ever. Its retirement condition is
the default: shipped in a production release, two weeks there never flipped, and the
readout recorded. It gets a line in #1798 when it is turned on in prod and in #1422
when it is promoted.

**Phase 6 also updates:**

- `CHANGELOG.md`, a `Removed` entry naming every variable above, with the profile
  value that replaces it.
- The docs-site flag table, the flag paragraph in `CLAUDE.md` and `.env.example`.
- A superseded note at the top of `large-document-attachments.md`,
  `document-context-offload.md` and `drive-folder-attachments.md` for the sections
  this spec replaces, and `docs/feature-summaries/FILE_UPLOAD_IMPLEMENTATION.md`.
- The Runtime's environment: removing the text flag frees its bytes of the V2 budget.

## 10. Risks

| Risk | Mitigation |
|---|---|
| A larger in-context budget raises spend | The cold-cache offload is unchanged, so a held document is never re-written after a pause; the budget stops below a model's price tier; the hard ceiling still counts everything; cold-turn cost is a reported measure |
| Excluding documents from the chat ceiling lets total input grow past the size the 2026-09 replay priced | The ceiling arms report cost per sample at each setting; the chat ceiling itself does not move |
| The subtraction uses a wrong document size | It reads row facts (phase 2), never bytes/4; phase 3 does not start before phase 2 |
| Text mode loses layout a native docx block kept | The docx and html arms compare `native` and `text` in phase 0 before the rule changes |
| Image restore from S3 adds first-token latency | Measured at realistic scale and accepted explicitly, or images keep bytes in Memory with a lower count |
| Preparing every upload raises Lambda invocations and KB ingest | Small files skip sharding; KB eligibility stays as it is until phase 5 |
| 20 native PDFs exceed the request payload or the stricter many-image limit | The planner counts native blocks and bytes per request against the probed ceilings |

## 11. Open questions

1. **Does anything besides the lazy digest read persisted document bytes?** The restore
   path says no; share, handoff and fork views have not been checked.
2. **Per-file cap.** This spec holds 50 MB, which the prepare pipeline is measured for.
   500 MB is a different pipeline (streaming preparation, memory, shard counts).
3. **The document budget per model, and whether the chat ceiling excludes documents.**
   Set from the ceiling arms in phase 0. If holding a document never beats
   `on_demand` on the late question, §5.2 is dropped and the budget stays under the
   ceiling.
4. **Bedrock's PDF page and payload ceilings for 1M-context models.** Probe on dev in phase 0.
5. **Per-conversation count.** The Claude apps cap a chat at 20 files. This spec caps
   a message at 20 and lets the budget and `on_demand` govern the conversation.
6. **Small CSVs.** They could go `text` under the budget instead of always `tool`.
   Left as `tool` until an arm measures it.
7. **Growth inside one turn.** Compaction acts between turns, so `document_read`
   slices can take a turn past the ceiling across tool rounds. Not addressed here.
8. **Memory event retention.** §9.1 lets the bytes-reading restore path go once it has
   passed since phase 4. The period needs confirming.
