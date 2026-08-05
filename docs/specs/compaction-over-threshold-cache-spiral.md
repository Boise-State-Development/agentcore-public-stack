# Compaction over-threshold cache spiral — stop paying a full prefix re-write on every turn

**Status:** PR-1 shipped (#838, merged 2026-08-05 — see "As shipped" under §3
PR-1, which differs from what this section originally specified). PR-5 built
2026-08-05 (quota runway — see "As shipped" under §3 PR-5; its acceptance
replay moved one of §3's own numbers). PR-2 through PR-4 unbuilt.
**Motivating incident:** prod, 2026-08-05 analysis. One faculty user exhausted
their $30/month quota in 5 days on a **single conversation** (session
`c94a3172-e1fb-4a1d-b375-6e51a56c75ad`, an essay-editing session created
2026-07-30). August: 56 model calls, $30.45 total, of which **$27.39 (90%) was
Bedrock cache writes** — every turn re-wrote the full ~200k-token prefix at the
$2.50/MTok write premium while reading only the ~11k tools+system segment.
Observability recorded `cacheStatus="hit"`, `wastedUsd=0` on all 56 calls.
**Related:** `docs/specs/agent-cache-extra-tools-bypass.md` (**dependency** —
the bypass is why this session restored every turn; see D3 and §3 sequencing),
`docs/specs/document-context-offload.md` (same tenet: nothing unbounded in the
prefix; this incident is the **attachment-free** counterexample — zero
`user-file-uploads` rows for the session — proving the prefix-rewrite spiral is
not document-specific), `docs/specs/document-offload-evaluation.md` (the
quality-veto evaluation pattern §4 reuses), `docs/specs/quota-cooldown-windows.md`
(the quota backstop this incident tripped), the compaction byte-stability
redesign (truncation anchor), and the 2026-07-27 fleet measurement that
`cacheStatus` masks partial prefix misses (write:read 1:2 fleet-wide).

---

## 1. The incident, measured

All figures from the session's `C#` rows in `boisestateai-v2-sessions-metadata`
and the runtime log group (`…h4MSyY7YSh-DEFAULT`), account 897729136999.

| | July (same user) | August (5 days) |
|---|---|---|
| requests | 78 | 56 (100% one session) |
| total cost | $7.41 | $30.45 |
| cache write tokens | 2.0M | **10.95M** |
| cache read tokens | 3.4M | 0.6M |
| write:read | 0.59 (healthy) | **18 : 1** |
| cost share: cache writes | — | **90%** |

Per-call shape, stable across all 56 calls — including calls **60–120 seconds
apart** with identical `toolConfigHash` and `systemPromptHash`:

- `cacheReadInputTokens` ≈ 11,278 (tools + system segments only — constant)
- `cacheWriteInputTokens` ≈ 170k–212k (the entire conversation history)
- `cacheStatus = "hit"`, `wastedUsd = 0`
- fingerprint `messageCount` frozen at 41 while the conversation kept growing

Runtime logs show, before **every** turn:

```
Compaction initialized: stage=checkpoint, original=74, final=40, anchor=68
Retrieved 10 memories from namespace: /strategies/ConversationSummary-…
Threshold exceeded: ~214,000 > 100,000
```

The session sits permanently at ~205–215k input tokens against
`COMPACTION_TOKEN_THRESHOLD = 100_000`, and compaction never gets it back
under — because the "summary" it prepends is itself **164,991 characters
(~40k tokens)**: a join of the session's AgentCore LTM `ConversationSummary`
records, which log every edit in `<topic>` blocks rather than compressing.

Counterfactual with a stable prefix: 8 of the 56 calls followed a real >1h gap
(TTL-cold, re-write legitimate, ~$4 total); the other 48 should have been
~$0.11 each (read 200k @ $0.20/M + delta write + output) instead of ~$0.55.
The month prices out at **~$10, not $30.45 — roughly $20 was avoidable waste,
paid by one user's quota.**

## 2. The defects

Five distinct problems compound. Each is independently fixable.

### D1 — `cacheStatus` calls a 95% prefix miss a "hit" — FIXED (#838)

As it stood in
[prompt_cache.py](../../backend/src/apis/shared/observability/prompt_cache.py):

```python
if cache_read_tokens > 0:
    return CacheStatus.HIT
```

Any nonzero read — here the 11k tools+system segment — classifies the call as
`HIT`, so `compute_wasted_usd` returns 0 and no EMF metric fires. 56 calls
writing 190k tokens apiece against an 11k read never registered as waste. This
is the exact blind spot identified in the 2026-07-27 fleet measurement; this
incident is its worst single-session expression.

### D2 — the compaction summary is a log, not a compression, and is unbounded

`_retrieve_session_summaries`
([turn_based_session_manager.py:612](../../backend/src/agents/main_agent/session/turn_based_session_manager.py:612))
fetches up to `maxResults=100` LTM records and `update_after_turn` joins **all
of them** into `compaction_state.summary`
([turn_based_session_manager.py:789](../../backend/src/agents/main_agent/session/turn_based_session_manager.py:789)),
which `_prepend_summary_to_first_message` injects into the first restored user
message on every restore. AgentCore's summarizer appends topic blocks per
extraction run, so the join grows monotonically for the life of the session.
At 40k tokens the summary alone is 40% of the threshold it exists to get
under — the session can *never* compact below 100k, so `Threshold exceeded`
fires every turn forever.

### D3 — the restored prefix is not byte-stable turn-to-turn

Proven by the token accounting (11k reads at 60s gaps, same tool/system
hashes), not yet root-caused to the byte. Two anomalies point the way:

- `Compaction initialized: original=74` is **frozen across turns that are
  completing** (session metadata says 94 messages). The restored event window
  and the live conversation have diverged; whatever window the SDK returns is
  not append-only from the model's point of view.
- The persisted `checkpoint` (34) is in **restored-window coordinates** while
  `truncation_anchor` (68) is absolute — `_apply_compaction`
  ([turn_based_session_manager.py:267](../../backend/src/agents/main_agent/session/turn_based_session_manager.py:267))
  documents a byte-stability contract that is a pure function of *(stored
  history, compaction state)*, but the stored-history input itself slides.

**Why the unstable derivation runs every turn at all:** this session has
`analyze_spreadsheet` / `list_spreadsheets` in `enabledTools`, which puts it in
the **76% `extra_tools` agent-cache-bypass cohort**
(`docs/specs/agent-cache-extra-tools-bypass.md`) — every turn builds a fresh
`Agent`, runs a full `initialize()`, and re-derives history from an AgentCore
Memory restore. On a warm cached agent, `agent.messages` appends in memory and
is append-only by construction; the restore derivation would only be exercised
on genuine cold starts. The bypass converts a cold-start-only defect into an
every-turn tax. Both fixes are needed: the bypass fix makes instability *rare*,
the byte-stability fix makes it *harmless*.

### D4 — a secondary buster: the system prompt mutates mid-session

`systemPromptHash` changed between each day's visit *and* twice mid-burst
(cache read dropped 11,278 → 8,929 both times). The memory-augmented system
prompt (UserPreference / SemanticFact retrievals, 10 records each, per turn)
re-writes the system+history segments whenever extraction lands new records —
including records extracted *from the very conversation in progress*. Small
next to D2/D3, but it breaks the prefix at an earlier cachePoint when it fires.

### D5 — quota warnings gave the user no runway — FIXED (PR-5)

All four warning events (80%, 90%) fired on Aug 4 — the same day the `block`
landed. Tier config (`softLimitPercentage: 80`) means a user in a pathological
session gets hours of notice on a month-long budget. Nothing surfaces "this
one conversation has cost $28."

Fixed by the ladder + `quota_session_notice` in §3 PR-5. Replayed against the
incident's own rows, the notice reaches the user on Aug 1 — 3.9 days before
the block, and 2.5 days before the earliest per-user warning. The extra 50%/75%
rungs, by themselves, would only have bought ~6 hours here (see "As shipped"
point 2); the per-session signal is the one that carries the runway, because
the defect was never that the *user* was overspending.

## 3. Fix plan

Five PRs, ordered so measurement lands first and each subsequent PR's effect is
visible in the metrics the first one adds. PR-1 and PR-2 are small and end the
bleeding; PR-3 is the investigation-shaped one.

**Sequencing against the `extra_tools` bypass fix:** the bypass fix (specced
separately) is the highest-leverage single change for *this* failure mode —
it stops the restore path from running per turn on 76% of sessions. It should
land between PR-1 and PR-3: after PR-1 so its fleet-wide effect is measured by
the `partial_miss` metric, and before PR-3 because it changes what PR-3 has to
prove (byte stability then only matters on genuine cold starts, which are
observable via `cacheGapSeconds`). PR-2 is independent of it — an unbounded
summary breaks the threshold math regardless of who assembles the prefix.

That sequencing held: its first arm (`create_artifact` only — that spec's §6
experiment) is open as #839, and PR-1's `partial_miss` is how it gets read.
Note the arm covers artifacts, **not** the `analyze_spreadsheet` /
`list_spreadsheets` pair the incident session actually had enabled (D3), so it
does not yet change this session's turn shape — spreadsheets need the cache key
extended first.

### PR-1 — observability: classify partial prefix misses (ship first)

- In `classify_cache_status`, before the `cache_read_tokens > 0` early return:
  when `previous_call_exists` and `cache_write_tokens > PARTIAL_MISS_RATIO ×
  cache_read_tokens` (start at ratio 3, constant in
  `apis/shared/observability/prompt_cache.py`), classify as new status
  `partial_miss`.
- `compute_wasted_usd` prices `partial_miss` the same way as `miss_avoidable`:
  the re-written portion that the previous call had cached, at (write − read)
  premium. For this incident that computes ≈ $0.43/call instead of $0.
- Emit it through the existing EMF pipeline (`AgentCoreStack/PromptCache`) and
  the session rollups (`avoidableMissCount` gets a sibling
  `partialMissCount`); surface on `GET /admin/costs/sessions/{id}/calls` and
  the anatomy page.
- CloudWatch alarm on session-level accumulation: any session whose trailing
  `partial_miss` waste exceeds $5 in 24h. (This session would have alarmed on
  Aug 1 at ~10 turns.)
- Kill switch: rides the existing `PROMPT_CACHE_OBSERVABILITY_ENABLED` gate —
  no new flag.

**Acceptance:** replaying the incident session's 56 `C#` rows through the
classifier yields ≥ 47 `partial_miss` with wastedUsd ≈ $20 ± 15%; fleet EMF
dashboards show the new status; no change to any request sent to Bedrock.

**As shipped (#838).** The bullets above are the design; three things changed
while building it, and the acceptance criteria only hold *with* those changes.
Recorded here because §3 as originally written would produce a different
classifier — and because PR-2..PR-5 and the §4.2 arms read this instrument.

1. **Two conditions the bullet omits.** The predicate also requires
   `cache_read_tokens > 0` and a same-prefix gap inside the TTL:
   - Without the read condition, `write > 3 × 0` is trivially true, so every
     zero-read call would be stolen from `miss_avoidable`. The *read* is what
     makes a miss partial.
   - Without the TTL gate, a re-write after the entry legitimately expired is
     booked as waste — the #753 mistake that made the previous metric
     untrustworthy. It also breaks the acceptance number: the incident's 8
     post->1h-gap calls join the other 48, giving 56 classifications and ~$24,
     outside the ±15% band. With the gate: 48 and $20.98. The cost of the gate
     is under-reporting when no same-prefix predecessor sits in the 10-row
     lookback (`gap_seconds=None` → stays `hit`), which is the deliberate
     direction.
2. **The session alarm is cumulative-per-session, not trailing-24h.**
   "Any session whose trailing `partial_miss` waste exceeds $5 in 24h" is not
   directly expressible: `sessionId` cannot be a metric dimension (unbounded
   cardinality). Shipped as `SessionPartialMissUsd` — the session's running
   `partialMissUsd`, emitted from the rollup bump's own `UPDATED_NEW` return —
   alarmed on `Maximum > 5` over a 24h period, which reads as "a session at or
   over $5 was active in the last day" and clears when it goes quiet. A Logs
   Insights widget names which session.
3. **Constant is `PARTIAL_MISS_WRITE_READ_RATIO`** (not `PARTIAL_MISS_RATIO`),
   value 3, in the module this section names.

**The incident's implied prices, for the §4.2 replay harness.** §1's "$2.50/MTok
write premium" is the write *price*: $27.39 ÷ 10.95M write tokens. The read side
is ~$0.20/MTok, so the premium `compute_wasted_usd` charges is **$2.30/MTok** —
which is what makes the per-call figure $0.437 (190k × 2.30/1M) and the session
$20.98. Any replay that assumes a standard Sonnet snapshot ($3.75/$0.30) prices
the same incident at ~$31 and will look like a regression against §1.

**No backfill.** Rows written before the deploy keep whatever status they were
given, so the incident session's own 56 rows still read `hit`. §4.4's standing
regression case works forward from the deploy, not backward, and the fleet
baseline in the roadmap's metric 1 starts at the first prod release — dev
deployment alone does not start that clock.

### PR-2 — bound the compaction summary

- Add `COMPACTION_SUMMARY_TOKEN_BUDGET` (default **8_000** tokens ≈ 32k chars)
  to [constants.py](../../backend/src/agents/main_agent/config/constants.py).
- In `update_after_turn`, after the join: if the summary exceeds the budget,
  re-summarize it with the cheap model (Nova Micro / Haiku, same path as title
  generation) down to the budget, **once, at checkpoint advance** — the turn
  that already pays a prefix re-write, so the compression is cache-free.
  Fallback if the re-summarize call fails: keep the **newest** records that
  fit the budget (newest-first truncation, never oldest-first — recent context
  is what the model needs).
- Persist the compressed result in `compaction_state.summary` so every
  subsequent restore prepends identical bytes (the byte-stability contract is
  unchanged: summary still only mutates at checkpoint advance).

**Acceptance:** a session with 200k chars of LTM summary records restores with
a prepended summary ≤ budget; integration test seeds oversized records and
asserts the restored first-message byte length; the incident session, replayed
locally, compacts below the 100k threshold within one checkpoint advance.

### PR-3 — root-cause and fix restore byte-instability

Investigation first, fix second; this PR is not mergeable until the divergence
is named.

- **Instrument first divergence:** dev-only (flag-gated) hook that persists a
  rolling hash chain of the outbound message list per cachePoint segment and,
  on a `partial_miss`, logs the first index where this call's bytes diverge
  from the previous call's. This turns "something changed" into a file:line.
- **Explain `original=74` frozen:** determine why the SDK restore returned an
  identical 74-message list across turns that were completing (write path
  failing silently at large message sizes? `events_to_messages` merge
  behavior? `_filter_restored_tool_context`?). If turns are failing to persist
  to AgentCore Memory, that is its own sev — restored sessions are silently
  losing tail turns.
- **Unify checkpoint/anchor coordinates:** `checkpoint` (window-relative) and
  `truncation_anchor` (absolute) must live in one coordinate space, anchored
  to a stable message identity (event ID, not list index), so a shifted
  restore window cannot re-slice a different front-of-history.
- Regression test: replay a >threshold session across 10 simulated
  cold-restore turns and assert the derived prefix is byte-identical between
  consecutive turns except the appended tail.

**Acceptance:** the incident session's turn shape (60s-gap turns, same config
hashes) reproduces locally showing the divergence, then shows `partial_miss`
count 0 after the fix; the byte-stability regression test is in CI.

### PR-4 — pin the memory-augmented system prompt per session visit

- Retrieved UserPreference / SemanticFact blocks are frozen at session
  initialize and reused for the life of the lease (or a bounded refresh
  interval ≥ the 1h cache TTL) instead of re-retrieved per turn. New facts
  extracted mid-session take effect next visit — acceptable staleness, and it
  removes the mid-burst `systemPromptHash` flips.
- Keep ordering deterministic at the source (prompt-cache contract).

**Acceptance:** in a 10-turn burst with concurrent extraction writes,
`systemPromptHash` is constant across all 10 `C#` rows.

### PR-5 — quota runway

- Add 50% and 75% warning thresholds alongside the existing 80%/90% in
  [quota.py](../../backend/src/apis/shared/quota.py) (tier-configurable,
  default on).
- New `quota_session_notice` event when a **single session** crosses a
  configurable share of the monthly limit (default 25%): "This conversation
  has used $X of your $Y monthly quota." Same SSE channel as
  `quota_warning`; SPA renders it on the existing quota banner surface.
- Admin: sessions list sortable by period cost so support can spot a runaway
  conversation before the user calls.

**Acceptance:** replaying the incident's cost curve emits 50%/75% warnings on
Aug 2–3 (two days of runway instead of hours) and a session notice on Aug 1.

**As shipped.** Built as specified, with one design decision the bullets left
open and one acceptance number that moved when it met the real rows. The
replay is a test, not a claim: `backend/tests/shared/test_quota_runway.py`
runs it over `tests/fixtures/quota_incident_cost_curve.json` — a content-free
projection (timestamp + `cost.total`) of the incident session's 105 `C#`
rows, read from prod on 2026-08-05. Its August 1–4 slice is 56 calls summing
to $30.45, which is §1's figure, so the fixture is anchored to the same
denominator this document uses everywhere else.

1. **The session notice reads the session's *lifetime* cost, not its cost in
   the calendar period.** `totalCost` on the session row is what
   `_bump_session_aggregates` already maintains, and it is also the honest
   number: this conversation opened 2026-07-30 and had spent $6.06 — 20% of
   a monthly budget — before August began. Replayed both ways, lifetime cost
   crosses the 25% share at **2026-08-01T00:16Z** (meeting the criterion),
   while period-scoped cost would not cross until **2026-08-02T22:57Z**. A
   period-scoped notice would have hidden, for a full day, exactly the
   conversation it exists to surface. Cost: one `SessionLookupIndex` query
   per turn, only for tiers with a notice share configured, swallowed on
   failure.
2. **The 50%/75% rungs land a day later than §3 predicted, and buy ~6 hours,
   not two days.** Measured against the user's August period usage: 50%
   crosses 2026-08-03T19:40Z and 75% crosses 2026-08-04T01:20Z (both
   2026-08-03 in the deployment's own timezone), versus 80% at
   2026-08-04T01:28Z and the limit reached at 2026-08-04T22:31Z. The spend
   was concentrated enough that halving the trigger barely moves the clock —
   §4.1's warning that "some §1 numbers will move" applies to §3's estimates
   too. **The runway in this incident comes from the session notice** (~3.9
   days ahead of the block), and the extra rungs are cheap insurance for the
   diffuse case rather than the fix. The replay asserts both sets of days, so
   a regression has to argue with the recorded rows.
3. **Everything is tier-configurable, and one knob was already broken.**
   `earlyWarningPercentages` (default `[50, 75]`, `[]` opts out) and
   `sessionNoticePercentage` (default 25, 0 disables) join
   `softLimitPercentage` on `QuotaTier`; the ladder always keeps the tier's
   soft limit and 90%, so this is strictly additive. `QuotaTierUpdate` did
   not carry `softLimitPercentage` or `actionOnLimit` while the SPA's edit
   form sent them — an admin editing a tier silently kept the old values —
   fixed alongside the new fields. Only `sessionNoticePercentage` gets a form
   control; the rung list stays API-level.
4. **The admin view is a fan-out, not a scan.** `GET /admin/costs/top-sessions`
   walks the period's top-cost users (`PeriodCostIndex`, already sorted) and
   queries each one's session rows — a session can only be expensive if its
   owner is. No new GSI on sessions-metadata (one admin view does not justify
   that deploy hazard), no table scan. The response carries `usersScanned` /
   `truncated` so a bounded list never reads as exhaustive, and each row
   carries `partialMissUsd` so a *platform* problem is distinguishable from a
   heavy user at a glance.
5. **A durable `session_notice` quota event** is recorded on first crossing
   (deduped per session per hour, like warnings), so support can answer "when
   did this conversation get expensive?" after the fact — the live SSE notice
   alone leaves no record.

### Deferred (tracked, not this epic)

- **Steer long-document iteration into artifacts / workspace files.** The
  essay lived in chat and was re-emitted at ~5.2k output tokens/turn even
  though `create_artifact` was enabled. Edit-in-place artifacts cut both the
  output spend and the history growth that crossed the threshold in the first
  place. Belongs to `docs/specs/document-context-offload.md`.
- **Quota credit for the affected user** — product/support decision. With a
  working cache their August is ~$10; the platform, not the user, spent the
  quota.

## 4. Validation & evaluation

The per-PR acceptance criteria in §3 are smoke tests — they prove we didn't
break anything and that the mechanism works on the incident session. They do
**not** prove the fixes deliver the cost outcome fleet-wide, and two of the PRs
change what the model sees, which invokes the repo's quality-veto tenet
(`CLAUDE.md`: when cost and quality conflict, quality wins). This section
holds this plan to the same bar as
`docs/specs/document-context-offload-validation.md` /
`document-offload-evaluation.md`.

### 4.1 Validate the diagnosis before building (adversarial re-scan)

The §1–§2 numbers are a **single-pass analysis by one investigator**. Before
PR-2/PR-3 are prioritized, an independent content-free re-scan (same method as
the offload validation doc) must:

- Re-derive the incident decomposition ($27.39 writes / 56 calls / 18:1) from
  the raw `C#` rows, with the denominator stated once (map-cost rows vs. all
  rows — the offload validation caught a ~20% understatement from legacy
  float-cost rows; don't repeat it).
- **Size the fleet cohort, not just the incident.** How many sessions have
  `lastInputTokens > COMPACTION_TOKEN_THRESHOLD`? What is the distribution of
  persisted `compaction.summary` length across all `S#` rows? What share of
  fleet spend sits in over-threshold sessions with write:read > 3? This
  incident is one user; the plan's priority depends on whether the cohort is
  $30 or $300/month. (Cheap: both fields are on the metadata row; one scan.)
- Verify the D3 anomalies reproduce on at least one *other* over-threshold
  session (frozen restore count, checkpoint/anchor coordinate mismatch) — or
  establish that they are specific to this session's history shape.
- Cautionary precedent: the offload validation failed to reproduce the
  conditional-TTL-policy magnitudes from the first pass. Assume some §1
  numbers will move; the plan survives if the *shape* (writes dominate,
  history segment never hits) survives.

### 4.2 Cost outcome: arm-separated attribution, not a before/after blur

Four system states, measured independently so wins are attributable (mirrors
the offload eval's A→B→C discipline):

| arm | state | expected effect |
|---|---|---|
| **B0** | today | baseline: `partial_miss` waste per over-threshold session-day |
| **B1** | + bypass fix | intra-burst turns hit (warm agent); cold-start turns still unstable |
| **B2** | B1 + PR-2 summary cap | over-threshold sessions compact below 100k; threshold-exceeded log rate → ~0 |
| **B3** | B2 + PR-3 byte stability | cold-start restores write only the delta beyond the TTL loss |

Each arm is measured with the **same instrument** (PR-1's `partial_miss` /
wastedUsd EMF rollups) over ≥1 week of prod traffic on the over-threshold
cohort from §4.1, plus a deterministic local replay: the incident session's
event stream replayed through each arm asserting (a) byte-identical
consecutive prefixes and (b) predicted vs. actual cacheRead/cacheWrite per
turn. Measuring only B0→B3 would conflate three mechanisms — if the number
disappoints, we would not know which fix underdelivered.

Success criteria (falsifiable): B1 alone eliminates ≥80% of *intra-burst*
partial-miss waste on the cohort; B2 drives per-turn `Threshold exceeded`
occurrences to zero for sessions under 2× threshold; B3 brings cold-start
write volume to ≤ (prefix − previous cached prefix) + delta.

### 4.3 Quality gate — PR-2 and PR-4 change model-visible context (veto)

PR-2 replaces a 40k-token verbatim edit log with an ≤8k re-summary; PR-4
freezes memory retrievals for a visit. Both are context *reductions* in
exactly the workload where continuity matters most (a user iterating on one
document across days). Per the tenet, cost results cannot ship a confirmed
quality regression; permitted responses are: raise the summary budget, change
what the re-summarizer is told to preserve, shorten PR-4's refresh interval,
or abandon the sub-change.

- **Task design (paired, per-family, following the offload eval):** simulate
  long editing sessions (30–60 turns, synthetic essay/document with planted
  earlier-turn constraints — "never change the thesis wording", "keep citation
  style X", facts stated once at turn 5). Families: (a) **constraint
  retention** — does turn 50 still honor a turn-5 instruction that now lives
  only in the summary; (b) **revision continuity** — does the model
  re-introduce an error it fixed 30 turns ago; (c) **reference lookup** —
  can it answer "what did we decide about §2" from summarized turns. Score
  paired (uncapped vs. capped summary), per family — pooling would dilute the
  family-specific failure signature, which for a re-summarizer is (a) and (b).
- **Sizing honestly:** ~40 paired binary tasks per family detects only large
  regressions (the offload eval's McNemar math applies — ~25pt at n=30).
  Constraint-retention and reference-lookup tasks are programmatically
  generatable and scorable (planted constraints, known decisions), so scale n
  there (≥100) and reserve rubric scoring for continuity tasks with k=3
  generations per (task, arm) at production temperature.
- **PR-4's arm** is simpler: same tasks with facts landed in LTM mid-session;
  measure whether next-visit pickup (the accepted staleness) is acceptable.
  Expected result: no within-visit dependency on mid-session extraction
  exists today worth preserving — but that's the claim to test, not assume.
- PR-1, PR-3, PR-5 and the bypass fix change **no model-visible bytes'
  content** (PR-3 changes only *which* turns re-derive vs. append) and need
  no quality arm — assert request-content equivalence in their tests instead.

### 4.4 Standing verification in prod

- The incident session is the standing regression case: after B1/B2 deploy,
  its next active day should show `cacheReadInputTokens` ≈ the full prefix on
  intra-burst turns and write:read well under 1.
- Fleet: re-run the 2026-07-27 measurement (write:read 1:2, ~$16/user hidden
  waste) after each arm lands; target write:read ≤ 1:4 fleet-wide. This same
  metric is the offload spec's cold-rewrite envelope instrument — the two
  efforts share it.
- PR-1's `partial_miss` EMF metric is the before/after instrument for every
  subsequent PR — no guessing (token-cost tenet: verify, don't assume).

## 5. Non-goals

- Raising `COMPACTION_TOKEN_THRESHOLD`. 100k is defensible *if compaction
  works*; the cost problem is prefix instability and an unbounded summary, not
  the threshold. Revisit only after PR-2/PR-3 land and measure.
- Changing Bedrock cachePoint layout (tools/system/auto) — the 3-point layout
  behaved correctly here; the 11k segment hit throughout.
- The AgentCore idle reaper / microVM lifetime — irrelevant here: the per-turn
  restores were caused by the `extra_tools` agent-cache bypass (see D3), not
  by container churn. (An earlier draft of this spec wrongly called per-turn
  restore "by design"; it is not — a warm cached agent skips `initialize()`
  entirely.)
