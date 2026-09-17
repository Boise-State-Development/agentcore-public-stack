# Document context offload — bound the cost of conversations with attachments

**Status:** PR-1 built — `feature/document-offload-pr1`, off `develop` @
`c28ccdbd` (after the compaction stack #1125 → #1128 → #1129 → #1131 →
#1132 merged). Revised 2026-09-16: analytics pulled forward from PR-7 into
PR-1 (§5, §6.1), decision log in §8.
**Motivating measurement:** prod scan 2026-08-03, all 18,942 `C#` cost rows
joined to `boisestateai-v2-user-file-uploads` on `GSI1PK = CONV#{sessionId}`;
re-confirmed by the 2026-09-15 prod cost audit — of 95 September sessions
that peaked over 100k tokens, 25 (26%) had an attachment in their last three
turns, including 1–3-turn sessions at 280k–530k that are a single huge
upload. A compaction cut cannot touch those (thresholds spec §3.6 keeps
attachments out of scope on purpose); a digest plus a page-range read can.
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

**Revised 2026-09-16.** The analytics that were PR-7 now ship *in* PR-1, so
the cost work is measured from the first day the recovery path exists and the
ship / widen-pinning / abandon decision in the evaluation spec (§4.2 there)
is decidable from stored rows rather than a one-off scan. The field design is
§6.1 below; the kaizen finding that the compaction work has no quality signal
joined to its cost data is addressed there too — as far as the platform
currently allows (see the outcome-signal row).

| PR | Scope | Gate |
|----|-------|------|
| 1 — **built** | `document_read` (page-range + pattern + bounded text; native `document` block reassembly), **gated on the session having a readable attachment**, id kept out of `INJECTED_TOOL_IDS`, presence carried in the agent-cache key; **analytics**: per-call document context on `C#` rows, the `documentReads` ledger entry, the `document_stripped` ledger event, session-row rollups, anatomy + profile surfaces, EMF; `document_read` results exempt from the tool-result offloader | 12-page PDF: `page_range="4-7"` returns 4 pages as one native block whose page 1 is original page 4; `max_pages` and the hard cap hold; tool built for a session with no grants and no picker toggle; content-policy test walks the new fields; full backend suite green |
| 2 — **built** (`feature/document-offload-pr2`, stacked on PR-1) | `DocumentDigest` (`apis/shared/files/document_digest.py`): a deterministic outline (headings with page / paragraph / line anchors, table and figure mentions, counts) plus a 3–5-sentence abstract from the cheap text model (Nova Micro, `DOCUMENT_DIGEST_MODEL_ID`), built off the request path when a document upload completes and persisted as `FileMetadata.digest`; `render_digest` produces the `<document-digest …>` block under a hard 1,500-token budget (sections dropped first, then the abstract) and stores the estimate as `digest.tokens`; `digest.abstract` / `digest.sections` denylisted, coverage on the attachment profile; **not yet used in context** | 200-page PDF digest ≤1,500 tokens (tested); extraction and abstract each fail open; kill switch `DOCUMENT_DIGEST_ENABLED`; no chat-path change; p95 latency to be read from `DocumentDigestMs` in dev |
| 3 — **built** (`feature/document-offload-pr3`, stacked on PR-2) | `_strip_document_bytes` → `session/document_rehydration.py`: each inline document block is matched to its upload row (sanitized filename incl. PromptBuilder's `_2` suffix, byte-size tiebreak, newest first, no double-claiming) and replaced by the rendered `<document-digest … upload_id=…>` block; rows without a digest get an **outline-only digest built from the bytes already in the restored message** (no S3, no model call) and persisted; unmatched blocks keep the placeholder byte-for-byte; ledger records `document_rehydrated` and `document_stripped` separately; kill switch `DOCUMENT_REHYDRATE_ENABLED` | restore output is stable across restores (tested); a matched block carries the abstract, outline and handle; unmatched blocks are unchanged from today; `document_stripped` per session-day should fall to the unmatched residue (direct base64 attachments, deleted files) — read it from the ledger; re-upload rate (byte-identical) starts falling |
| 4 — **built** (`feature/document-offload-pr4`, stacked on PR-3) | Offload at **head-of-turn**, in the slot right after `apply_pending_compaction` (not post-turn: the compaction stack's §3.5 "decide post-turn, apply when free" is the same rule) — `session/document_offload.py` + `TurnBasedSessionManager.apply_document_offload`: pinning (attach turn + next, prompt names it, recent `document_read` result), `DOCUMENT_OFFLOAD_MIN_TOKENS` (5,000), and the cache-gap decision (`cache_expired` / `prefix_changed` / `over_ceiling`, else wait); the replacement is PR-3's restore transformation so the live block equals a cold restore's; aged `document_read` slices stubbed on both paths; `DOCUMENT_OFFLOAD_ENABLED` kill switch + `DOCUMENT_OFFLOAD_ROLLOUT_PERCENT` bucket on `crc32(session_id)`; records `document_offload` with `cacheGapSeconds` | in-place mutation only (no rebinding); a pinned document never moves; nothing moves while the cache is live (tested); **zero** `document_offload` events with `cacheGapSeconds` under the TTL except `over_ceiling` / `prefix_changed` ones, which carry their reason |
| 5 — **verified closed + measured** (`feature/document-offload-pr5`, stacked on PR-4) | The defect this row was written against (July 2026: 34 of 47 truncation events fired inside the 5-minute TTL) was closed by the compaction stack before this spec's PRs began: the truncation anchor now moves only with a cut (applied paid-when-free, thresholds spec §3.5) or in `_maybe_advance_truncation_anchor`, which checks `_cache_window_expired` first — the same predicate PR-4 reuses — and the stability suite already pins "a warm cache never advances the anchor". What was missing was the proof from rows: the advance was a log line. PR-5 records a `truncation_anchor` ledger event (`anchorFrom`, `anchorTo`, `cacheGapSeconds` measured before the save re-stamps `updated_at`) plus `TruncationAnchorAdvanced` / `TruncationAnchorCacheGapSeconds` EMF, and puts `cacheGapSeconds` on the restore-time `applied` event so a slice or truncation landing inside the TTL is countable as what it is — a rebuild while the cache was live | `truncation_anchor` events with `cacheGapSeconds` under the TTL: **zero** (any one is a regression of the guard); restore `applied` events with `truncatedToolResults > 0` and a short gap measure the rebuild-while-live cohort the `extra_tools` bypass spec owns |
| 6 | Backend guard on a turn's aggregate inline attachment bytes (~7.5 MB), mirroring the SPA's `MAX_FILES_PER_MESSAGE` | oversized turn degrades to the `oversized_inline` guidance path, never to `SessionException` |
| 7 | **Outcome signal**: a content-free thumbs up/down on assistant messages, persisted keyed on `(sessionId, messageId)` so it joins the `C#` row's `hasDocuments` / `documentDigests` / `documentReads` in one key; fleet-level document-share column on the admin dashboard | down-thumb rate reported by turn class (full / digest-only / retrieved) with n per class |

**PRs 1–3 are the correctness fix and should ship together as a unit.** None
carries cost-regression risk: the digest is strictly smaller than the document,
and `document_read` only adds tokens when the model chooses to spend them. PR-1
must precede PR-3 — a digest that points at a tool nobody has is no better than
today's placeholder.

PR-4 is the cost work and is independently revertible. PR-6 is unrelated to both
and can go whenever. PR-7 exists because **no feedback surface exists today**
(`MessageMetadata` carries only a `# feedback: …` placeholder comment); the
rows PR-1 writes make the join a one-key lookup the moment one does.

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

### 6.1 Analytics field design (PR-1, built)

**Rule: content-free by construction.** Every field below is an id, a count,
a byte size, a token estimate or an enum key. No document text, title or
filename is ever persisted in a metric, a cost row or an EMF record — the
`document_read` tool result carries filenames (that is conversation content,
where the model needs them); the ledger reads only the result's numbers. The
content-policy test walks the new projections and response models.

**Alignment.** Same mechanism as the compaction ledger (#1130): per-call
facts ride the `C#` cost row as flat extra fields next to `prefixTokens` /
`windowRemovedMessages` / `compactionEvents`; lifecycle events use
`record_compaction_event` and the `compactionEvents` list; session rollups are
`ADD`ed on the `S#` row beside `compactionAppliedCount`; EMF goes to
`AgentCoreStack/Compaction`; the anatomy (`GET /admin/costs/sessions/{id}/calls`)
and the profile read them. One namespace, one ledger, one anatomy view.

**Per model call (`C#` row).** Written by the stream coordinator from
`agent.messages` at turn end (`session/document_context.py`); gated by
`COST_DIAGNOSTICS_ENABLED` like the rest of the ledger, so an absent field
reads "not tracked", never 0.

| field | meaning |
|---|---|
| `hasDocuments` | ≥1 inline attachment block (document or image bytes) is in the live context |
| `documentCount` | inline attachment blocks on user prompts |
| `documentTokens` | their estimated weight — the compaction estimator's bytes/4 per document, flat per image. Heuristic, comparable across rows; the measured total it is a share of is `tokenUsage` (input + cacheRead + cacheWrite) minus `prefixTokens.system + prefixTokens.tools` |
| `documentDigests` | digest / placeholder stand-ins in context (`[Document placeholder:` today, `<document-digest` from PR-3) |
| `documentsAttached` | attachment blocks on this turn's prompt — an attach turn vs a follow-up |
| `documentSlices` / `documentSliceTokens` | `document_read` page slices still living in history as tool-result document blocks (the re-injected pages, bounded by the tool's cap) |
| `documentMime` | `{format: count}` keyed by Bedrock's `document.format` enum plus `image` — the attachment MIME class |
| `documentReads` | `{calls, pages, bytes}` of `document_read` retrievals this call requested (ledger hook, `AfterToolCallEvent`, attributed like the tool census) |

**Turn class**, derived per row: *full* (`hasDocuments`), *digest-only*
(`documentDigests > 0` and not `hasDocuments`), *retrieved*
(`documentReads.pages > 0`), or *none*. This is the digest-vs-full share.

**Lifecycle events** (`compactionEvents[].kind`, numbers only):

| kind | when | fields |
|---|---|---|
| `document_stripped` | restore replaced inline documents with contentless placeholders (the defect; recorded from PR-1 so its reach is measured before and after PR-3) | `documents`, `documentTokens` |
| `document_rehydrated` | PR-3: restore replaced them with a digest + live handle | `documents`, `documentTokens` |
| `document_offload` | PR-4: the post-turn trigger swapped a document for its digest | `documents`, `documentTokens`, **`cacheGapSeconds`** at the moment it fired |

**Session rollups (`S#` row, `ADD`):** `fullDocumentCalls`, `digestOnlyCalls`,
`documentReadCalls`, `documentReadPages` — so the profile can answer without
the rows once they expire, and so a fleet query needs one table.

**EMF (`AgentCoreStack/Compaction`):** `DocumentRead` / `DocumentReadPages` /
`DocumentReadBytes` with properties `mode` (`list` / `index` / `pages` /
`pattern` / `text`) and `format`. PR-4 adds `DocumentOffloaded` /
`DocumentOffloadedTokens` beside `ToolResultOffloaded`.

**Admin surfaces:** anatomy rows carry every per-call field (`documentReads`
as a map); the profile carries `fullDocumentCalls`, `digestOnlyCalls`,
`peakDocumentTokens`, `documentReadCalls`, `documentReadPages` and
`dataCoverage.documents`; the SPA anatomy page shows a per-row `doc` /
`digest` / `+Np` badge, a Documents line in the expanded row, and the
consumption summary under the Attachments card.

**What is decidable from the rows alone** (the point of pulling this forward):

- *Document share of the prefix*, per call: `documentTokens / (context −
  prefixTokens.system − prefixTokens.tools)`. Summed over cold rows
  (`cacheStatus ∉ {hit}`), `cacheWriteInputTokens × share` is the document
  part of every re-write — the recoverable envelope the evaluation spec's §4.1
  says was unmeasured, and the number that turns "−15%" into a derived target.
- *Was the offload ever wrong*: `document_offload` events whose
  `cacheGapSeconds` is under the cache TTL. Target zero; any other value is
  the compaction PR-3 rule being broken.
- *Did the recovery path reach users*: `document_stripped` per session-day
  before PR-3, `document_rehydrated` after; `documentReadCalls` per attachment
  session against the evaluation's health band (≈0.3–3; ≈0 means the digest is
  answering unaided, >3/turn means it is too thin).
- *Digest-vs-full shares and the B/C arms*: `fullDocumentCalls :
  digestOnlyCalls` per session, split by the PR-4 rollout bucket.
- *The outcome signal* — **not buildable today.** There is no feedback
  surface (`MessageMetadata` has a `# feedback:` placeholder only). The rows
  are keyed so that a thumbs row on `(sessionId, messageId)` joins the turn
  class in one lookup; PR-7 adds that surface. Until then the quality gate
  stays the evaluation spec's offline harness, and this remains the kaizen
  gap it has been for the compaction work too.

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

> **G3 probe run 2026-08-12 — `document-citations-probe-findings.md`.** The
> baseline is established: **full visual fidelity, uncited.** Today's bare
> document block reads unlabeled chart bars, image-only table cells and
> rotated scans — 14/14 on two models — so the §3 "quality tension" is real
> and this spec's *offload as native blocks, never flattened text* rule is now
> measured rather than precautionary.
>
> Two revisions follow. **Citations are a text-layer feature**, not a visual
> one: enabled explicitly, image-only documents returned none at all. So
> "citations stay enabled on it" (§4 lifecycle, and the reassembly note) is
> not a quality requirement for image-heavy documents — there is nothing to
> preserve — and **enabling citations is no longer part of PR-1**. It is a
> standalone product question about attribution, with a real migration cost:
> with citations on, the answer text moves *inside* `citationsContent` and
> top-level `text` blocks go empty, so every consumer must handle both shapes.
> Where citations *do* apply — text-layer documents — `location.documentPage`
> supplies the page identity the evaluation's reassembly test needs.

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

## 8. Decision log — PR-1 (2026-09-16)

1. **Analytics ship first, not last.** PR-7's fields moved into PR-1 (§6.1) so
   the cost work is measured from day one and the evaluation's stopping rule
   is decidable from stored rows. The `document_stripped` event is recorded
   *before* the fix that removes it, so PR-3's effect is a before/after on one
   counter rather than a scan.
2. **Gate = session state; presence lives in the agent-cache key, not a
   cache veto.** `document_read` is built when the session has a READY
   document-class upload (PDF, DOCX, TXT, MD, HTML — not tabular, decks or
   images, which have other paths) or this turn attaches one. Positive
   answers are memoized per process (monotonic in practice). Vetoing the
   agent cache instead — the Memory-Space pattern — would have made every
   attachment session rebuild its agent each turn and hit the strip on turn
   2 for the ~19% that keep a warm agent today: a quality regression PR-1
   alone would introduce. A `document_tools` element in `_create_cache_key`
   flips at most once per session, on the attach turn, when restored history
   has no document to lose; the resume path recomputes the same gate so a
   paused agent's key is reproduced.
3. **Id recorded, never injected-filtered.** `DOCUMENT_TOOL_IDS` exists for
   the record and is deliberately not in `INJECTED_TOOL_IDS`; the only control
   is `DOCUMENT_READ_ENABLED` (default on, `=false` kills).
4. **`document_read` results are exempt from the tool-result offloader**
   (`OFFLOAD_EXEMPT_TOOLS` in `core/tool_result_offload.py`, plus the
   plugin's own `should_offload`). Offloading the slice the model just asked
   for would undo the read. The tool's `max_pages` (default 8, hard cap 20)
   is the bound instead.
5. **Slices persist in history.** A retrieved page range is a tool-result
   document block with a unique Bedrock-safe name (`"<stem> p4-7 <6 hex>"`),
   so it survives restore (the strip only touches top-level blocks) and never
   collides. They are counted on the row (`documentSlices`); PR-4 should age
   them like documents rather than let them re-write forever.
6. **Page identity.** The slice is a new PDF numbered 1..k; the payload's
   `page_numbering` note and the tool description say "page k is original
   page start+k−1 — cite original page numbers". The evaluation's
   citation-page-identity family is the test of whether models follow it.
7. **No new dependency.** PDF slicing and text extraction use `pypdfium2`
   (already pinned for thumbnails); DOCX text comes from a stdlib
   `zipfile` + `ElementTree` extractor over `word/document.xml`. `page_range`
   is PDF-only; DOCX and text use `pattern` or bounded `offset` reads
   (the workspace read bound, 48 KB).
8. **Token numbers are heuristics.** Bedrock reports no per-block usage;
   `documentTokens` is bytes/4 (documents) and a flat estimate per image,
   the compaction estimator's own numbers. They are comparable across rows and
   against the measured messages partition, which is all the decisions in
   §6.1 need.
9. **Metrics share the compaction namespace** (`AgentCoreStack/Compaction`)
   on purpose: the cut record, the tool-result offload record and the
   document read are three answers to one question — what bounded this
   session's prefix — and belong on one dashboard.
10. **`docs/specs/document-conversations-cost.md` does not exist in the
    repo.** The numbers it would hold are §1 here and the validation report;
    nothing in this spec depends on it.
11. **Validation nits carried in:** `INJECTED_TOOL_IDS` is in
    `apis/shared/tools/injected.py` (line drifted); `WORKSPACE_READ_MAX_BYTES`
    lives in `apis/shared/files/workspace.py`. Citations remain out of PR-1
    (§6 probe note).

**PR-2 (2026-09-16)**

12. **Outline is deterministic; only the abstract uses a model.** Page /
    paragraph / line counts, headings (numbered, ALL CAPS, or title-cased
    short lines; markdown `#`), table / figure mentions and a text sample
    come from `pypdfium2` and the PR-1 DOCX extractor. The abstract is 3–5
    sentences from Nova Micro over the sample plus outline — the same cheap
    text model the tool-batch summaries and the compaction summary run on,
    and the extraction step never needs a frontier model. The §4A sketch said
    Haiku over the document; a text model over extracted text is cheaper,
    text-only is enough for an abstract, and the visual channel is preserved
    by `document_read`, not by the digest.
13. **Built at `complete_upload` only, fire-and-forget.** The SPA upload flow
    is where documents enter; agent-written files (`workspace_write`, Word /
    Excel / PowerPoint tools) register `FileMetadata` directly and get no
    digest. Uploads that predate PR-2 have none either. **PR-3 must generate
    lazily on the restore path when `digest` is absent** (same builder,
    `with_abstract` optional under a latency budget) rather than assume it.
14. **The digest is content-bearing.** `digest.abstract` and
    `digest.sections` are denylisted; `digest.status` / `format` / `count` /
    `tokens` are projected so the attachment profile can report coverage and
    the per-file token cost the 1,500 ceiling is enforced against.
15. **Budget enforcement is in the renderer**, not the extractor: sections
    are dropped from the end, then the abstract is truncated, and the opening
    tag (the `document_read` handle) always survives. `digest.tokens` records
    the rendered estimate, so "digest ≤1,500 tokens" is a stored fact per
    file rather than a claim.

**PR-3 (2026-09-16)**

16. **Matching is by sanitized filename, not by id.** A document block
    carries no upload id — only the name `PromptBuilder` gave it, which is
    `FileSanitizer.sanitize_filename(filename)` (the extension's dot becomes
    an underscore: `BBR Policy.pdf` → `BBR Policy_pdf`, duplicates get
    `_2`/`_3`) — so restore matches blocks to the session's upload rows on
    that name with the byte size as tiebreak, newest row first, each row
    claimed once. Adding an upload id to the block itself would be cleaner
    but changes the attach-turn bytes (a prefix change for every attachment
    turn); left for a later PR that touches the prompt builder anyway.
17. **Lazy digests are outline-only and come from the restored bytes.** The
    bytes are in `agent.messages` at the moment of the strip (§4E), so no S3
    read is needed, and the restore path is synchronous inside the agent
    constructor, so no model call is made there. The digest is persisted so
    the next restore renders identical bytes; the upload-path build (PR-2)
    may later overwrite it with one that has an abstract, which changes the
    rendered block once — one prefix re-write, visible as a jump in
    `document_rehydrated.documentTokens`.
18. **Restore output is byte-stable** given the same upload rows: the
    renderer is deterministic over `FileMetadata.filename`, `upload_id` and
    the persisted digest, and unmatched blocks keep the pre-PR-3 placeholder
    verbatim. Tested. The cache contract in CLAUDE.md is therefore honoured
    on the restore path exactly as before.
19. **Never worse than before.** Every failure — lookup, matching, digest
    build, persistence — falls back to the placeholder for that block and
    is recorded as `document_stripped`; the lookup is attempted once per
    restore, and only when the history holds a document block.
20. **The synchronous constraint is real.** `TurnBasedSessionManager.initialize`
    runs under the Strands agent constructor with an event loop already
    running, so it cannot await; the repository gained `_sync` bodies for the
    session query and the digest write (boto3 was synchronous underneath all
    along) rather than a thread-with-its-own-loop.

**PR-4 (2026-09-16)**

21. **Head-of-turn, not `update_after_turn`.** §4C said the trigger runs
    post-turn. The compaction stack has since moved to "decide post-turn,
    apply at the head of the next turn only when the re-write is free or
    unavoidable" (`apply_pending_compaction`, thresholds spec §3.5), and
    that predicate — cache expired, prefix changed, or the ceiling is
    already breached — *is* this spec's cache-aware rule. So the offload
    runs in the same slot, right after the parked cut is applied, and reads
    the same facts (`updated_at`, `last_prefix_key`, `last_input_tokens`).
    One decision point for every prefix mutation.
22. **The offload is the restore transformation, applied early.** A block is
    replaced by exactly what PR-3 would render for it on a cold restore
    (same matcher, same persisted digest, same renderer). That is what
    makes it byte-stable: after an offload, the live prefix and the next
    restore agree on that block. Unmatched blocks stay inline on the live
    path (restore has to drop bytes; offload does not).
23. **Pinning is a pure function of the message list plus the incoming
    prompt**, not Strands' `pin_message` metadata. Our slice is not
    `SummarizingConversationManager`, and message metadata is persisted to
    AgentCore Memory — writing pins there would alter stored bytes. Rules:
    on the attach turn and the next (`DOCUMENT_OFFLOAD_PIN_TURNS=2`); the
    incoming or previous prompt contains the document's name stem; a
    `document_read` result for it sits in the recent turns.
24. **Slices are aged on both paths.** `document_read` page slices older
    than `DOCUMENT_SLICE_MAX_TURNS` become a deterministic stub on the live
    path (same gate) and on restore, so a warm agent and a cold restore
    agree. The stub is a function of the block's unique name, so it is stable.
25. **Rollout is a bucket, not a boolean.** `DOCUMENT_OFFLOAD_ROLLOUT_PERCENT`
    (default 100) over `crc32(session_id) % 100` gives the evaluation spec
    its concurrent B/C arms; `DOCUMENT_OFFLOAD_ENABLED=false` is the kill
    switch. `crc32`, not `hash()`, because the latter is salted per process.
26. **Ceiling without a policy snapshot.** When the compaction state has no
    `policy.ceiling` yet, the reason falls back to
    `CompactionPolicy.resolve(config, None)` (the fixed threshold) — a
    conservative ceiling, so `over_ceiling` never fires early.

**PR-5 (2026-09-16)**

27. **Nothing to fix; something to prove.** The July finding was a real
    defect in the pre-stack code (truncation ran at every `initialize()`,
    and the bypass path rebuilt the agent every turn). The compaction stack
    made the anchor a pure function of persisted state that moves only with
    a cut or a cold cache, so the guard now holds by construction and by
    test. PR-5 therefore adds the ledger event and metric that turn "holds by
    construction" into a standing measurement, rather than re-implementing
    the guard under a new name. The gap on the event is measured *before* the
    advance saves state, because the save re-stamps `updated_at` and would
    otherwise read as zero.

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
