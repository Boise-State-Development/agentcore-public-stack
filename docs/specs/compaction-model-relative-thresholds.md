# Compaction relative to the model window — a trigger ceiling, a target floor, and paying the rewrite only when it is free

**Status:** PR-1 open (#1125, this document rides with it). PR-2 (bounded
summary + compaction metrics, #1128), PR-3 (paid-when-free apply, #1129),
PR-4 (tool-result offload at intake, #1131) and PR-5 (selective 1h TTL,
flag off, built 2026-09-16) stacked on top of it.
**Owner:** Phil Merrell
**Related:** `compaction-over-threshold-cache-spiral.md` (#833 — the incident
and the summary-cap PR this spec depends on) ·
`compaction-v2-versioned-prefix.md` (#835 — the frozen-segment redesign; this
spec implements v2's I3/I4 *policy* on the v1 machinery so it survives either
way) · `agent-cache-extra-tools-bypass.md` · `document-context-offload.md` ·
`gpt-5-6-prompt-caching.md` (why `maxInputTokens` is a pricing cap) ·
`docs/one-pagers/cost-effectiveness-roadmap.md` (W2 row) · the 2026-09-15 prod
cost audit (top-5 September users + 7 largest conversations, via
`/admin/costs`), whose measurements §2 quotes

---

## 1. The question this answers

Compaction fires at a fixed 100,000 tokens
(`AGENTCORE_MEMORY_COMPACTION_TOKEN_THRESHOLD`), regardless of whether the
model's window is 200k, 272k or 1M. Should the trigger vary with the window?

Yes — but the trigger is the least important number. Under Bedrock prompt
caching the marginal costs are:

| operation, at a 200k-token prefix (Sonnet 5, `global.*`, $2.00/MTok input) | cost |
|---|---|
| keep the history for one more turn (cache read, 0.1× input) | ~$0.04 |
| change the history once (cache write, 1.25× input) | ~$0.50 |

Retention is ~12× cheaper than mutation *per event*. What compaction spends
is **prefix rewrites**, and what it saves is **the size of every read after**.
So the two numbers that matter are (a) how small the conversation is after a
compaction — the *floor* — and (b) whether the rewrite lands on a turn that
was going to rewrite anyway. The July replay in the 2026-07-27 measurement
made this concrete: compacting the ten biggest sessions at 120k down to 25k
cut input-side cost 64%; deeper-and-rarer beat shallower-and-more-often in
every trajectory.

A model-relative threshold that merely *raises* the ceiling on 1M models
would therefore make things worse: every read is larger, every cache bust is
larger, and long-context quality degrades well before a 1M window fills
(`document-context-offload.md` §"context rot"). The design below scales the
trigger with the window but **caps** it, and puts the engineering weight on
the floor and the scheduling.

## 2. Current state (verified 2026-09-15 on `develop` @ 222e1d26)

- `CompactionConfig.token_threshold` = 100,000 (`constants.py` Defaults),
  compared in `update_after_turn` against the turn's cache-inclusive input
  (`inputTokens + cacheRead + cacheWrite`, the only correct context size under
  caching — see the coordinator comment at the call site).
- The catalog already carries the window: `ManagedModel.max_input_tokens`
  (`maxInputTokens`) — 200k on the Claude 4.x rows, 1,000,000 on Sonnet 5,
  272,000 on the GPT-5.6 family and GPT-6 Astra, 256,000 on Qwen. The stream
  coordinator looks it up every turn for the badge (`final_metadata["contextWindow"]`)
  and the storage path. **It is not a capability field on the OpenAI rows: it
  is the short-context pricing cap** (`curated-models.ts:373`). Deriving
  thresholds from it therefore keeps us inside short-context pricing for free.
- The checkpoint is chosen by *turn count*: `cutoffs[-protected_turns]`, i.e.
  "keep the last 3 user turns". Turn count is uncorrelated with tokens — the
  byte-stability audit found three cheap turns summarized while a 92k tool
  result was kept.
- There is **no hysteresis.** Over threshold, the checkpoint advances on every
  turn (each new turn pushes `cutoffs[-3]` forward), the LTM summary join is
  re-fetched and re-persisted, and `Threshold exceeded` logs every turn — the
  spiral session did this 56 times.
- `update_after_turn` receives the agent's live list, which on a restored
  session is *already sliced* at the checkpoint, and compares a slice-relative
  index against the persisted absolute checkpoint (spiral spec D3; 199 of
  1,238 rows carry the mismatch).
- **Strands' default 40-message sliding window runs underneath all of this.**
  `AgentFactory.create_agent` passes no `conversation_manager`, so the SDK
  installs `SlidingWindowConversationManager(window_size=40)` and applies it
  after every event-loop cycle (`agent.py:1631`). Past 40 messages the front
  of `agent.messages` slides every turn. The 2026-09-15 prod cost audit (20
  sessions, $127, content-free) measured the consequence: fingerprint
  `messageCount` pinned at 39–41, every turn start reading only tools+system
  and re-writing the whole window (session e7e75953 flips exactly at message
  41), and the checkpoint/anchor coordinate mismatch (`ANCHOR_MISMATCH`) on
  14 of 20 sessions — because the list the checkpoint indexes into is being
  mutated by something other than compaction.
- The same audit's cache-write attribution for September (Sonnet 5 = $683 of
  $742; cache writes ~54% of it): 36% full re-writes after a >5 min pause
  (cost scales with the context size at the pause — 17 of 20 sessions
  peaked above 100k), 22% live re-writes inside over-100k sessions (the
  spiral, still live), 2.6% the hourly system-prompt tick
  (`get_current_date_pacific()` renders `%H:00`, so every Pacific hour
  boundary flips `systemPromptHash`), ~2% the 40-message window in sub-100k
  sessions. Summaries of 23k–40k tokens were present in 6 of 20 sessions.
