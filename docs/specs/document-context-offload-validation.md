# Validation report — document-context-offload & agent-cache-extra-tools-bypass

**Scope:** adversarial re-verification of every headline claim in
`docs/specs/document-context-offload.md` and
`docs/specs/agent-cache-extra-tools-bypass.md`.
**Method:** independent content-free re-scan of prod (profile `prod-ai`,
2026-08-03 ~21:40 UTC — a few hours after the original scan, so counts drifted
slightly upward: 19,137 `C#` rows vs 18,942; 897 FILE rows vs 882; 434
attachment sessions vs 431), plus line-level verification of every code
reference on `develop` (HEAD `79eb80a9`) and content inspection of
`origin/main` (`3921f2d4`, Release/1.13.0), plus source reading of the pinned
`strands-agents==1.48.0` and `bedrock_agentcore` (1.19.0; the repo pins no
version) from the uv cache.

Shipped-code checks used `git show origin/main:<path>` content inspection, not
merge-base ancestry. The AgentCore blob-fallback claim was re-derived from SDK
source, not taken on faith. Analysis scripts: session scratchpad
`analyze.py` / `analyze2.py` (DynamoDB dumps were projected content-free:
no `citations`, no `displayText`, no `D#` rows, no `S#.title`).

---

## Verdict table

| # | Claim | Verdict |
|---|-------|---------|
| 1 | 11% of sessions / 31% of spend; means $0.620 vs $0.176; 47 of top-100 | **Confirmed** |
| 2 | cacheWrite-dominated decomposition; uncached input *lower* for attachment sessions | **Confirmed** |
| 3 | Cold re-write cliff at the 5-min TTL; $188/$747 fleet envelope | **Confirmed with caveats** (definition-sensitive; >15 min is ~55–75 % cold, not ~100 %; $ envelope reproduces at $160–173, not $188) |
| 4 | 1h TTL a net loss: +12.3 % blanket / +5.7 % / +4.2 % / −8.7 % oracle | **Confirmed for blanket & oracle; conditional-policy magnitudes not reproduced** (my re-implementation gets ≈ break-even) |
| 5 | 79.6 % of attachment / 76.3 % of all sessions bypass the agent cache | **Confirmed** |
| 6 | 14 % of attachment sessions re-upload the same filename | **Confirmed, and strengthened** (87 % of duplicate groups are byte-size-identical) |
| 7 | 4 of 615 upload clusters exceed the ~7.5 MB event bound | **Confirmed with caveats — the true rate is *higher*, not lower** (8–9 of ~500–620 under a better proxy) |

