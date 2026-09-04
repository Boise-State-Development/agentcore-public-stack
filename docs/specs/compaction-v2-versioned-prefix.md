# Compaction v2 — the prefix as an append-only ledger with versioned, frozen rewrite events

**Status:** Design draft — **gated on the §4.1 cohort numbers from
`docs/specs/compaction-over-threshold-cache-spiral.md` (PR #833)**. Do not
branch until the go/no-go criteria in §7 are evaluated. The triage PRs in that
spec and the `extra_tools` bypass fix (PR #834) land first regardless of this
document's fate.
**Motivating pattern:** four compaction incidents in twelve months with one
shared root — #751 (state clobbered across agent instances), the
sliding-window per-turn cache buster (fixed by the truncation-anchor
redesign), "compaction cannot evict documents" (document-offload defect 2),
and the 2026-08-05 over-threshold spiral ($27 of one user's $30 quota spent on
cache re-writes). Each fix patched a leak; this spec addresses the pipe.
**Related:** `docs/specs/compaction-over-threshold-cache-spiral.md` (triage +
the measurement this depends on) · `docs/specs/agent-cache-extra-tools-bypass.md`
(prefix assembled once per session, not per turn) ·
`docs/specs/document-context-offload.md` (payload escalation target) ·
`docs/specs/session-workspace-tools.md` (the retrieval primitive) ·
`docs/specs/document-offload-evaluation.md` (quality-veto harness, reused in §8)

---

## 1. The premise that changed

Compaction v1 optimizes **token count**: keep the prompt under
`COMPACTION_TOKEN_THRESHOLD` (100k). That objective predates the platform's
prompt-cache economics. Under Bedrock caching, the marginal costs invert:

| operation | cost at a 200k-token prefix (Sonnet 5 pricing snapshot) |
|---|---|
| **keep** history for one turn (cache read) | ~$0.04 |
| **change** history once (full re-write) | ~$0.50 |

Retention is ~12× cheaper than mutation, per event. The scarce resource is not
context tokens — it is **prefix rewrites**. Yet v1's architecture makes the
prefix a *per-restore derivation* over inputs it does not own:

- the summary is a join of AgentCore LTM `ConversationSummary` records that
  grow asynchronously and unboundedly (165k chars in the incident session);
- the checkpoint indexes into a restore window that can slide (frozen
  `original=74` vs. a growing conversation; checkpoint in window coordinates,
  anchor in absolute coordinates);
- the byte-stability contract in `_apply_compaction` is a pure function of
  *(stored history, compaction state)* — but neither input is stable.

Every incident above is that same shape in a different spot. The fix pattern
to date — pin one more input, re-read one more field per turn — accumulates
custody without removing the derivation.

## 2. Design goal, stated as invariants

The prompt sent to Bedrock is always:

```
[system] [tools] [compacted segment vN] [live tail] [current turn]
```

with these invariants, which together make the v1 failure class structurally
impossible rather than carefully avoided:

- **I1 — Frozen segment.** `segment vN` is computed once, persisted as bytes,
  and loaded verbatim on every subsequent restore. It is never re-derived, and
  no external store (LTM, restore window) can change it after it is written.
  Version advance (vN → vN+1) is the *only* mutation the pre-tail prefix ever
  experiences.
- **I2 — Append-only tail.** Between version advances, the live tail only
  appends. Restore must reproduce the tail byte-identically from the event
  store; any repair pass (tool pairing, empty-block sanitize, document strip)
  runs at **write time or version-advance time**, never per-restore.
- **I3 — Hysteresis.** A version advance must land the total context at or
  below a *target* (`COMPACTION_TARGET_TOKENS`, e.g. threshold/2.5 ≈ 40k) —
  not merely below the threshold. If the summarizer cannot reach the target
  within its budget, compaction **escalates** (I5) instead of retrying next
  turn. "Threshold exceeded" firing twice in a row is a bug by definition.
- **I4 — Paid when free.** Version advances prefer turns where the prefix
  re-write is already being paid: the first turn after a cache-TTL-expired
  gap (observable via `cacheGapSeconds`), or a turn whose prefix changed
  anyway (model switch, `@`-mention agent swap). A steady-state warm turn
  never pays a rewrite. Hard ceiling: if context exceeds the threshold by a
  safety factor (e.g. 1.5×) with no free turn arriving, advance anyway.
- **I5 — Bounded summary, escalation not loops.** The segment's summary part
  has a hard token budget. Content that cannot be summarized into budget
  (bulk documents, long verbatim artifacts) is offloaded to a session
  workspace file behind a retrieval tool (`document-context-offload` /
  `session-workspace-tools` machinery) and referenced, not inlined.
- **I6 — Monotone versions.** vN never moves backwards, across any number of
  concurrent agent instances (the #751 class). Version advance is a
  conditional write (`version = vN` → `vN+1`); a losing writer adopts the
  winner's segment. One session, many agents, one ledger.

### Observability falls out for free

The segment version is the fingerprint: every `C#` row records
`(segmentVersion, tailLength)`. Two consecutive rows with the same version and
growing tail **must** be a cache hit beyond the tools/system points; if
`partial_miss` fires there, the defect is localized to the tail by
construction. v1's whole-history hash cannot distinguish "grew" from
"mutated"; v2's fingerprint can.

## 3. What a version advance does

Runs post-turn (same lifecycle slot as v1's `update_after_turn`), only when
I3's trigger and I4's scheduling agree:

1. Split the current ledger at a tool-pair-safe boundary, protecting the last
   K turns and any pinned messages.
2. Produce the new summary with a **dedicated, budgeted summarization call**
   (cheap model; explicit output budget; prompt tuned for the quality families
   in §8). Inputs: segment vN's summary + the messages being retired. LTM
   records may *inform* this call; they are never injected verbatim.
3. Apply I5 escalation for anything that won't fit.
4. Persist segment vN+1 (summary message + retained boundary metadata) with
   the I6 conditional write; emit the `compaction` SSE event (existing UX,
   #243, unchanged).
5. The *next* model call pays the one re-write. Log it as `rewrite_scheduled`
   vs `rewrite_forced` so I4's effectiveness is measurable.

## 4. Native vs. custom — what to build on

Three candidate foundations were evaluated against the invariants
(strands-agents 1.48.0 and bedrock_agentcore 1.19.0, the pinned versions).

### 4.1 AgentCore Memory native summarization: **retrieval-only — never in the prefix**

The `SUMMARIZATION` strategy is what produced the incident's 165k-char
summary. Its properties are the *opposite* of I1: extraction is asynchronous
(content changes at times we don't control), record count grows unboundedly,
and output shape (`<topic>` logs) is append-oriented, not compressive. These
are fine properties for what it actually is — a cross-session **retrieval**
store — and disqualifying for prefix injection. v2 demotes all three
strategies (Summary, Semantic, UserPreference) to retrieval/inputs:
`list_memory_records` may feed the §3 summarization call, and semantic/user
records stay available to the memory tools, but no LTM record text ever
enters the cacheable prefix directly. (This also subsumes spiral-spec PR-4's
system-prompt pinning rationale.)

AgentCore Memory **event branches** (`create_event(branch=...)`) were
considered for persisting segments as forked histories rooted at a summary
event. Rejected: it couples segment identity to the event store's semantics,
makes I6's conditional write awkward (branches have no compare-and-set), and
buys nothing over a small DynamoDB item + S3 blob we already know how to
version. Events remain what they are today: the append-only source of truth
for the raw conversation (I2's substrate).

### 4.2 Strands native conversation management: **adopt the machinery, not the policy**

Strands 1.48.0 ships more than v1 used:

- `SummarizingConversationManager` — replaces the oldest `summary_ratio` of
  messages with a single summary message; supports a dedicated
  `summarization_agent`, `preserve_recent_messages`, `pin_first`; and —
  decisive for I1 — **persists the summary message verbatim in conversation-
  manager state** (`get_state`/`restore_from_session`), round-tripped by the
  session manager. The summary is stored bytes, not a re-derivation. Between
  events, `agent.messages` is append-only. This is the frozen-segment idea,
  already implemented and maintained upstream.
- `conversation_manager/compression/` — `adjust_split_point_for_tool_pairs`
  (the boundary problem we hand-rolled in `_find_valid_cutoff_indices` and
  paid for again in `_repair_tool_pairing`), message pinning via
  `metadata.custom.pinned` (I1's "protect this" primitive), and shared
  summary-generation helpers. The module docstring signals upstream is
  actively building toward model-driven compaction — this rail is maintained
  and moving in our direction.

What it does **not** provide: the trigger policy (its default is reactive —
summarize on `ContextWindowOverflowException` — with proactive
`reduce_context(e=None)` available but unscheduled), token-threshold/
hysteresis logic, cache-TTL-aware scheduling (I4), the summary budget and
escalation (I5), cross-instance version monotonicity (I6), and the
fingerprint observability. It also has no opinion about our restore
sanitizers or the document strip.

### 4.3 Fully custom (v1's path): **rejected**

v1 *is* the custom option, and its incident history is the argument. The
custody burden is concrete: we own tool-pair boundary logic, summary
generation, state persistence, restore re-derivation, and their interactions
with two SDKs that keep evolving underneath — and every one of those seams has
produced at least one prod incident. Rebuilding v2 fully custom rebuilds the
seams.

### Verdict

**Hybrid: Strands machinery under a thin custom policy layer.**
`SummarizingConversationManager` (or a subclass) becomes the mutation engine —
split points, pinning, summary-message persistence, session-state round-trip —
and the platform owns exactly four things: (a) the trigger/hysteresis/
scheduling policy (I3, I4), (b) the summary budget + offload escalation (I5),
(c) the I6 conditional-write versioning around the engine's persisted state,
and (d) fingerprint observability. AgentCore Memory strategies retire to
retrieval-only. `TurnBasedSessionManager` shrinks: `_apply_compaction`'s
checkpoint/anchor slicing, `_retrieve_session_summaries`, and
`_prepend_summary_to_first_message` are deleted rather than fixed — the
byte-stability fix in spiral-spec PR-3 becomes "the tail restores
byte-identically," a much smaller claim.

Risk to carry: we are coupling to an upstream module that is newer than the
parts of Strands we already depend on. Mitigations: exact version pins (repo
rule), the #741 aliasing guard test extended to conversation-manager state,
and the §8 harness re-run on every Strands bump that touches
`conversation_manager/`.

## 5. Migration from v1

- A session's existing `compaction` map (checkpoint / anchor / summary)
  converts on first post-deploy turn: persisted summary → segment v1 verbatim
  (no re-summarization — conversion must not change model-visible bytes on a
  warm cache), checkpoint/anchor → the segment boundary. One conditional
  write; idempotent; the conversion turn is I4-scheduled like any advance.
- Sessions with no compaction state start at v0 (empty segment) and behave
  append-only until first advance.
- v1 code paths stay behind the existing `COMPACTION_ENABLED` flag family;
  v2 ships default-on with its own kill switch
  (`AGENTCORE_COMPACTION_V2_ENABLED=false` reverts to v1, not to nothing),
  per the platform's flag convention.

## 6. What this absorbs and retires

| today | under v2 |
|---|---|
| spiral-spec PR-2 (summary cap) | subsumed by I5 — implement PR-2 anyway as triage; its budget constant carries over |
| spiral-spec PR-3 (byte stability) | shrinks to tail-restore determinism |
| spiral-spec PR-4 (memory pinning) | subsumed by §4.1 retrieval-only rule |
| truncation-anchor opportunism | generalized into I4 |
| document-offload "strip on restore" | escalation target of I5 (offload, don't strip) |
| LTM summary join in the prefix | deleted |

## 7. Sequencing and go/no-go

**Hard prerequisites (in order):** spiral-spec PR-1 (`partial_miss` — the
instrument), the `extra_tools` bypass fix (prefix assembly once per session;
without it, v2's per-restore guarantees are exercised 30× more often than
designed), spiral-spec PR-2 as triage, and the §4.1 cohort re-scan from the
spiral spec.

**Go criteria (any one suffices):**
- over-threshold sessions (>100k `lastInputTokens`) exceed ~3% of active
  sessions or ~15% of fleet spend in the cohort scan; or
- post-bypass-fix, `partial_miss` waste attributable to compaction-state
  mutation still exceeds ~$50/month fleet-wide; or
- the 1M-context model mix pushes median long-session context high enough
  that v1's fixed threshold fires for >10% of weekly-active users.

**No-go / defer:** if the cohort is a corner case after the triage PRs land,
v2 stays a design doc and the invariants become review criteria for any
future compaction change ("does this mutation own its inputs?").

## 8. Evaluation

> **Amended 2026-08-12.** The quality-veto harness referenced here is now
> partly off-the-shelf: `agentcore-evaluations-spike-findings.md` verified that
> the managed `bedrock-agentcore` evaluation service supplies the judges,
> tool-trajectory scoring and result plumbing on our existing pin, with no
> infrastructure. The paired arms, corpus and statistics remain ours, and the
> **blinded** holistic judge cannot run through the managed path at all (spike
> §2). This does not change what v2 must prove — only how much of the
> instrument has to be written.

Reuses the spiral spec's §4 machinery wholesale: arm-separated cost
attribution (v1-triaged vs. v2 as a new arm B4 on the same `partial_miss`
instrument and replay harness) and the **quality-veto** long-session eval
(constraint retention / revision continuity / reference lookup) — v2's
budgeted summarizer is a bigger context change than PR-2's cap, so the veto
applies with full force. One addition: an **advance-cadence** metric (version
advances per session-day, `rewrite_scheduled` vs `rewrite_forced` ratio) with
an alarm on any session advancing more than ~2×/day — the v2 expression of
"the spiral is structurally impossible" gets verified, not assumed.

## 9. Non-goals

- Changing the Bedrock cachePoint layout (tools/system/auto) — v2 changes
  what the auto point covers, not the layout.
- Cross-session memory quality (Semantic/UserPreference extraction) — only
  their *injection point* moves; their content and UX are untouched.
- The quota system, the idle reaper, model routing.
- Upstreaming the policy layer to Strands — worth proposing once proven, but
  the design must not depend on upstream accepting it.