- Compaction changes bytes in two places only: the restore-time slice in
  `_apply_compaction` (cold starts) and — nowhere on a warm agent. That is the
  subject of PR-3, not PR-1.
- Strands 1.55 ships the native form of "trigger as a ratio of the window":
  `SummarizingConversationManager(proactive_compression={"compression_threshold": r})`
  reads `model.context_window_limit` (a `BedrockConfig` key we do not set). It
  has no floor, no cache-aware scheduling and no summary budget, and the
  2026-05-18 decision bars it as a bare swap. Per v2 §4.2 it is the *engine*
  we would move onto, not the *policy*.

## 3. Design

### 3.0 Prerequisite: one owner of history size

Compaction cannot express a checkpoint in a list that something else is
trimming. PR-1 sets the conversation manager explicitly:
`SlidingWindowConversationManager(window_size=2000, should_truncate_results=True)`
(`AGENTCORE_CONVERSATION_WINDOW_MESSAGES`; `40` restores the SDK default).
The manager is kept rather than replaced with `NullConversationManager`
because its `reduce_context` is the stack's only
`ContextWindowOverflowException` recovery, and that path is independent of
the window size. Consequence to state plainly: conversations between 40
messages and the ceiling now go to the model whole — more *read* tokens per
turn (0.1×), far fewer *re-writes* (1.25×), and the model sees the
conversation instead of its last 40 messages. Above the ceiling the
compaction policy bounds it.

### 3.1 Three numbers per model, derived from `maxInputTokens`

```
ceiling      = min(window × CEILING_RATIO,       CEILING_CAP_TOKENS)   # trigger
floor        = ceiling × FLOOR_RATIO                                    # target after a cut
hard_ceiling = min(window × HARD_CEILING_RATIO,  ceiling × HARD_MULT)   # force, even on a warm cache
```

Defaults: `CEILING_RATIO 0.5`, `CEILING_CAP_TOKENS 100_000`, `FLOOR_RATIO 0.25`,
`HARD_CEILING_RATIO 0.7`, `HARD_MULT 1.5`. Which gives:

| `maxInputTokens` | ceiling | floor | hard ceiling |
|---|---|---|---|
| 128,000 (a small-window model, for illustration) | 64,000 | 16,000 | 89,600 |
| 200,000 (Claude 4.x, Haiku 4.5) | 100,000 | 25,000 | 140,000 |
| 256,000 (Qwen) | 100,000 | 25,000 | 150,000 |
| 272,000 (GPT-5.6 / GPT-6 pricing cap) | 100,000 | 25,000 | 150,000 |
| 1,000,000 (Sonnet 5) | 100,000 | 25,000 | 150,000 |
| unknown (catalog miss) | `token_threshold` (100,000) | 25,000 | 150,000 |