Both specs' central defects (`_strip_document_bytes` placeholder strip;
`extra_tools` cache bypass) are **live in `origin/main`**, byte-identical to
develop (develop line numbers are shifted −9 in `turn_based_session_manager.py`
by a comment-only PR #831 change; `service.py` is byte-identical).

---

## Claim-by-claim detail

### Claim 1 — 11 % of sessions, 31 % of spend: **confirmed**

Re-derived: 398/3,557 sessions (11.2 %) with uploads; $248.69 of $809.08
(30.7 %). Mean $0.625 vs $0.177 (medians $0.161 vs $0.048); calls/session 10.1
vs 4.8; output tok/call 1,397 vs 727; **exactly 47** of the top-100 sessions
carry files. Per-type means also reproduce: tabular-only $1.04, non-PDF docs
$0.63, PDF $0.59, images $0.29, none $0.18. Peak-context >100k: 9.8 % vs 2.2 %.

Two bookkeeping defects in the spec, neither fatal:

- **The spec quotes three different attachment-spend figures** ($244.91 in §1,
  $233.61 in §6) **and two fleet totals** ($746.98, $798) without saying why.
  The reconciliation: ~$51 of fleet spend (and ~$11 of attachment spend) sits on
  legacy rows where `cost` is a bare float with no breakdown. $746.98/$233.61
  are map-rows-only; $798/$244.91 include the floats. Any figure derived from
  the breakdown (`cacheWriteCost` etc.) silently excludes the float rows. The
  spec should state its denominator once and use it consistently.
- **Missing-cost rows are 11.8 % (2,257/19,137) and are *not* random** — they
  concentrate in 2026-04 (58 % of April rows) and 2026-05 (33 %). 2,242 of them
  do carry `tokenUsage`; repricing them at snapshot/list prices adds ≈ $175 of
  unrecorded fleet spend. This means **absolute fleet totals are understated
  ~20 %**, but the headline is robust: the corrected attachment share is
  29.6 % vs 30.7 % uncorrected. `AdminUsageAggregates` (the known 2.6×
  triple-counter) was not used; the raw `C#` rows with both cost shapes handled
  are the right source and the original scan used them.

### Claim 2 — cacheWrite-dominated, input-not-document driven: **confirmed**

Attachment cohort (map rows): cacheWrite 50.0 % / output 22.5 % / input 20.5 %
/ cacheRead 7.0 % (spec: 47.9/21.5/19.9/6.2 — drift consistent with a few
hours of new traffic). Uncached input per call is indeed *lower* for attachment
sessions: 7,268 vs 8,080 (spec: 7,455 vs 8,130). The inversion — the cohort
with big documents pays *less* per-call uncached input — is real and is the
strongest single piece of evidence that the cost driver is prefix re-writes,
not document tokens on the attach turn.

One mechanism the spec missed that *supports* it (found in SDK source,
`strands/models/bedrock.py:409–458`): with `CacheConfig(strategy="auto")` the
message cache point is injected at the end of the **last user message**, but is
moved to sit **before the first non-PDF document block** in that message — and
if that block is at index 0, no message cache point is injected at all for the
turn. Since `PromptBuilder` appends text first and documents after, a DOCX/TXT
/HTML attach turn caches only up to the text block; the document bytes enter
the cached prefix one turn later. (Also: the repo `CLAUDE.md` says auto-caching
injects "at the end of the last assistant message" — wrong; it is the last
*user* message.)

### Claim 3 — cold re-write vs gap, 5-min TTL: **confirmed with caveats**

The cliff at 5 minutes is unambiguous under every definition I tried:

| definition | <1min | 1–5min | 5–15min | >15min |
|---|---|---|---|---|
| `cw>cr` | 5.0 % | 17.3 % | 73.3 % | 75.6 % |
| `cw>0 ∧ cr==0` | 3.2 % | 10.3 % | 63.9 % | 66.6 % |
| `cw ≥ 80 % of prev prefix` | 4.8 % | 11.6 % | 63.0 % | 63.2 % |
| `cacheStatus` cold (post-#697 rows only) | 0.5 % | 1.3 % | 53.1 % | 55.3 % |

Caveats:

- **The spec's exact numbers (3.4/16.3/66.1/~65) are definition-dependent and
  the definition is not written down.** They fall inside the band above, so the
  claim stands, but it is not reproducible to the decimal. The spec should
  state the predicate.
- **">15 min ≈ 65 %" deserves its own explanation, which the spec doesn't
  give:** if the 5-minute TTL were the whole story, >15 min would be ~100 %
  cold. It is 55–75 % because the tools/system cache points are shared across
  sessions with the same config (fleet traffic keeps them warm), and
  classification mixes prefix levels. "Cold *full-prefix* re-write" therefore
  overstates what the >5 min rows are; a substantial minority still read a
  shared partial prefix.
- The fleet envelope reproduces at **$159.93–172.62** depending on definition
  (spec: $188 of $747, 25 %; mine is 20–21 % of $809). Part of the gap is the
  float-cost rows, which carry no `cacheWriteCost` and drop out of my sum. Same
  story for the attachment envelope: **$58.47–63.57** vs the spec's $66.24.
  Right order, ±15 % on the magnitude.

### Claim 4 — 1h TTL is a net loss: **blanket and oracle confirmed; conditional policies not reproduced**

I re-implemented the counterfactual from scratch (1h writes at 2× base input vs
1.25×; cold re-writes with gap ∈ (5 min, 1h] converted to cache reads):

| policy | spec | my re-implementation |
|---|---|---|
| blanket 1h | +12.3 % | **+6.6 %** |
| 1h after one >5 min gap | +5.7 % | **−0.2 %** |
| 1h after two >5 min gaps | +4.2 % | **−0.4 %** |
| perfect oracle | −8.7 % | **−9.9 %** |

Blanket-1h-is-a-loss and oracle-upside-is-small both reproduce, and those are
the two numbers §7 actually needs. But the conditional policies land at
break-even in my model vs clearly-negative in the spec's, and neither model is
documented enough to arbitrate. **The honest statement for §7 is: blanket 1h is
confirmed a loss; heuristic policies are somewhere between −1 % and +6 % —
i.e., not worth building, but not "independently confirmed a loss" either.**
The conclusion (don't do 1h TTL) survives; the precision implied by "+5.7 %"
does not. Note both simulations share an unstated assumption: session
trajectories wouldn't change under a different TTL.

### Claim 5 — cache bypass reach: **confirmed**

76.5 % of sessions (2,735/3,574) have ≥1 injected tool enabled; 80.7 % of
attachment sessions (342/424). Per-tool counts match (`analyze_spreadsheet`
2,678, `list_spreadsheets` 2,671, `create_artifact` 974). Bypassed-cohort
spend $718.26 — 94.8 % of map-row spend, matching the spec's "95 %" (which is
map-only; against the all-rows total it is 88.8 % — same denominator issue as
claim 1). Within-TTL (60–300 s) cold-write rates: bypassed 19.5 % vs cacheable
6.1 % under `cw>cr` (spec: 11.2 % vs 5.8 % under its undocumented definition —
the ~2–3× ratio is stable even though the absolute rates aren't).

The denominator zoo across the two specs (395 vs 426 vs 431 attachment
sessions) reconciles cleanly and should be footnoted: 431→434 = sessions in the
uploads table; 426→424 = those with an `S#` row; 395→398 = those with `C#` cost
rows; 8 upload sessions have neither.

### Claim 6 — 14 % re-upload rate: **confirmed and strengthened**

63/434 sessions (14.5 %) upload the same filename ≥2× (group sizes 2–6). The
"maybe they're revised versions" objection now has an answer the original scan
didn't have: **86 of 99 duplicate groups (87 %) have byte-identical
`sizeBytes`**, and the median span from first to last duplicate is **200
seconds** (p90 ≈ 82 min). A user attaching a revised document produces a
different byte size essentially always; a user re-attaching because the model
lost the file produces an identical one minutes later. This is still
correlational — but it is now the *strip-loss* story that fits the data and the
*revision* story that doesn't. (No content hash exists on `FileMetadata` to
settle it outright; worth adding one in PR-7.)

### Claim 7 — over-quota turns: **confirmed with caveats, in the opposite direction from the spec's hedge**

The 120 s clustering reproduces (624 clusters, 7 > 7.5 MB = 1.1 %, 7 clusters
>5 files, max 29.89 MB). The spec worried its proxy *merges* turns and
therefore overstates the rate. I built the better proxy it asked for — group
each upload by the **next `C#` model call** in the same session (uploads
between two calls belong to the message that precedes the second call):

- 622 call-aligned groups; **9 exceed 7.5 MB** (1.4 %) — 8 after excluding
  tabular files that never reach the message (10.3/8.2/9.6/13.5/16.3/7.7/8.7/
  7.5 MB).
- Several over-quota groups have only **3–4 files** — inside the SPA's 5-file
  cap. The 29.89 MB 120 s cluster splits into smaller per-call groups (max
  16.34 MB), so merging did inflate the *maximum*, but the *count* of
  over-quota turns goes up, not down.

So the caveat cuts the other way: **~0.7 % was an undercount; ~1.3–1.4 % of
attachment turns risk the 10 MB event quota**, and the SPA cap does not protect
against it. Remaining limitations of both proxies: an upload isn't proof the
file was sent, and sparse call timelines can still merge. The real fix is a
`messageId` on `FileMetadata` (PR-7 material) — the table has no turn
attribution at all today.

Related code findings that sharpen PR-6:

- The backend `max_files_per_message` setting (env-plumbed through CDK,
  default 5) is **dead code** — assigned in `files/service.py` and never read.
- The `file_upload_ids` resolver caps at 5 by **silently truncating**
  (`upload_ids[:max_files]`) — files 6+ are dropped with no user feedback.
- The direct inline-`files` request path has **no count cap at all**
  server-side (per-file 4 MB is enforced; count and aggregate bytes are not).
- The SDK re-check: neither `strands` 1.48.0 nor `bedrock_agentcore` 1.19.0
  references the 10 MB event bound anywhere — it is server-side only, and the
  batched flush path concatenates a session's buffered messages into one
  `create_event` with no aggregate check, so batching can blow the quota with
  individually-legal messages.

---

## The two prior reasoning errors — re-checked correctly

1. **Shipped-to-prod**: verified by content on `origin/main`. Both defects are
   live in prod. `§4E`'s "line 214" is the `origin/main` line number; on
   develop it is line 223 (comment-only drift from PR #831). The spec should
   cite develop's numbers.
2. **Blob fallback**: independently confirmed in SDK source.
   `exceeds_conversational_limit` (`CONVERSATIONAL_MAX_SIZE = 100000`,
   `bedrock_converter.py`) routes oversized messages to a `blob` payload
   (`session_manager.py:612–631`), and the read path decodes it back through
   `SessionMessage.from_dict` → `b64decode` (`bedrock_converter.py:85–97`,
   `strands/types/session.py:28–55`). A 4 MB document round-trips intact. Two
   precision notes for §4E: the 100 KB check measures the *base64-inflated
   JSON*, so the blob path actually triggers at ≈ 72 KB of raw bytes; and blob
   decode failures are swallowed (`logger.error`, message dropped) — corruption
   presents as missing history, not an exception.

---

## New defects found in the specs (things to fix before circulating)

1. **Citations are not enabled today — anywhere.** The document block built by
   `document_handler.py:84–92` has exactly three keys (`format`, `name`,
   `source.bytes`); no request-side citations config exists in the repo. The
   offload spec's lifecycle diagram labels the attach turn "citations on ←
   unchanged", which is false — enabling citations is *new work*, not preserved
   behavior. And the spec's citation of `_BEDROCK_CONTENT_BLOCK_KEYS`
   containing `citationsContent` as supporting evidence conflates a
   *response-side* history-sanitizer discriminator with request-side config.
   Downstream consequence: whether prod PDFs currently get visual (page-image)
   understanding at all is **unverified** — on Bedrock, full visual PDF
   processing is tied to the citations-enabled document path. The evaluation
   must establish the *current* fidelity baseline before scoring the digest
   against it (see the evaluation spec §2).
2. **The bypass spec's §6 isolating experiment is not clean — it is nearly
   powerless.** 921 of 974 `create_artifact` sessions *also* have spreadsheet
   tools enabled, so "enable caching for `create_artifact` only" changes the
   caching behavior of the **53 sessions** (1.5 % of fleet) where it is the
   sole injected tool — a tiny, self-selected cohort. The clean experiment must
   treat the spreadsheet builders (1,761 sessions have *only* spreadsheet tools
   injected), which requires keying `assistant_id` into the cache key first —
   i.e., the experiment is gated on part of the fix it was meant to justify.
   Alternative that avoids the confound entirely: measure `initialize()`
   invocations per turn and p50 TTFT under a randomized per-session flag; the
   latency claim is causal under randomization regardless of workload mix.
3. **Line-number and location nits**: `INJECTED_TOOL_IDS` is at
   `injected.py:48` (line 43 is `WORKSPACE_TOOL_IDS`);
   `WORKSPACE_READ_MAX_BYTES` lives in `apis/shared/files/workspace.py:41`, not
   `workspace_tools.py`; "MAX_FILES_PER_MESSAGE exists only in the SPA" is
   imprecise — a backend count cap exists but is dead code, a per-file 4 MB cap
   *is* enforced server-side, and the resolver path truncates at 5.
4. **Minor data defect noticed in passing**: `FileMetadata.createdAt` is
   malformed ISO — `...+00:00Z` (offset *and* Z suffix). Harmless to humans,
   breaks strict parsers.

## What was not checked, and why

- **The prior session's exact cold-write predicate and TTL-simulation code** —
  not recorded anywhere I could find; I rebuilt both from the spec text, which
  is why claims 3/4 carry definitional error bars.
- **Whether Bedrock Converse accepts `context_management` via
  `additionalModelRequestFields`** — the spec flags this as an open question;
  verifying it requires a live Bedrock call, out of scope for a read-only
  validation pass.
- **Actual visual fidelity of prod PDF handling** (dual-encoding) — requires a
  live model probe; folded into the evaluation design as a required baseline
  experiment instead.
- **Whether every upload was actually sent as a message attachment** — no
  turn/message attribution exists in the uploads table; both claim-7 proxies
  inherit this.
