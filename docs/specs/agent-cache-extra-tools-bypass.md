# The `extra_tools` agent-cache bypass — 76% of sessions rebuild their Agent every turn

**Status:** Arm 1 merged (#839). **G1 read complete — see §8.** The §3
prompt-cache thesis is **disproven**; the win is latency (~60%/turn), and it
required a prerequisite this spec never identified: nothing forwarded the
AgentCore runtime session id, so the agent cache could not hit at all
(#841). Remaining families and the key/snapshot work in §6 are unbuilt and
should now be justified on latency. See §5 hazard 4, found while building
arm 1.
**Found while:** measuring document-conversation cost (2026-08-03) — see
`docs/specs/document-context-offload.md`, defect 4
**Related:** [[project-prod-cache-write-premium]] · #741 (history fork) · #751
(compaction state) · the paused-agent resume path

---

## 1. What happens

`get_agent` reads the in-process agent cache only when the turn built no
per-request tools, and refuses to write one for the same reason:

```python
# read  — inference_api/chat/service.py:279
if not extra_tools and cache_key in _agent_cache:

# write — inference_api/chat/service.py:351
if extra_tools:
    logger.debug("⏭️ Skipping cache for agent with extra_tools")
    return agent
```

`extra_tools` is the concatenation of the seven per-request tool builders
([routes.py:1987](../../backend/src/apis/inference_api/chat/routes.py:1987)).
Six of them gate purely on `enabled_tools` membership — no assistant, no
binding, no other precondition
([routes.py:393](../../backend/src/apis/inference_api/chat/routes.py:393)). So
**any session with one of those tools toggled on never uses the agent cache**,
and every turn constructs a fresh `Agent`: new `TurnBasedSessionManager`, a full
`initialize()`, a fresh AgentCore Memory restore, `_apply_compaction`, history
repair, and the document strip.

## 2. Blast radius (measured in prod, 2026-08-03)

From `preferences.enabledTools` on the `S#` rows of
`boisestateai-v2-sessions-metadata` (3,565 sessions):

| cohort | sessions | share |
|---|---|---|
| ≥1 injected tool enabled → **cache bypassed** | 2,720 | **76.3%** |
| cacheable | 845 | 23.7% |

Attachment sessions specifically: **339/426 = 79.6%** bypassed.

The drivers are `analyze_spreadsheet` (2,669 sessions) and `list_spreadsheets`
(2,662) — they look **default-on in the tool picker**, which is why this reaches
three quarters of the fleet rather than a power-user slice. `create_artifact`
adds 957.

By spend, the bypassed cohort is essentially the whole platform: **$707.38 of
$746.98 (95%)**.

## 3. What it costs

**Certain — restore churn.** Every turn on 76% of sessions pays a full
`initialize()`: AgentCore Memory `list_events`, message deserialization, tool
registry construction, compaction application, history repair. This is pure
per-turn latency the cache exists to avoid.

**Certain — it multiplies every restore-path defect by the turn count.** The
document strip is the worked example: a bug that looks like "only bites after a
15-minute idle gap" actually fires on turn 2 for four out of five attachment
conversations. Any future defect on the `initialize()` path inherits the same
amplification.

> **DISPROVEN, 2026-08-05 (G1).** The prompt-cache claim below did not
> survive its own experiment. With the agent cache fully working, the token
> split is **identical** to the bypassed arm — write:read 0.336 either way,
> 7,231 vs 7,229 tokens written. Rebuilding the `Agent` every turn costs
> **latency, not cache writes**, because the restore path was already
> producing a byte-stable prefix. §6's falsifiable branch fired exactly as
> written. The measured latency case is in §8; the paragraph below is kept
> as the hypothesis that was tested, not as a finding.

**Suspected, not established — prompt-cache re-writes.** Comparing
turn-opening calls where the gap was 60–300s (inside the 5-minute Bedrock TTL,
so a re-write is unexplained):

| cohort | within-TTL turns | unexplained cold-write | cacheWrite as % of cohort spend |
|---|---|---|---|
| bypassed | 3,167 | **11.2%** | **56%** |
| cacheable | 590 | 5.8% | 17% |

**This comparison is confounded and must not be quoted as causal.** Sessions
with spreadsheet/artifact tools enabled also do tool-heavy work — more calls,
larger contexts, bigger tool results — any of which could produce the same
spread. The cacheable cohort is also small ($39.46 total spend) and skews short.
The honest reading is: the cohort that carries 95% of spend spends 56% of it on
cache writes, and rebuilding the Agent every turn is a plausible contributor
that has never been ruled in or out. §6 proposes the experiment that would
settle it.

## 4. Why the bypass exists

Not an oversight — a deliberate shortcut. `_create_cache_key`
([service.py:52](../../backend/src/apis/inference_api/chat/service.py:52))
already includes `session_id` and `user_id`, so identity-bound closures are not
the problem. The problem is the values injected tools capture that the key
**doesn't** carry:

- `assistant_id` — `make_list_spreadsheets_tool(assistant_id, session_id, user_id)`
  binds it by closure; it is not a key element. A cached agent reused after the
  session switched assistants would query the wrong knowledge base.
- The resolved **memory binding** (`ResolvedMemoryBinding`: space id, access
  level) — `_build_memory_tools` binds it by closure and is deliberately not
  gated on `enabled_tools` at all. [routes.py:1378](../../backend/src/apis/inference_api/chat/routes.py:1378)
  states this outright: a memory binding stays out of the key by *"skipping the
  cache entirely (extra_tools)."*

So `if extra_tools` is standing in for "this agent captured something the key
doesn't describe." It is correct. It is just far broader than the two cases that
motivated it, and it silently swallowed the six `enabled_tools`-gated builders
that *are* fully described by the existing key plus `assistant_id`.

## 5. Why it isn't a one-line fix

Three hazards, all documented in the code and all previously paid for:

1. **The resume path rebuilds the key from `PausedTurnSnapshot`.** Any new key
   element the snapshot doesn't carry orphans a paused agent and breaks
   OAuth-consent / tool-approval resumes — `service.py` warns about exactly
   this. `enabled_skills` on the snapshot
   ([models.py:110](../../backend/src/apis/shared/sessions/models.py:110)) is
   the precedent *and* the template, including its back-compat rule: `None` on
   snapshots written before the field existed falls back to request-time
   resolution.
2. **#741 aliasing.** One session can be served by more than one `Agent`, and
   the conversation list must be aliased across them. Caching more agents means
   more concurrent siblings — the invariant gets more load, not less. Guard test
   `test_second_cache_key_for_a_session_shares_the_conversation`.
3. **Per-session state must not go stale.** `initialize()` never re-runs on a
   cache hit, which is precisely what bit #741 (history) and #751 (compaction
   state). Anything a manager loads once and holds must be aliased or re-read
   per turn. Caching agents that currently never cache moves more state into
   that category — including whatever the injected tools captured.

4. **Callers that share a cache slot but build a different toolset.** Found
   while building arm 1, not anticipated above. The MCP App dispatch paths
   (`app_tool_call`, `app_context_update` —
   [routes.py:1081](../../backend/src/apis/inference_api/chat/routes.py:1081))
   call `get_agent` with **no** `extra_tools` but otherwise the same key as the
   session's real turns. They could not collide before, because injected-tool
   turns never cached; the moment one does, whichever caller reaches the slot
   first wins — and if that is an App call, every later real turn cache-hits an
   agent missing its injected tools and silently loses them for the session.
   Arm 1 closes this with a `cache_write=False` flag on those two callers (read
   the slot, never seed it). **Any future caller of `get_agent` that builds a
   partial toolset needs the same treatment** — this is the generalization of
   hazard 3, and it is a *tool-loss* bug, not a staleness one.

Hazard 3 is the real work: today the bypass *is* the mechanism that keeps
injected-tool state fresh.

## 6. Fix direction

**Narrow the bypass to the cases that need it, rather than removing it.**

- Add `assistant_id` and a memory-binding descriptor (space id + access, or
  `None`) to `_create_cache_key`, and add both to `PausedTurnSnapshot` with the
  `enabled_skills` back-compat rule.
- Replace the blanket `if extra_tools` with a predicate over what a turn
  actually captured — e.g. cache when every injected tool's captured values are
  represented in the key; bypass otherwise. Memory-space tools can keep
  bypassing until their binding is fully keyed.
- Because injected tools are rebuilt per request anyway, a cached agent must
  have its bound tools **refreshed** on a hit, or the cached closures must be
  provably equivalent under the key. Decide which; do not leave it implicit.
  **Decided (arm 1): equivalence, proven at the key — cached tools are never
  refreshed on a hit.** Eligibility *is* the proof, so the predicate has to
  stay conservative: a family only joins `KEY_DESCRIBED_INJECTED_TOOL_IDS`
  once every value its factory closes over is a key element. Refresh-on-hit
  was rejected because it re-introduces per-turn work on the path whose whole
  purpose is to skip it, and because "rebuild the tools but keep the agent"
  is a third state to reason about on top of hazards 2 and 3.

**Which families are already eligible** (read off the factories while building
arm 1, so the next promotion is mechanical rather than another audit):

| family | captures | eligible? |
|---|---|---|
| `ARTIFACT` | session, user | **yes — shipped in arm 1** |
| `WORD_DOCUMENT` / `EXCEL_SPREADSHEET` / `POWERPOINT_PRESENTATION` | session, user | yes on identical reasoning — held back only so the experiment measures one variable |
| `WORKSPACE` | session, user | same |
| `SPREADSHEET` | session, user, **`assistant_id`** | no — needs the key + snapshot work above |
| Memory-Space | user, email, **resolved binding** | no — and not gated on `enabled_tools`, so it can never be represented by an id; callers pass it as a separate veto |

So four of the six families are a one-line change to that frozenset once the
artifact arm reads clean; only spreadsheets need the key extended.

**Validate with an experiment, not more observational data.** The §3 numbers
cannot separate the bypass from the workload. Enable caching for one builder
only — `create_artifact` is the cleanest: 957 sessions, no `assistant_id`
capture, no memory binding — and compare that cohort's within-TTL cold-write
rate and p50 turn latency before and after. That isolates the variable. If the
cold-write rate doesn't move, the prompt-cache theory is wrong and the remaining
case is latency, which is still worth having.

**Suggested gates:** within-TTL unexplained cold-write rate for the treated
cohort; p50/p95 time-to-first-token; `initialize()` invocations per turn (should
approach 1 per *session* for treated sessions, not 1 per turn); and the #741
aliasing guard test staying green under concurrent siblings.

**How to read them, now that the instruments exist.** The cold-write rate is
`partial_miss` + `miss_avoidable` from
`docs/specs/compaction-over-threshold-cache-spiral.md` PR-1 (#838) — that gate
is the reason PR-1 shipped first, and note it counts only calls written *after*
that deploy, so the pre/post comparison needs a clean cutover date rather than
a look back at history. `initialize()` per turn comes from the structured
`agent_cache outcome=hit|miss` logs arm 1 adds; group by `session` in Logs
Insights and the treated cohort should trend toward one miss per session.

## 7. Non-goals

- **Don't fold this into the document-offload work.** That spec's PRs 1–3 fix
  the strip on the restore path and are correct whether or not this lands. This
  changes *how often* that path runs — an independent variable, and a much
  riskier one.
- **Don't remove the bypass wholesale** to "just cache everything." The two
  motivating cases are real correctness constraints.
- **Don't change the tool picker defaults** to reduce the bypass rate. Turning
  off default-on spreadsheet tools would shrink the blast radius while removing
  capability users have — treating the symptom, and a product decision that
  doesn't belong in a caching fix.

## 8. G1 read — measured 2026-08-05

Two controlled experiments against dev, driven through the real runtime by the
headless harness (`backend/scripts/experiment_agent_cache_arms.py`,
`backend/scripts/probe_runtime_session_affinity.py`). Dev never accumulates
enough observational traffic for the §3 comparison to be re-run honestly, and
a controlled A/B answers the causal question better anyway.

### 8.1 The arm experiment: the agent cache never hit at all

Three arms differing by one variable, no redeploy needed — `create_word_document`
(injected, unpromoted) vs `create_artifact` (injected, promoted) vs no injected
tools (the ceiling, always eligible):

| arm | agent_cache | write:read |
|---|---|---|
| control | miss/miss/miss/miss | 0.34 |
| treatment | miss/miss/miss/miss | 0.34 |
| ceiling | miss/miss/miss/miss | 0.34 |

**Zero hits in every arm — including the ceiling**, which predates all of this
work. The logs read `injected_tools=True cacheable=True … outcome=miss`: arm 1's
predicate fires correctly, the agent is cached, and it is gone by the next turn.

### 8.2 The cause: no microVM affinity

AgentCore routes an invocation to a microVM by runtime session id. Nothing in
the repo forwarded one — `run_agent_headless` sent only Content-Type and
Authorization, and no other caller referenced the header — so AWS assigned a
fresh runtime session per call and a process-local cache was cold by
construction. A two-arm probe differing **only** by that header:

| | agent_cache | turn 1 | turns 2–4 | write:read |
|---|---|---|---|---|
| unpinned | miss/miss/miss/miss | 7.9s | 7.2s, 8.0s, 7.5s | 0.336 |
| pinned | miss/**hit/hit/hit** | 7.2s | **3.1s, 3.1s, 3.1s** | 0.336 |

Fixed in #841. Note pinned turn 1 is itself a miss and stays slow — a warm
Bedrock cache cannot produce a speedup that appears exactly when the agent
cache starts hitting, which is what makes the latency attribution clean.

### 8.3 What this settles

- **The prompt-cache thesis (§3) is disproven.** Identical splits with the
  cache working and not working. The restore path was already byte-stable.
- **The latency case is real and large**: ~60% off every turn after the first,
  for the 76% cohort. §6 called this outcome in advance — *"the remaining case
  is latency, which is still worth having."*
- **Arm 1 was not wasted, but it was inert**: a correct predicate gated behind
  a prerequisite nobody had identified. Nothing it enables mattered until #841.
- **Promoting the remaining four families is still worth doing — for latency.**
  It should follow #841 landing *and* a read showing hits in prod, not just dev.
- **This spec belongs to a latency/performance workstream**, not to the
  roadmap's W2 (prefix stability). Filed there on a cost thesis that no longer
  holds.

**Caveats, stated so nobody over-reads this.** One run per arm, 4 turns, ~7.5k
prefix, 20s gaps, one model, in dev — where concurrency and microVM reuse odds
differ from prod. The incident that motivated the sibling spec ran ~200k of
prefix, where restore cost and byte-stability may behave differently; **this
does not clear #833 PR-3**. And the first affinity probe appeared to show a
cost win too — that was run-order confound (both arms primed with identical
bytes, so the second inherited the first's Bedrock entry), caught only by
re-running with salted priming. Any future arm comparison must salt.

### 8.4 Post-merge verification (2026-08-05, after #839 + #841 deployed to dev)

Re-ran the arm experiment through the **shipped** code path rather than a
hand-rolled probe. The arms separate exactly as designed:

| arm | tools | agent_cache | steady-state turns |
|---|---|---|---|
| control | `create_word_document` (unpromoted) | miss/miss/miss/miss | 4.8s, 5.0s, 4.7s |
| treatment | `create_artifact` (promoted) | miss/**hit/hit/hit** | 4.0s, 4.0s, 3.7s |
| ceiling | none | miss/**hit/hit/hit** | 3.7s, 3.8s, 4.1s |

**Treatment now equals ceiling.** A session carrying a promoted injected tool
performs identically to one carrying no injected tools at all — arm 1 delivers
its full theoretical benefit, and the bypass predicate still correctly excludes
the unpromoted family.

**The attribution splits, and the earlier headline was too generous to the
agent cache.** §8.2 reported ~7.6s → ~3.1s and credited it to pinning. With a
control arm that has affinity but *no* cache hit, the two mechanisms separate:

- **warm container alone**: ~7.6s → ~4.8s — the larger share, and every
  session gets it, cacheable or not
- **reused Agent on top**: ~4.8s → ~3.9s — roughly 19% more, only for
  sessions the predicate admits

That is better news overall (most of the win is fleet-wide) and *worse* news
for the remaining #834 work: **promoting the other four families buys the ~19%
marginal delta, not the whole win.** Still worth doing; no longer headline.

*Rigour note:* the control-vs-treatment comparison is within a single run and
is solid. The ~7.6s figure comes from a different run at a different time, so
treat the warm-container share as indicative rather than measured to the tenth
of a second. Prod numbers will differ again — dev has essentially no
concurrency, which is precisely the condition under which microVM pinning
looks best.