So the window scales the ceiling **down** for small-window models and never
up: for every model in the catalog today the cap binds at 100k. That is the
honest answer to "shouldn't the threshold vary with the window" — under
cache economics, no, not upward. Why:

- **The cap was drafted at 200k for 1M models and moved to 100k on
  evidence.** The 2026-09-15 replay of the 20 audited Sonnet 5 sessions
  (input side, this spec's PR-3 scheduling rule applied, priced at the
  incident's $2.50/$0.20 per MTok) gave: actual today **$102.64**, no
  compaction **$208.81**, 100k/25k **$73.70**, 200k/50k **$105.30**. The 200k
  policy was worse on 13 of 20 sessions and never better, and roughly equal
  to today — raising the ceiling gives back the whole PR-1 win on that cohort.
  Mechanism: 36% of cache-write dollars are cold re-writes after a >5 min
  pause, and their size is the context *at the pause*; under 200k/50k most
  heavy sessions never reach the ceiling and run 100–190k the whole time, so
  each return costs ~$0.375 instead of $0.08–0.12. Caveats: 20 sessions, all
  Sonnet 5, all heavy; the "actual" column already benefits from the
  40-message window that PR-1 removes, so "no compaction" is the baseline
  PR-1 replaces. Raise the cap only when `compaction_forced` and the cost
  anatomy show sessions that need more room; it is one constant.
- **Quality is on the same side.** Every warm turn reads the whole prefix;
  the offload spec's evidence on context rot says the model is not better at
  400k of chat history than at 100k plus a good summary.
- **The floor at a quarter of the ceiling** is the "deeper and rarer" result:
  a session that compacts to 25k and grows back to 100k pays one rewrite of
  ~25k and then ~75k tokens' worth of *reads* before the next cut. A session
  that compacts 100k → 80k pays a rewrite of 80k every few turns.
- **The hard ceiling** exists so that PR-3's "wait for a free turn" cannot
  wait forever. 70% of the window leaves room for the turn's own output and
  for one oversized tool result without an overflow. On a 200k window the 70%
  term (140k) binds; on larger windows the 1.5× term (150k) does.
- **These are starting points, not tuned constants.** §5 says how to move
  them. Every one is an env override (`AGENTCORE_MEMORY_COMPACTION_*`), and
  `AGENTCORE_MEMORY_COMPACTION_MODEL_RELATIVE_ENABLED=false` reverts to the
  fixed 100k threshold and the legacy turn-count cut exactly.

### 3.2 A floor-seeking, token-aware checkpoint

When the trigger fires, the cut is chosen to land the retained history **at
or below the floor**, keeping as much as fits:

1. Estimate tokens per message from the message's serialized size (chars/4;
   flat 1,500 for an inline image; document bytes are already stripped at
   restore). Calibrate the estimates so they sum to the turn's *history*
   tokens — the `messages` partition from the context-attribution breakdown
   when the turn has one, otherwise the full input count. Over-attributing
   system/tools tokens to history biases the cut slightly deeper, which is
   the safe direction.
2. Candidate cuts are the same tool-pair-safe boundaries as today (user
   messages that are not tool results), and never newer than
   `cutoffs[-protected_turns]` — the last N turns are always kept, as today.
3. Choose the **oldest** candidate whose retained estimate is ≤ floor. If
   even the minimum-protection cut exceeds the floor (a giant tool result
   inside the protected tail), take the minimum-protection cut and log it;
   evicting inside the protected tail is PR-4's escalation, not a deeper cut.

### 3.3 Hysteresis: a cut disarms the trigger

`CompactionState.armed` (persisted, legacy rows default `True`):

- Over the ceiling **and armed** → cut, then `armed = False`.
- Over the ceiling **and disarmed** → do nothing unless input ≥ hard ceiling,
  in which case cut anyway and log `compaction_forced`. A forced cut is the
  signal that the previous cut did not take (the summary is too large, or the
  slice has not landed on this agent yet) — it is a metric, not a code path
  we expect to run.
- At or below the ceiling → `armed = True`.

This makes v2's I3 ("threshold exceeded twice in a row is a bug by
definition") structural on the v1 code: the spiral's 56 consecutive cuts
become one cut plus 55 no-ops, and the LTM summary fetch stops being a
per-turn call.

### 3.4 One coordinate system

