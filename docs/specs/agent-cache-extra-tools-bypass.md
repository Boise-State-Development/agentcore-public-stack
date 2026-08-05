# The `extra_tools` agent-cache bypass — 76% of sessions rebuild their Agent every turn

**Status:** Issue write-up / fix direction. No branch.
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
