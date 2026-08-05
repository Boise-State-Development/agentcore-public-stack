# Document context offload — bound the cost of conversations with attachments

**Status:** Draft (no branch yet)
**Motivating measurement:** prod scan 2026-08-03, all 18,942 `C#` cost rows
joined to `boisestateai-v2-user-file-uploads` on `GSI1PK = CONV#{sessionId}`
**Related:** [[project-prod-cache-write-premium]] (the compaction root cause this
depends on), `docs/specs/session-workspace-tools.md` (the retrieval primitive
this extends), `docs/specs/tool-search-token-bloat-strategy.md` (same tenet,
different payload)

---

## 1. Problem

Conversations with attachments are 11% of prod sessions and **31% of prod
spend**.

| | sessions | mean $/session | median | calls/session | output tok/call | peak ctx >100k |
|---|---|---|---|---|---|---|
| With attachments | 395 (11%) | **$0.620** | $0.162 | 9.9 | 1,416 | 9.6% |
| No attachments | 3,137 (89%) | $0.176 | $0.048 | 4.8 | 725 | 2.3% |

47 of the 100 most expensive sessions carry files. Per-session cost by
attachment type: **tabular only $1.01**, non-PDF documents $0.63, PDF only
$0.59, images $0.29, none $0.18.

The cost is **not** the document's tokens on the turn it is attached. It is that
the document then lives in the cacheable prefix forever and is re-written in
full every time the cache goes cold. Cost decomposition for attachment
sessions: `cacheWrite 47.9%`, `output 21.5%`, `input 19.9%`, `cacheRead 6.2%`.

Full-prefix re-write rate vs. gap since the previous model call — the 5-minute
Bedrock cache TTL is plainly visible:

| gap | <1min | 1–5min | 5–15min | >15min |
|---|---|---|---|---|
| cold re-write | 3.4% | 16.3% | **66.1%** | ~65% |

Fleet-wide, cold re-writes on resumed turns are **$188 of $747 (25%)**.
Attachment sessions pay it hardest because their prefix is larger and their
users think longer between turns (2× the output tokens to read).

### Four concrete defects behind it

1. **The document never leaves the prefix.** `PromptBuilder.build_prompt`
   ([prompt_builder.py:23](../../backend/src/agents/main_agent/multimodal/prompt_builder.py:23))
   inlines the bytes as a `document` content block on the user message. Nothing
   ever removes it while the agent is warm. A 2 MB PDF (p90 of prod uploads is
   2.23 MB; PDFs run 1,500–3,000 tokens/page) enters on turn 1 and is re-written
   on every cold turn for the life of the session.

2. **Compaction cannot evict it.** `update_after_turn`
   ([turn_based_session_manager.py:703](../../backend/src/agents/main_agent/session/turn_based_session_manager.py:703))
   still only writes bookkeeping — `checkpoint`, `summary`, `truncation_anchor`.
   `_apply_compaction` (line 267) is called *only* from `initialize()` (line
   245), which never re-runs on an agent-cache hit. Nothing token-aware bounds
   the live list. This is the pre-existing root cause traced 2026-07-27;
   documents are what makes it expensive.

3. **Restore discards the document.** `_strip_document_bytes`
   ([turn_based_session_manager.py:944](../../backend/src/agents/main_agent/session/turn_based_session_manager.py:944))
   runs unconditionally on every restore and replaces the document with
   `[Document placeholder: name=…, format=…, original_size=… bytes]` — zero
   content. The bytes are present and fully decoded in `agent.messages`
   immediately before we overwrite them (verified — see §4E). **62 of 431
   attachment sessions (14%) upload the same filename 2–5 times**, which is what
   that looks like from the user's side. Each re-attach costs the document's
   tokens again *plus* a full prefix re-write.