`update_after_turn` computes cuts over the agent's live list. The live list
starts at the absolute index the restore sliced at (or 0). The manager now
tracks that offset (`_live_offset`, set in `_apply_compaction`) and persists
`checkpoint = _live_offset + relative_cut`. The persisted checkpoint stays
absolute — the coordinate `_apply_compaction` slices with — and the D3
comparison (`new <= current`) is finally like-for-like. The deeper D3
question (the restore window itself moving — `original=74` frozen) is
untouched here and stays with spiral-spec PR-3.

### 3.5 Pay the rewrite when it is free (PR-3) — BUILT

Everything above decides *what* to cut. PR-3 decides *when the bytes change*.
As built (`apply_pending_compaction` + the `pending*` fields on
`CompactionState`; kill switch
`AGENTCORE_MEMORY_COMPACTION_DEFERRED_APPLY_ENABLED=false` applies cuts
immediately as PR-1/2 did; legacy mode is always immediate):

- **Post-turn parks, never applies.** `update_after_turn` computes the cut and
  the bounded summary and stores them as `pendingCheckpoint` /
  `pendingSummary` / `pendingHardCeiling` / `pendingSince`. `checkpoint` stays
  the *applied* value — the one `_apply_compaction` slices at on restore. A
  second over-ceiling turn while a cut is parked is a no-op
  (`compaction_pending_waiting`); it never cuts deeper.
- **Head of turn decides.** The stream coordinator calls
  `apply_pending_compaction(agent, prefix_key="<model>|<agent>")` before the
  first model call of every turn, on cached and freshly restored agents
  alike. It applies when, in this order: `cache_expired` (more than
  `cache_ttl_seconds` since the previous turn's save — the entry is gone and
  the next call re-writes the prefix regardless), `prefix_changed` (the
  model|agent key differs from the persisted `lastPrefixKey` — the cached
  prefix is already invalid), or `hard_ceiling` (the previous turn's input
  reached the hard ceiling the cut was computed under). Otherwise it waits.
- **Applied in place.** `messages[:] = [first_with_summary] + messages[k+1:]`
  where `k = pendingCheckpoint − _live_offset`; then `_live_offset` moves to
  the checkpoint and the state is promoted (`checkpoint`, `truncation_anchor`,
  `summary`) and persisted. The result is byte-identical to what
  `_apply_compaction` derives from stored history under the promoted state
  (pinned by `test_live_apply_matches_a_cold_restore_of_the_same_state`), so
  a cold restore after a live apply reads the same prefix.
- **Aliasing carries the offset.** `_adopt_session_conversation` copies
  `_live_offset` when it points a new agent at the live list.
- **Measured.** Each application persists `applied` (the reason),
  `cacheGapSeconds` and `pendingSince` on `compaction.policy`, logs
  `rewrite_scheduled` vs `rewrite_forced`, and emits `CompactionApplied`,
  `CompactionAppliedForced` and `CompactionCacheGapSeconds`.
- **Not done here:** `context_window_limit` on the Strands model config
  (needs the window at agent construction; small follow-up).

The original design sketch, kept for the record:

- Post-turn computes and persists the pending checkpoint + summary (the
  expensive part, off the critical path). Nothing is applied.
- Pre-call on the next turn, the pending cut is applied to the live list
  **in place** (`agent.messages[:] = ...`, never rebound — the #741 alias)
  when any of: the gap since the last call exceeds the Bedrock TTL
  (`cacheGapSeconds` > 300 — the prefix was going to rewrite anyway), the
  model or the `@`-mentioned agent changed (prefix already invalid), or the
  turn's input is projected at or above the hard ceiling.
- Between the ceiling and the hard ceiling on a warm cache, the cut waits.
  `rewrite_scheduled` vs `rewrite_forced` is logged per application so I4's
  effectiveness is measurable.
- Aliasing the message list across agent instances must also alias the
  offset; `_adopt_session_conversation` syncs `_live_offset` when it adopts.

### 3.6 The rest of the sequence

- **Bounded summary** (= spiral spec PR-2, unchanged): an 8k-token budget,
  re-summarized once with the cheap model at cut time. Without it the floor
  is unreachable — a 40k-token summary is larger than the 25k floor. PR-1's
  `compaction_forced` metric will show exactly how often this bites until it
  lands.
- **Offload escalation** (PR-4) — BUILT for tool results, at intake. The
  case that defeats the floor is a huge tool result inside the protected
  tail. Rather than escalate at cut time (which would mutate a protected turn
  and need a restore-replay of every edit), oversized tool results are bounded
  the moment they are produced, before they enter the prefix: Strands 1.55's
  vended `ContextOffloader` on `AfterToolCallEvent`, S3 storage in the
  user-files bucket under `compaction-offload/{userId}/{sessionId}/` (one
  namespaced storage per agent, so references are session-scoped by
  construction and an `@`-mention agent resolves the same ones), no eviction
  from the model path (`evict_after_cycles=None`; a 90-day S3 lifecycle rule
  expires the objects), gate 4,000 tokens / preview 1,000 (env-backed;
  `AGENTCORE_TOOL_RESULT_OFFLOAD_ENABLED=false` removes the plugin). The
  persisted message already carries the bounded form, so restore reproduces
  it byte-for-byte. Our subclass adds a chars/4 pre-filter so the plugin's
  per-result `CountTokens` round trip only runs for results near or over the
  gate, and a content-free `ToolResultOffloaded` record per offload. The
  model keeps a preview and pulls spans back with
  `retrieve_offloaded_content` (pattern / line range / full) — one stable
  spec in `toolConfig`, not an RBAC-gated tool, on the `read_skill_file`
  precedent (the `workspace_files` catalog key is granted to no prod role,
  so escalating into the workspace tools would have shipped dark). **User
  attachments are not touched**: the digest + page-range read in
  `document-context-offload.md` is the right shape for those and stays that
  spec's PRs 1–4. What remains of the floor-unreachable case after this is
  measured by `CompactionFloorUnreachable` on the cut record.
- **`context_window_limit` on the model** (PR-3, small): plumb
  `maxInputTokens` into `ModelConfig` and set `context_window_limit` in
  `to_bedrock_config` (a valid `BedrockConfig` key in 1.55), so Strands'
  own `estimate_utilization` and our policy agree on the window, and the
  eventual v2 engine swap inherits it.
- **Per-section cache TTL** (PR-5) — BUILT, **default off**. `AGENTCORE_PROMPT_CACHE_STATIC_PREFIX_TTL=1h`
  makes `ModelConfig.to_bedrock_config` emit
  `CacheConfig(system_prompt_ttl="1h", tools_ttl="1h")`; upstream rewrites
  the hand-placed TTL-less system point and gives the tools point its own,
  the message point stays at 5m (`cache_config.ttl` unset), so the order is
  tools(1h) → system(1h) → messages(5m), which Bedrock requires
  non-increasing. Anything but the literal `1h` emits exactly today's bytes.
  **Why off by default, against the flags-default-on house style:** the
  2026-07-27 model found a *blanket* 1h TTL a wash — the 2× write premium ate
  the saving — and only the selective variant worth testing; and the repo's
  standing rule since #954/#956 is that a caching default is never adopted on
  inspection alone. The gate is `scripts/probe_static_prefix_ttl.py` (two
  arms, same static prefix, a configurable gap) plus a week of cost rows in
  dev with the flag on. **The experiment arm's rows are priced honestly:**
  `CostCalculator.calculate_message_cost` bills the unread remainder of the
  static segment at the 1h premium (2× base) and the rest of the write at the
  catalog's 5m rate, using the context-attribution breakdown for the static
  size; every such row carries `staticPrefixTtl: "1h"` so the anatomy can
  split arms. Economics to expect: +0.75× base on every static write, −1.15×
  base on every return inside the hour but past five minutes; it pays when
  the second is more frequent than the first (the audit's 36% cold re-writes
  after a >5 min pause say it might, on the 28k static prefix; the hourly
  system-prompt tick would bust it hourly until that fix lands).

## 4. Cost model

Per-turn input cost for a session sitting at `P` prefix tokens on a model with
base input rate `r`:

```
warm turn          ≈ 0.10 r P              (cache read)
cache-busted turn  ≈ 1.25 r P              (cache write)
compaction turn    ≈ 1.25 r F  + summarizer call     (F = post-cut size)
```

For the July cohort (10 sessions, peaks 112k–597k, ~19 cache tokens written
per output token) the replay gave input-side cost **$90.35 → $32.40** when
cutting at 120k to 25k. The same replay with the ceiling at 200k and the floor
at 50k is the number to produce for the Sonnet 5 row before ratifying §3.1 —
`scan_fleet_prefix_spend.py` and the spiral spec's §4.2 harness already
replay real trajectories under a policy.

## 5. Quality gate and tuning

- **Veto before default change in prod:** the spiral spec §4.3 long-session
  eval (constraint retention / revision continuity / reference lookup) runs
  on PR-1 with the fixed-threshold arm as control. A deeper cut is a bigger
  context change than the summary cap, so the veto applies with full force.
- **Tuning knobs move on evidence, not taste:** raise `FLOOR_RATIO` if the
  eval shows retention loss; lower `CEILING_CAP_TOKENS` if the Sonnet 5
  cohort's write:read ratio stays worse than 1:5 after PR-3; never raise the
  cap above 272k while GPT-family rows share the constants (pricing tier).
- **Summarizer prompt:** preserve standing user instructions and constraints
  verbatim (kaizen 2026-05-29 item); this is PR-2's prompt, not PR-1's.

## 6. PR breakdown

### PR-1 — policy, floor-seeking cut, hysteresis, coordinates (this PR)

- `AgentFactory.build_conversation_manager()`: the explicit 2000-message
  window (§3.0) — the prerequisite for every coordinate claim below.
- `compaction_policy.py`: `CompactionPolicy.resolve(config, context_window)`,
  `estimate_message_tokens`, `choose_checkpoint`.
- `CompactionConfig`: `model_relative_enabled` + the five ratio/cap fields,
  all env-backed; `CompactionState.armed` + a `policy` snapshot of the cut;
  `CompactionResult` gains `context_window`, `ceiling`, `floor`,
  `hard_ceiling`, `forced`, `retained_tokens_estimate`.
- `TurnBasedSessionManager`: `_live_offset`; `update_after_turn(...,
  context_window=, history_tokens=)` implements §3.2–§3.4. **No change to
  when bytes change** — the slice still applies at restore only.
- Stream coordinator passes the catalog window (already looked up for the
  badge) and the breakdown's `messages` tokens; the `compaction` SSE payload
  carries the policy fields (additive — the SPA validator ignores extras;
  the TS interface gains them as optional).
- Kill switch `AGENTCORE_MEMORY_COMPACTION_MODEL_RELATIVE_ENABLED=false` →
  fixed threshold, turn-count cut, no arming. Default on (house style).

**Acceptance:** unit tests for the table in §3.1 (including the unknown-window
and kill-switch rows); a 5-turn conversation with 1,000-token threshold cuts
to the oldest candidate under the floor; a turn over the ceiling on a disarmed
state is a no-op and a turn at the hard ceiling is a forced cut; the existing
byte-stability suite is unchanged; a replayed spiral-shaped sequence (input
constant above ceiling for 10 turns) produces exactly one checkpoint advance.

### PR-2 — bounded summary (spiral spec PR-2, as written there) — BUILT

As built (`compaction_summary.py`, stacked on PR-1): `bound_summary()` holds
the persisted summary at `COMPACTION_SUMMARY_TOKEN_BUDGET` (8,000 tokens,
chars/4 — the same estimate the admin `SUMMARY_OVER_BUDGET` diagnosis uses).
Within budget → unchanged. Over budget → one Nova Micro `converse` call
(`AGENTCORE_MEMORY_COMPACTION_SUMMARY_MODEL_ID`; kill switch
`AGENTCORE_MEMORY_COMPACTION_SUMMARY_MODEL_ENABLED=false`) with a prompt that
keeps standing instructions, decisions, current state of the work, open items
and exact identifiers, and drops narration and superseded drafts (the kaizen
2026-05-29 item). Model failure, a ceiling-hit generation, or an overshoot →
newest-first truncation (keep the newest records that fit; if none fit, the
tail of the newest). Runs once at cut time and the result is persisted
verbatim, so the byte-stability contract is untouched. Provenance lands in
the persisted `compaction.policy` map (`summarySource`, `summaryOutcome`,
`summaryTokensBefore/After`, `summaryTokenBudget`).

**Sequenced immediately after PR-1, ahead of PR-3, on evidence.** In the
2026-09-15 audit all 16 over-100k sessions carried `AGENT_CACHE_BYPASS`
(spreadsheet/word/ppt tools enabled), so they rebuild the agent every turn
and the restore slice already runs for them; zero were warm-agent
(`create_artifact`-only) sessions. What bit in every one was the slice
running and *still* not getting under the threshold — 23k–40k-token summaries
and the protected tail. Session 65b6d4ab: 17 live full re-writes from message
15 onward at 40–100k context, before the 40-message window ever engaged —
the compaction slice re-running on a fresh agent each turn with a changing
summary. That is this PR's problem, not PR-3's.

### PR-3 — paid-when-free scheduling (§3.5) — BUILT; `context_window_limit` deferred

Value on the audited cohort is the `create_artifact` (warm-agent) sessions and
turn latency, not the over-100k dollars — those are PR-1 + PR-2. Two of the
four window-pinned sessions examined (e7e75953, 0d8ba8f8) never exceeded 100k
and were pure 40-message-window effects, which PR-1's §3.0 change alone
recovers.

**Acceptance:** on a warm agent held above the ceiling, the live list shrinks
on the first turn after a >300s gap and not before (unless hard ceiling);
`partial_miss` on the cut turn only; `test_second_cache_key_for_a_session_shares_the_conversation`
still passes with the in-place apply.

### PR-4 — offload escalation (§3.6) — BUILT as tool-result offload at intake

**Cohort split (2026-09-15 audit, content-free, September prod).** Of the 95
sessions that peaked over 100k: 52 (55%) had a single tool result ≥4k tokens
in their last three turns (43 only that, 9 also an attachment) — PR-4's
target; 25 (26%) had an attachment there (16 only that) — the document-offload
spec's; 27 (28%) had neither — long sessions whose bulk is old history plus a
23–40k summary, which the cut and the summary cap reach. The biggest single
intra-turn writes are squarely tool results (123k, 149k, 131k in one call);
the attachment-only cases include 1–3-turn sessions at 280–530k that are one
huge upload. Method: turns split at `cacheGapSeconds ≥ 10s`; "big tool result"
= an intra-turn call that read the prior prefix and wrote ≥4,000 tokens
(slightly overstated on long `tool_use` blocks); "attachment in the last 3
turns" = an upload row between the first call of those turns minus 5 min and
the last call.

### PR-5 — selective 1h TTL experiment (§3.6) — BUILT, flag off; enable per the gate above

**Probe run 2026-09-16, dev-ai us-west-2, Haiku 4.5, 420 s gap
(`scripts/probe_static_prefix_ttl.py --gap-seconds 420`):**

| arm | call | read | write | $ (base $1.10/MTok) |
|---|---|---|---|---|
| 5m | first | 0 | 6,251 | 0.008598 |
| 5m | second | 0 | 6,251 | 0.008598 |
| 1h | first | 0 | 6,251 | 0.013756 |
| 1h | second | **5,924** | **327** | 0.001374 |

Bedrock honors `ttl: "1h"` on the tools and system points: after the 5m
entry expired, the 1h arm **read** the static segment (5,924 tokens) and
re-wrote only the message segment (327), while the 5m arm re-wrote all
6,251. Pair cost $0.01513 vs $0.01720 — the 1h arm was **12% cheaper at this
gap**, having recovered its 60% dearer first write on one return. The
arithmetic that generalizes: the premium is 0.75× base per static write
(~$0.0052 on this prefix), the saving 1.15× base per return inside the hour
but past five minutes (~$0.0075); the arm pays when such returns outnumber
static writes by more than ~0.7 : 1. Static writes also happen on every
prefix *change* (a model or tool-set switch, and today the hourly
system-prompt tick), so the dev-week measurement should run **after** the
tick fix lands.

**60 s gap, same day:** both arms read the full 6,251 on the second call
(5m: $0.008598 + $0.000691; 1h: $0.013756 + $0.000691). The 1h arm was
**$0.005157 more expensive** — exactly the 0.75× base premium on the first
write, with nothing to recover inside five minutes. So the arm is a fixed
surcharge per static write, ~$0.0052 on this 6.3k prefix (≈$0.023 on a 28k
prod prefix at Haiku rates, ≈$0.042 on Sonnet 5), recovered at ~$0.0075 /
$0.034 / $0.061 per return that lands between five and sixty minutes. The
dev week decides whether prod sessions return in that window often enough;
the probe cannot.

## 7. Observability

### 7.1 What PR-2 adds — one content-free record per cut

`AgentCoreStack/Compaction` EMF namespace (silenced with the rest of the cost
observability layer by `PROMPT_CACHE_OBSERVABILITY_ENABLED=false`):

| metric | answers |
|---|---|
| `CompactionCut` | cadence — cuts per session-day is v2 §8's spiral detector (alarm at >2/day) |
| `CompactionForced` | how often a cut ran while disarmed = how often the previous cut did not take |
| `CompactionInputTokens`, `CompactionRetainedTokens` | how far above the ceiling cuts fire and how deep they land (is the floor being reached?) |
| `CompactionSummaryTokens`, `CompactionSummaryOverBudget` | is the summary the reason cuts miss the floor; how often the model vs truncation path runs |
| `CompactionFloorUnreachable` (PR-4) | how often the protected tail alone still exceeds the floor after intake offload — the residual document/attachment case |
| `ToolResultOffloaded`, `ToolResultOffloadedTokens` (PR-4) | how much tool payload was kept out of the prefix, per tool (`toolName` property) |

Properties (queryable in Logs Insights, not dimensions): `policySource`,
`contextWindow`, `ceiling`, `floor`, `summaryOutcome`, `summaryTokensBefore`.
No text from the conversation or the summary is ever emitted.

### 7.2 Data points still worth collecting (not built)

The question behind all of them is *what does a turn cost because of history,
and what did compaction do to it* — today we can see the first half (cost
rows) and, after PR-2, the second, but not joined:

- **Per-call `checkpoint` / `armed` / `liveOffset` on the `C#` cost row** —
  lets the anatomy page show the context trajectory against the cuts without
  correlating timestamps by hand. Additive fields on an existing write.
- ~~**`cacheGapSeconds` next to every cut**~~ — done in PR-3: each application
  records the reason and the gap.
- **Retained-vs-actual calibration** — the next turn's measured
  `contextBreakdown.messages` against the cut's `retainedTokensEstimate`. One
  number per cut, and the only way to know whether the estimator is off by 5%
  or 50%.
- **Turn shape** — messages per turn and tool-result bytes per turn (the
  `ToolCensusHook` has the count; bytes are one more `ADD`). Partly covered
  by PR-4's `ToolResultOffloadedTokens`; the sub-gate long tail is not.
- **Outcome signal joined to compaction** — a down-thumb rate keyed to
  "turns since last cut" is the first evidence that a cut costs anything
  besides dollars (the kaizen review-queue already lists this as the
  counterweight the cost roadmap lacks). Without it the §5 quality gate stays
  a one-off eval rather than a standing measurement.
- **Warm-vs-cold split per cut** — whether the cut's session was on the
  agent-cache bypass path (restore every turn) or a warm agent; the 2026-09-15
  audit had to infer this from `enabledTools`. It decides how much PR-3 is
  worth.

- Log lines: `compaction_cut` (window, ceiling, floor, hard, relative cut,
  absolute checkpoint, retained estimate), `compaction_disarmed_noop`,
  `compaction_forced`, `compaction_rearmed`.
- The persisted `compaction.policy` map on the session row records what the
  last cut believed the window and thresholds were, so
  `GET /admin/costs/sessions/{id}/profile` can show it. The
  `OVER_COMPACTION_THRESHOLD` diagnosis should read that map instead of the
  fixed default once PR-1 is live (small follow-up, not in PR-1).
- **Measurement confounder:** the hourly system-prompt tick (`%H:00` in
  `get_current_date_pacific()`) re-writes every session's prefix once an hour
  regardless of compaction. Fix it (render the date only, or the hour in a
  turn-scoped message) before attributing write:read movement to this spec's
  PRs; filed separately, not in PR-1.
- `compactionCount` (already emitted) divided by session-days is the
  cadence metric v2 §8 asks for; an alarm on >2/day is the spiral detector.

## 8. Non-goals

- Changing the cachePoint layout (tools/system/auto) — v2 §9.
- Replacing the mutation engine with Strands' conversation manager — that is
  v2 and stays gated on its §7 criteria; every policy field here maps onto
  v2's thin policy layer unchanged.
- Cross-session memory quality; the quota system; model routing.

## 9. Open questions

- Should an explicit `AGENTCORE_MEMORY_COMPACTION_TOKEN_THRESHOLD` in an
  environment override the model-relative ceiling? PR-1 says no: it is the
  fallback for an unknown window only. Flip this if an operator needs a
  per-environment ceiling.
- Whether Bedrock prices Claude's 1M window in tiers the way it prices
  GPT-6 Astra. The model cards say no long-context premium for the Claude
  rows today; if that changes, `CEILING_CAP_TOKENS` is the one constant to
  move.