4. **…and restore runs on nearly every turn, not just after an idle gap.**
   `get_agent` reads the agent cache only when no per-request tools were built —
   `if not extra_tools and cache_key in _agent_cache`
   ([service.py:279](../../backend/src/apis/inference_api/chat/service.py:279)).
   Any session with an injected tool enabled therefore builds a **fresh Agent
   every turn**, re-running `initialize()` → restore → strip. `enabled_tools`
   membership is the only precondition those builders check
   ([routes.py:393](../../backend/src/apis/inference_api/chat/routes.py:393)).

   **79.6% of prod attachment sessions (339/426) have at least one injected tool
   enabled** — overwhelmingly `analyze_spreadsheet` / `list_spreadsheets`, which
   look default-on in the picker (2,669 and 2,662 sessions; `create_artifact`
   957). Fleet-wide it is 76.3% of all sessions.

   So for four out of five attachment conversations the document is discarded on
   **turn 2**, regardless of idle time. The idle reaper (armed in prod by
   Release/1.13.0 on 2026-08-02) is a secondary trigger, not the main one.

   Those sessions also rebuild the Agent on every turn — its own latency and
   prompt-cache problem, but out of scope here. It deserves a separate issue.

**Corollary that shapes the fix.** The strip is currently *saving* money by
throwing the document away. Narrowing it to real name collisions — the obvious
one-line quality fix — would push the full document back into the prefix on
every turn and regress cost. Fixing quality without paying that is exactly what
the digest path below is for.

The `s3Location` path referenced in the comment at line 971 was never
implemented. Every document is inline bytes.

---

## 2. What Strands already gives us (and what it doesn't)

Checked against the pinned `strands-agents==1.48.0`.

### `ContextOffloader` — right shape, wrong hook

`strands/vended_plugins/context_offloader/plugin.py` intercepts oversized
results, persists each content block, and replaces it in context with a preview
plus per-block references. It registers a `retrieve_offloaded_content` tool
whose interface is exactly the quality-preserving one we want:

- `pattern` — regex/keyword grep, returns matching lines with `context_lines`
- `line_range: {start, end}` — 1-indexed span
- neither — full content, documented as "use sparingly — re-injects all tokens"

`_decode_full_content` reconstructs native blocks on retrieval: `image/*` comes
back as an `image` block, `application/*` as a `document` block. So a retrieved
PDF page returns at **full model fidelity**, not as flattened text.

**But it only fires on `AfterToolCallEvent`.** Our documents arrive on the user
message via `PromptBuilder`, never through a tool. `ContextOffloader` will never
see them. It is a template, not a drop-in.

Its `Storage` protocol is two methods — `store(key, content, content_type) -> ref`
and `retrieve(ref) -> (bytes, content_type)` — and an `S3Storage` backend ships
in the box. Our `user-file-uploads` table + user-files bucket satisfy that
protocol trivially.

### Message pinning — usable as-is

`strands/agent/conversation_manager/compression/pin_message.py` provides
`pin_message` / `is_pinned` / `partition_pinned` via
`message.metadata.custom.pinned`, with tool-pair partner protection.
`SummarizingConversationManager._summarize_oldest` honours it and mutates via
`agent.messages[:] = protected + [summary] + remaining` — **slice assignment**,
which is the pattern our #741 aliasing contract requires. Good precedent to
copy, not re-derive.

### What is missing

- No hook for user-message content. Offload for attachments must be ours.
- `workspace_read` ([workspace_tools.py:94](../../backend/src/agents/builtin_tools/workspace_tools.py:94))
  returns text inline up to 48 KB, but for binary (PDF, Office) it returns
  **metadata plus a download URL** — a URL the model cannot read. There is no
  path today to pull a specific PDF page back into context.

---

## 3. Existing methods, and the quality tension

Anthropic's [effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
names four primitives — compaction, structured note-taking, sub-agents, and
just-in-time retrieval — and explicitly recommends a **hybrid**: load critical
data upfront for speed, enable autonomous exploration on demand, "useful for
less dynamic content like legal or finance work." That is exactly our corpus
(policy PDFs, contracts, job descriptions, award nominations). It also calls
tool-result clearing "one of the safest lightest touch forms of compaction," and
warns that "overly aggressive compaction can result in loss of subtle but
critical context" — maximize recall first, tighten precision after.

Anthropic's [context editing](https://platform.claude.com/docs/en/build-with-claude/context-editing)
API (`clear_tool_uses_20250919`) is the productized version: server-side
clearing of old tool results, measured at **84% token savings and +39% task
performance on a 100-turn benchmark**. Two things carry over directly:

- **Clearing invalidates the cache prefix.** The API exposes `clear_at_least` so
  you only break the cache when the clearing is large enough to pay for itself.
  Our design must obey the same rule — an offload event costs one re-write.
- **`exclude_tools`** exists because some results must never be cleared. We need
  the same escape hatch for documents the user is actively working through.

Open question: whether Bedrock Converse accepts `context_management` via
`additionalModelRequestFields` (we already pass `anthropic_beta` there for
fine-grained tool streaming). It is undocumented on the Bedrock side. **Verify
before designing around it** — and note it would only cover tool results anyway,
not user attachments.

### The quality tension, stated honestly

The 2026 literature is consistent that neither extreme wins:

- Long context degrades non-linearly well before the window fills ("context
  rot"), and relevant content buried mid-window loses 20+ points versus the same
  content at the edges.
- But long context **beats** retrieval on holistic tasks that reason across a
  whole document — summarize, compare, "does this contract contradict itself."
- The 2026 default is hybrid: retrieve a focused slab, then reason over it.

Two stack-specific costs that the generic literature does not cover:

1. **Native citations require the document inline.** Bedrock's `DocumentBlock`
   citations config produces `citationsContent` blocks
   ([already in `_BEDROCK_CONTENT_BLOCK_KEYS`](../../backend/src/agents/main_agent/session/turn_based_session_manager.py:831)).
   Offload the document and Claude can no longer cite passages in it natively.
2. **PDFs are dual-encoded.** Each page is understood as an image *and* an
   extracted text layer — that is what preserves tables, charts, seals, and
   layout. A text-only digest throws the visual channel away. Any offload that
   flattens a PDF to text is a real quality regression on exactly the documents
   people attach.

Both push the same design conclusion: **offload by page/section as native
blocks, never as flattened text, and never on the turn the document is
introduced.**

---

## 4. Design

### Principle

The turn that introduces a document gets the **full document, inline, with
citations enabled** — best possible quality where it matters most. Subsequent
turns get a **digest plus a retrieval handle**, and the model pulls back the
pages it needs as native blocks.

This is the CLAUDE.md tenet ("per-turn payloads should be bounded or offloaded,
never unbounded pass-through") applied to the one payload that currently
violates it — and it is *not* a quality-for-cost trade, because today the
alternative after a restore is a placeholder carrying zero content.

### Lifecycle

```
turn N   (attach)  user msg: [text] [document: full bytes, citations on]   ← unchanged
                   pinned; the model answers with citations

turn N+1 (offload) user msg: [text] [text: <document-digest .../>]         ← ~800–1,500 tok
                   + tool: document_read(upload_id, page_range|pattern)
                   document bytes live in S3 (already there)

turn N+k (recall)  model calls document_read(upload_id, page_range={4,7})
                   tool result: native [document] block, pages 4–7 only
                   → full fidelity where it matters, ~4 pages not 200
```

### Components

**A. `DocumentDigest` — built once, at upload, off the model path.**

Extends the existing upload flow (`apis/app_api/files/service.py`). For each
non-tabular document: page/section count, a per-section outline with heading +
first-line snippet, detected structure (tables, figures), and a 3–5 sentence
abstract. Stored on the `FileMetadata` row. Rendered into context as a compact
XML-ish block:

```xml
<document name="BBR 5.0 Policy Form.pdf" upload_id="u-abc123" pages="47">
  <abstract>Cyber liability policy form, Beazley Breach Response 5.0 …</abstract>
  <section pages="1-3">Declarations — named insured, limits, retentions</section>
  <section pages="4-11">Insuring Agreements — A. Breach Response …</section>
  …
</document>
```

Budget: **≤1,500 tokens per document**, hard-capped. Generated with Haiku (the
extraction step does not need a frontier model; the *answer* still does).

**B. `document_read` tool — page-range and pattern retrieval, native blocks.**

New injected tool, bound to `(session_id, user_id)` like the workspace tools.

**Gated on session state, not `enabled_tools`.** The tool is built when the
session has at least one attachment, full stop — no catalog entry, no RBAC
grant, no picker toggle. Its ids stay **out** of `INJECTED_TOOL_IDS`
([injected.py:43](../../backend/src/apis/shared/tools/injected.py:43)) so they
never reach `ToolFilter`, exactly as Memory-Space tools do
([routes.py:609](../../backend/src/apis/inference_api/chat/routes.py:609)):
*"Not gated on `enabled_tools`: the governing capability is the Agent's
binding, not the user's tool picker."* Here the governing capability is the
user's own attachment.

Why not the obvious `workspace_files` key: **it is granted to no prod role.**
Verified against `boisestateai-v2-app-roles` — `staff` (27 tools), `faculty`
(26), `student` (13), `default` (0) and `demo_day` (0) all lack it; only
`system_admin` has it via `*`. Registering there would ship the recovery path
dark and reproduce the half-write trap in
[[project-rbac-grant-half-write-trap]] — the fix would be merged, deployed, and
reach nobody.

This also fixes a gap the check exposed: `analyze_spreadsheet` /
`list_spreadsheets` **are** granted, so CSV/XLSX already have a recovery path
(and are diverted from inline anyway). Everything that actually goes
inline — PDF, DOCX, TXT, MD, HTML, images, ~692 of 882 prod file rows — has
none today, text included.

```
document_read(upload_id, page_range={start,end} | pattern, max_pages=8)
```

- PDF → returns a `document` block containing **only the requested pages**,
  re-assembled server-side. Citations stay enabled on it.
- DOCX/HTML/MD/TXT → returns text for the matching sections, bounded by the
  existing `WORKSPACE_READ_MAX_BYTES` (48 KB) with `offset` continuation.
- `pattern` greps the extracted text layer and returns the *page numbers* that
  match plus surrounding lines, so the model can then ask for those pages.
- Hard cap `max_pages` so one call cannot re-inject the whole document.

Modelled on `retrieve_offloaded_content`'s interface deliberately — same three
modes (pattern / range / full-as-last-resort), same guidance text shape.

**C. Offload trigger — deferred, cache-aware, once per document.**

Runs in `update_after_turn`, alongside (not inside) the compaction checkpoint
logic. Replace a document block with its digest when **all** of:

- the document is no longer pinned (see D), **and**
- ≥1 full turn has elapsed since it was introduced, **and**
- the document's estimated tokens ≥ `DOCUMENT_OFFLOAD_MIN_TOKENS` (default
  5,000 — our `clear_at_least`; below this the cache re-write costs more than it
  saves), **and**
- the prompt cache is already cold (>`cache_ttl_seconds` since the last call),
  *or* the live context exceeds the compaction threshold.

The last condition is the one [[project-prod-cache-write-premium]] found being
violated by the existing truncation deferral — 72% of truncation events fired
while the cache was still live. **Get this guard right here, and fix it there.**

Mutation is **slice assignment on `agent.messages`, never rebinding** — guard
test `test_second_cache_key_for_a_session_shares_the_conversation`. The offload
is monotonic and recorded in the compaction state so it never runs twice and
never moves backwards (the #751 shape).

**D. Pinning — the `exclude_tools` equivalent.**

A document stays pinned (never offloaded) while it is the active subject:

- the turn it was attached, plus the next turn;
- any turn where the model called `document_read` against it;
- while the user's message names its filename.

Uses Strands' `pin_message` / `is_pinned` so compaction and offload agree on one
flag. A user working through one contract for ten turns keeps it inline; a user
who attached six files and is now discussing one keeps one.

**E. Restore becomes lossless.**

*Verified: the content is still there to recover.* Uploads are capped at 4 MB
([`MAX_FILE_SIZE_BYTES`](../../frontend/ai.client/src/app/services/file-upload/file-upload.service.ts:43),
`FILE_UPLOAD_MAX_SIZE_BYTES`, `INLINE_DOCUMENT_MAX_BYTES`). AgentCore's 100 KB
`CreateEvent` *message* quota looks like it would reject the write, but the SDK
checks `exceeds_conversational_limit` (`CONVERSATIONAL_MAX_SIZE = 100000`) and
falls back to a `blob` payload, bounded by the 10 MB *event* quota. Strands'
`SessionMessage` base64-encodes bytes on write and decodes them on read. So a
4 MB document (~5.33 MB encoded) round-trips intact and is present in
`agent.messages` immediately before line 214 discards it. **PR-3 can rehydrate
from restored history itself — it does not need S3.**

*Measured edge (real, rare, own PR).* The message — not the file — is the
stored unit, so a turn's attachments are summed against the 10 MB event quota;
the break point is ~7.5 MB of raw attachments. `MAX_FILES_PER_MESSAGE = 5`
exists **only in the SPA**
([file-upload.service.ts:48](../../frontend/ai.client/src/app/services/file-upload/file-upload.service.ts:48));
there is no backend count or aggregate-size cap, and 5 × 4 MB is comfortably
over. In prod, clustering uploads within 120s in a session as a proxy for one
turn: **4 of 615 clusters (0.7%) exceed the threshold**, largest 29.89 MB across
11 files; median cluster 0.17 MB, p90 2.58 MB. Both caveats push the true rate
lower — the 120s window can merge distinct turns, and tabular files are diverted
before the message is built. `create_message` re-raises as `SessionException`
rather than swallowing, so the failure mode is a hole in history: worse than the
strip, but ~100× rarer.

`_strip_document_bytes` stops emitting a contentless placeholder and emits the
**same digest block** instead, rehydrated from `FileMetadata` (`GSI1PK =
CONV#{sessionId}` already indexes it), with `document_read` live. A returning
user gets a model that still knows what is in their document and can pull any
page back. This is the change that should end the 14% re-upload rate — and it is
a *correctness* fix that happens to save money.

### Failure modes, all fail-open

Digest generation fails → keep the document inline (today's behaviour).
S3 unreachable at `document_read` → tool returns an error string, model reasons
from the digest. Offload raises → leave the message untouched, log, continue.
Kill switch `DOCUMENT_OFFLOAD_ENABLED=false` (default-on-with-kill-switch, the
`WORKSPACE_TOOLS_ENABLED` pattern).

---

## 5. PR sequence

Re-sequenced around defect 4: the loss fires on turn 2 for ~80% of attachment
sessions, so the recovery path and the digest are the urgent half, and the
turn-level offload — the cost work — comes after.

| PR | Scope | Gate |
|----|-------|------|
| 1 | `document_read` (page-range + pattern), native `document` block reassembly, **gated on the session having an attachment**; ids kept out of `INJECTED_TOOL_IDS` | 47-page PDF: `page_range={4,7}` returns 4 pages, ≤6k tok; `max_pages` cap holds; tool present for a `student`-role session with no grants |
| 2 | `DocumentDigest` model + Haiku extractor + persist on `FileMetadata`, generated at upload; **not yet used in context** | digest ≤1,500 tok for a 200-page PDF; extractor p95 < 8s; no chat-path change |
| 3 | `_strip_document_bytes` → digest + live `document_read` handle instead of the placeholder (restore path only) | a session with `analyze_spreadsheet` enabled answers a document question correctly on **turn 2**; re-upload rate starts falling |
| 4 | Offload trigger in `update_after_turn` + pinning, behind `DOCUMENT_OFFLOAD_ENABLED` | slice-assignment guard test passes; offload fires at most once per document; never fires while cache is live |
| 5 | Fix the cache-live guard on the *existing* truncation deferral (same predicate as PR-4) | truncation events while cache live: 72% → ~0 |
| 6 | Backend guard on a turn's aggregate inline attachment bytes (~7.5 MB), mirroring the SPA's `MAX_FILES_PER_MESSAGE` | oversized turn degrades to the `oversized_inline` guidance path, never to `SessionException` |
| 7 | `hasDocuments` / `documentTokens` on `MessageMetadata`; admin cost anatomy shows document share | the two-table join this spec required becomes a dashboard column |

**PRs 1–3 are the correctness fix and should ship together as a unit.** None
carries cost-regression risk: the digest is strictly smaller than the document,
and `document_read` only adds tokens when the model chooses to spend them. PR-1
must precede PR-3 — a digest that points at a tool nobody has is no better than
today's placeholder.

PR-4 is the cost work and is independently revertible. PR-6 is unrelated to both
and can go whenever.

**Out of scope, but surfaced by this work:** the `extra_tools` agent-cache
bypass makes ~76% of *all* sessions rebuild their Agent every turn. Fixing that
would cut latency and re-run `initialize()` far less often, but it interacts
with the #741 aliasing fix and the paused-agent resume path, so it needs its own
design. File it separately; do not fold it in here.

---

## 6. Expected impact, and how we'll know

Steady-state prefix for a document session drops from the document's full token
count to ~1,500. Against the measured prod trajectories, the recoverable
envelope is the cold-re-write cost in attachment sessions — **$66.24 of the
$233.61 attachment spend (28%)** — minus what pinning deliberately leaves
inline. A conservative target is **−15% on attachment-session cost with no
quality regression**, measured as:

- `cacheWrite` tokens per attachment session, before/after (primary)
- write:read ratio for the attachment cohort — the metric
  [[project-prod-cache-write-premium]] established as the one that actually
  moves. `wastedUsd` and the `AvoidableMiss` alarm are blind to this mode.
- re-upload rate (same filename ≥2× in a session): 14% → target <3%. Note this
  is the one metric PRs 1–3 move on their own, and it should move *before* PR-4
  lands — if it doesn't, the recovery path isn't reaching users (check the gate
  first, per the `workspace_files` finding in §4B)
- `document_read` call rate per attachment session — if it is near zero the
  digest is too good to be true and the model is answering without the source;
  if it is >3/turn the digest is too thin

### Quality gate — this must not ship on cost numbers alone

**Full evaluation design: `docs/specs/document-offload-evaluation.md`** (three
axes — answer quality, token efficiency, cost effectiveness — with the
randomized-flag comparison, the blinded scoring protocol, and the stopping
rule). Validation of this spec's measurements:
`docs/specs/document-context-offload-validation.md`.

In brief: a fixed eval set of real-shaped tasks over held documents (~120, not
the ~30 first sketched here — 30 paired tasks only detects ~25-point
regressions), scored blind across three arms (today / PRs 1–3 / PRs 1–4):
holistic tasks (summarize, compare two documents, find internal
contradictions), lookup tasks (single fact, table cell, figure/chart — the
dual-encoding canaries), and citation tasks (right page, including page-number
identity under `document_read` reassembly). Holistic and citation tasks are
where offload is most likely to hurt — if either regresses, pinning is too
narrow, and the answer is to widen pinning, not to accept the regression. Per
the CLAUDE.md tenet: when cost and quality genuinely conflict, quality wins; we
look for the cheaper path to the *same* quality.

Note from validation: the lifecycle diagram's "citations on ← unchanged" is
wrong — no citations config is sent anywhere today (`document_handler.py`
emits only `format`/`name`/`source.bytes`), so enabling citations is new work
in PR-1, and the evaluation's §1 baseline probe must establish current PDF
visual fidelity before any digest comparison is scored.

---

## 7. Non-goals

- **Extended (1h) cache TTL.** Modelled against the real prod trajectories:
  blanket +12.3% fleet cost; "1h once the session has shown a >5min gap" +5.7%;
  "after two >5min gaps" +4.2%; a perfect oracle only −8.7%. `CacheConfig(ttl=)`
  and `CacheToolsConfig` exist in the pinned SDK and it looks like a one-line
  win, which is exactly why this is written down. Independently confirms the
  2026-07-27 finding — do not re-litigate a third time.
- **Per-session RAG over attachments.** A real option (S3 Vectors + the
  assistant-KB pipeline already exist) and complementary, but it is a bigger
  build and the digest+`document_read` path covers the same cases with less
  machinery and no embedding-model lock-in. Revisit if `document_read` call
  rates show the model thrashing.
- **The tabular cohort.** Most expensive per session ($1.01) but a different
  mechanism — the Code Interpreter probe loop, not prefix re-writes. Separate
  spec.
- **Replacing `SlidingWindowConversationManager` wholesale.** Out of scope; this
  spec must not depend on that landing first.

---

## Sources

- [Effective context engineering for AI agents — Anthropic](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Context editing — Claude Platform Docs](https://platform.claude.com/docs/en/build-with-claude/context-editing)
- [Managing context on the Claude Developer Platform](https://claude.com/blog/context-management)
- [PDF support — Claude Platform Docs](https://platform.claude.com/docs/en/build-with-claude/pdf-support)
- [Citations — Claude Platform Docs](https://platform.claude.com/docs/en/build-with-claude/citations)
- [Citations API and PDF support for Claude models in Amazon Bedrock](https://aws.amazon.com/about-aws/whats-new/2025/06/citations-api-pdf-claude-models-amazon-bedrock/)
- [Long Context vs. RAG for LLMs: An Evaluation and Revisits](https://arxiv.org/pdf/2501.01880)
- [Context Rot, RAG, and Long Context: How to Architect LLM Systems in 2026](https://glasp.co/articles/context-rot-rag-long-context-hybrid)
