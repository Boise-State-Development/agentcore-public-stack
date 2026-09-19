# Turn latency: inside the preamble

**Status:** PR-1 + PR-1b SHIPPED (#1184) and **VALIDATED on dev 2026-09-19** —
the measurement ran and is recorded below. PR-2 is now justified by data rather
than by a code reading, and its expected saving is **~4x larger** than this spec
originally estimated. PR-3+ still open.
**Follow-up to:** `docs/specs/agent-state-feedback.md` — its PR-3 measured the
pre-stream window into four stages and then declined to narrate three of them.
This spec opens the one that was left closed.
**Related:** CLAUDE.md § Token cost effectiveness (the "don't guess — verify"
tenet this spec is an application of), `agent-cache-extra-tools-bypass.md` §8
(runtime session affinity, the other latency-not-cost fix).

## Problem

`agent-state-feedback.md` PR-3 instrumented the window between the user's click
and the first SSE byte, and recorded four turns on dev:

| Turn | total | preamble | rag | tools | agent_build |
|------|------:|---------:|----:|------:|------------:|
| cold agent cache | 2542 | 802 | 0 | 261 | **1478** |
| warm | 641 | 494 | 0 | 146 | 0 |
| warm | 644 | 460 | 0 | 149 | 34 |
| warm | 678 | 484 | 0 | 154 | 38 |

`agent_build` got a fix (PR-3 deferred it into the stream generator and narrates
it). `rag` and `tools` are small. **`preamble` was never opened.** On a warm
turn it is the single largest stage — 460–494ms of the 641–678ms handler total,
and the spec's own summary put its range at 450–900ms across the sample.

That is now the largest avoidable wait in the product, and unlike `agent_build`
it is not a status problem: no label makes it shorter, and the loading indicator
already covers the window. It is a latency problem.

## What `preamble` covers

One `prelude.mark()` spanning ~500 lines of `inference_api/chat/routes.py`, from
handler entry to the quota round trip. The mark's own comment — "request
validation, model/settings resolution, file handling and the quota round trip" —
undersells it. Read from the code, a plain warm turn (non-resume,
non-continuation, no attachments) performs this sequence, entirely serially:

| # | Call | DynamoDB work |
|---|------|---------------|
| 1 | `session_owned_by_other_user` | `SessionLookupIndex` Query |
| 2 | `_resolve_accessible_skill_ids` | RBAC (cached) + `SkillOwnerIndex` Query — only when `enabled_skills` is non-empty |
| 3 | `pop_pending_attachments` | `_get_session_by_gsi` |
| 4 | `ensure_session_metadata_exists` | `_get_session_by_gsi` |
| 5 | `clear_paused_turn` | `_get_session_by_gsi` |
| 6 | `clear_pending_interrupts` | `_get_session_by_gsi` |
| 7 | `clear_truncated_turn` | `_get_session_by_gsi` |
| 8 | `clear_interrupted_turn` | `_get_session_by_gsi` |
| 9 | `check_quota` → `_resolve_session_notice` → `get_session_metadata` | GSI Query (+ a lazy cost-aggregate backfill on legacy rows) |

**Rows 1, 3, 4, 5, 6, 7, 8 and 9 all read the same item** — the session's `META`
row — eight times in one turn, each on its own round trip. Rows 5–8 then
short-circuit on the happy path (`if "pausedTurn" not in existing: return`), so
four of the eight reads exist only to discover there is nothing to clear.

Three things multiply that.

**They are synchronous boto3 calls inside `async def`.** `_get_session_by_gsi`
calls `table.query(...)` directly, with no `asyncio.to_thread`. Each one blocks
the runtime's event loop for its whole round trip. This is the same shape as the
starved-timer bug PR-3 already paid for — a synchronous `create_agent` occupying
the loop so `asyncio.wait` never fired its timeout. Here the consequence is
throughput as well as latency: the runtime-session affinity header deliberately
packs a conversation onto a warm microVM, so these blocking hops serialize
against *other users' turns on the same container*.

**A fresh client per call.** `metadata.py` constructs `boto3.resource("dynamodb")`
in 28 separate function bodies; the repo has 89 such sites across `apis/` and
`agents/`, and no cached-client helper anywhere in `apis/shared/`. Measured
locally: ~327ms for the first construction in a process, **~1.5ms** each
thereafter. So this is ~12ms per turn — real, but not the story. Worth fixing
for the cold-container case and for tidiness, not for the warm number.

**No VPC endpoints.** `network-construct.ts` provisions one NAT Gateway and zero
interface or gateway endpoints. This does *not* affect the preamble —
inference-api runs inside an AgentCore Runtime container on AWS-managed network,
not our VPC — but every app-api DynamoDB and S3 call egresses through NAT. A
DynamoDB gateway endpoint is free and reduces both latency and NAT
data-processing spend. Tracked here because it was found on the same sweep; it
belongs to app-api's latency, not the preamble's.

### What is already fine

Named so nobody re-audits them: RBAC permission resolution, the quota tier
resolver, and the user cost summary are all TTL-cached in-process. Feature flags
are pure `os.environ` reads with no IO. Skill resolution short-circuits to `[]`
when the turn selects no skills, so it costs nothing on a plain turn. Title
generation is already an `asyncio.create_task` off the critical path.

## The honest status of the attribution

**The eight-read count is read off the code. The attribution of 450–900ms to it
is a hypothesis.** The mechanism is strong and the arithmetic is plausible — 8
serialized GSI queries at an in-region p50 of 10–15ms, p90 of 25–40ms, plus
event-loop blocking under concurrency — but `preamble` has no sub-marks, so
nothing here is measured.

This is exactly the trap CLAUDE.md's cost-effectiveness tenet names for prompt
tokens, and it applies identically to milliseconds: *don't guess — verify*. The
repo has been burned by the inverse before (`agent_status`'s "and N more" was
dead code for a pinned sequential executor; PR-3's first two suppression attempts
each looked right and each failed on dev for a reason the unit tests could not
see). A refactor justified by an unmeasured hypothesis is how that happens again.

So PR-1 measures, and nothing else.

## PR-1 — sub-stage marks (BUILT)

Decompose the single `preamble` mark into five ordered sub-stages, named with a
dotted prefix:

| Stage | Covers |
|-------|--------|
| `preamble.ownership` | the cross-user session guard (read #1) |
| `preamble.skills` | effective-skill resolution (read #2, absent on a turn with no skills) |
| `preamble.files` | attachment recovery + upload-id resolution + inline partitioning (read #3, plus S3) |
| `preamble.session_state` | metadata pre-create and the four stale-marker clears (reads #4–#8) |
| `preamble.quota` | the quota round trip (read #9) |

**`preamble` itself survives as a number.** `emit()` gains a `groups` field that
sums stages sharing a dotted prefix, so `groups.preamble` reproduces exactly what
the old single mark reported and the four-turn baseline above stays comparable.
Without that, decomposing the stage would have silently discarded the only
measurement anyone has.

**Cost.** Four extra `perf_counter()` calls and one extra log field per turn.
Nothing reaches the model, the conversation, or the cacheable prefix — the same
standing `turn_timing.py` has had since it shipped.

**Not gated behind a flag.** `TurnPrelude` is already best-effort in every
direction (`mark` swallows, `emit` swallows) and a flag would be a second thing
to get wrong for a measurement that cannot break a turn. The precedent is the
existing four marks, which shipped ungated for the same reason.

**How to read it.** Unchanged from `turn_timing.py`'s own docstring — one
structured line per turn on the inference-api runtime log group, via
`filter-log-events --filter-pattern turn_prelude`. What PR-1 changes is that
`stages` now names *which* sub-stage owns the time instead of reporting one
opaque number.

### What would falsify the hypothesis

Stated in advance, so the measurement cannot be read to agree with whatever we
already believe:

- **Hypothesis confirmed** if `preamble.session_state` dominates — it holds five
  of the eight reads and does no other work.
- **Hypothesis wrong, and PR-2 must not be built as written**, if
  `preamble.quota` or `preamble.files` dominates instead. Those are different
  bugs with different fixes: quota is one cached GetItem plus a session read, and
  files is S3 plus extraction, neither of which the single-read refactor touches.
- **Something unmodelled is going on** if the five sub-stages sum to visibly less
  than `groups.preamble`. The stages are contiguous by construction, so a gap
  means time is being spent between them — in code this spec has not accounted
  for.

## PR-1b — make the improvement provable (BUILT)

PR-1 makes the preamble *attributable*. It does not make a fix *provable*, and
those are different problems: attribution needs one turn, proof needs a
population.

### The field that would lie to us

There is already a persisted field called `time_to_first_token`
(`apis/app_api/messages/models.py`, computed in `stream_coordinator.py`). It is
the obvious instrument and it is the wrong one. It measures
`first_token_time - stream_start_time`, and `stream_start_time` is set *inside*
the coordinator's generator — which, since `agent-state-feedback.md` PR-3
deferred the build, runs after the preamble **and** after the agent build. So it
is structurally blind to:

| Excluded from `time_to_first_token` | Size |
|---|---|
| app-api hop + auth + Runtime routing | ~478ms warm, ~1.5s cold |
| the whole preamble | 450–900ms |
| the agent build | 0–2728ms |

**A successful PR-2 moves that field by exactly zero.** Anyone validating with
it concludes the work was pointless. This is the same trap this repo already
documented once for `latency.endToEndLatency` vs `turnDurationMs` — a field
whose name describes what you want and whose definition does not. Do not use it
here; it is a *model* TTFT, and it is correct at that job.

### Server side: percentiles, not grepped lines

`TurnPrelude.emit` now also writes one EMF record per turn into
`AgentCoreStack/TurnLatency`, through the existing
`apis/shared/observability/emf.py` helper — no new infrastructure, no new IAM.
Metric names are *derived* from the stage names (`preamble.session_state` →
`PreambleSessionStateMs`) rather than listed, so a new mark cannot silently go
unmeasured; the price is that a new mark quietly creates a metric stream
(~$0.30/month), which is the cheaper mistake.

Dimension-less, like every other EMF caller here. `isResume`, `deferredBuild`
and `sessionId` ride as queryable log properties and the dashboard's Logs
Insights widgets do the slicing. That is not only metric-stream cost: a
dimension invites reading a p99 off a slice too thin to have one.

`TurnLatencyObservabilityConstruct` graphs p50/p90/p99 per stage on **its own
dashboard** — the fourth, which costs $3/month.

That cost was argued, not absorbed. `observability-platform-dashboard.test.ts`
pins the dashboard count precisely so crossing CloudWatch's free three stays a
deliberate trade; the first attempt therefore folded the widgets onto the
AgentCore Runtime board to stay inside it. That version worked and was still
worse: this board is read *while shipping a latency change*, side by side with
load-test output, and burying the stage breakdown under runtime-health widgets
answering an unrelated question made it harder to use for its one job.
$3/month against a 450–900ms wait on every turn is not a close call. **The next
dashboard should have to make the same argument** — the pinned count now reads
"three free + one bought" rather than being raised to whatever is convenient.

The AgentCore Runtime board stays the natural companion: it graphs AWS's own
`Latency` p50/p90/p99 measured at the **data plane**, so the gap between it and
`PreludeTotalMs` is another read on the routing overhead no server-side stage
can see. Both dashboards' headers point at each other, and the platform health
dashboard links to all three drill-downs.

Alongside the percentile graphs are two widgets that exist to catch our own
errors: a split by turn shape (a resume skips most of the preamble, so a
traffic-mix shift toward resumes would look exactly like a latency win) and an
**unaccounted-time** widget
(`PreludeTotalMs − (PreambleMs + RagMs + ToolsMs + AgentBuildMs)`), which is how
we find out the decomposition is incomplete.

**No alarms.** A threshold needs a baseline and there is none yet. Inventing one
is the guessing this spec exists to prevent; add them once the dashboard has run
long enough to say what normal is.

**Kill switch:** `TURN_LATENCY_METRICS_ENABLED` (default on). Note this differs
from PR-1's marks, which are deliberately ungated — a `perf_counter()` call
costs nothing, while an EMF line costs log ingestion and custom-metric charges.
Same reasoning that gave `PROMPT_CACHE_OBSERVABILITY_ENABLED` its switch. Like
every other flag of its kind here it is not threaded through CDK.

### Client side: the part the server structurally cannot see

No metric in `AgentCoreStack/TurnLatency` sees the app-api hop, auth or Runtime
routing, because they all start at handler entry. That is up to 20% of the turn,
and it is where a fix can be real on the server and invisible to the user.

`tests/load` already covers it. The Locust harness measures **true client-side
TTFT** — dispatch of `POST /chat/stream` → first `content_block_delta` — through
CloudFront → ALB → app-api → AgentCore Runtime (`agentcore_load/users.py`).

And the architecture hands us a clean decomposition for free. Because PR-3
deferred the agent build into the stream generator, inference-api returns its
`StreamingResponse` **after the preamble but before the build**, so response
headers arrive at exactly the boundary between the two fixes:

| Measure | Covers | Which fix it judges |
|---|---|---|
| **TTFB** (Locust's built-in `POST /chat/stream` time — headers) | fixed hop + **preamble** | PR-2 |
| **TTFT − TTFB** | **agent build** + model | the MCP pre-flight fix |

**Verify this before trusting it.** It depends on headers flushing through the
AgentCore data plane ahead of the first body byte. The proxy's own half has a
regression test (`test_ttfb_under_200ms_with_x_accel_buffering`) but that test
mocks the upstream, so it proves the *proxy* does not buffer — not the data
plane. The check: run turns and compare Locust TTFB against `groups.preamble`
from the same turns. If TTFB tracks preamble, the channel is clean. If TTFB
tracks TTFT instead, the data plane is buffering and TTFB is useless as a
preamble proxy — fall back to comparing the EMF percentiles and accept that the
client-side half is only measurable end to end.

### Design the comparison as A/B, not before/after

Before/after across a deploy confounds with the two largest variance terms on
this path — container warmth and traffic mix — either of which can swamp the
effect being measured. The template already exists:
`scripts/probe_runtime_session_affinity.py` is a two-arm probe differing by
exactly one header, and its docstring makes the point that it deliberately
bypasses shared code rather than "building the fix to test the hypothesis."

### Acceptance criteria, stated before the data

| Claim | Passes if | Fails if |
|---|---|---|
| The eight reads are real cost | `PreambleSessionStateMs` p50 ≥ 100ms | < 40ms — the reads are cheap; drop PR-2 |
| PR-2 removes them | `PreambleMs` p50 falls ≥ 50ms | < 20ms — the reads were not the cost |
| It reaches the user | Locust TTFB p50 falls by the same amount | TTFB flat while `PreambleMs` falls — something downstream absorbs it |
| The decomposition is complete | unaccounted-time widget ≈ 0 | a visible gap — time is going somewhere unmodelled |

### The arithmetic that does not close

Stated plainly because it is the strongest argument against this spec's own
hypothesis. Eight serialized GSI queries at an in-region p50 of 10–15ms is
~100–150ms. The preamble is 460–494ms on a warm turn. **So even if the read
count is entirely right, PR-2 recovers roughly a quarter of the stage** — and
~300ms is unexplained by anything here.

Two readings, and PR-1's sub-marks separate them: either DynamoDB latency from
an AgentCore Runtime container is much worse than in-region typical (in which
case `session_state` is larger than modelled and PR-2 is worth more), or a large
part of the preamble is something not yet identified (in which case PR-2 is a
rounding error and the real win is elsewhere). Either way the answer arrives
before the refactor is written, which is the entire point of sequencing it this
way.

## THE MEASUREMENT (dev, 2026-09-19) — hypothesis confirmed, magnitude wrong

Three turns on dev immediately after deploy: one cold, two warm. All times ms.

| Turn | total | own | skills | files | **sess_state** | quota | **=preamble** | rag | tools | build | unacct |
|------|------:|----:|-------:|------:|---------------:|------:|--------------:|----:|------:|------:|-------:|
| cold | 4110 | 53 | 171 | 56 | **357** | 265 | **902** | 0 | 252 | 2951 | 5 |
| warm | 656 | 55 | 17 | 53 | **272** | 62 | **459** | 0 | 147 | 47 | 3 |
| warm | 591 | 53 | 5 | 53 | **278** | 62 | **451** | 0 | 136 | 1 | 3 |

Every acceptance criterion stated in advance passes:

| Claim | Threshold | Measured | Verdict |
|---|---|---|---|
| The eight reads are real cost | `session_state` p50 >= 100ms | **272-278ms** | **CONFIRMED** |
| The decomposition is complete | sub-stages ~= group | unaccounted **3ms** | **CONFIRMED** |
| The split preserved the baseline | ~= the old flat 494ms | **451-459ms** | **CONFIRMED** |

`session_state` is ~60% of the warm preamble, exactly as predicted.

### What was wrong: the per-read cost, by 4-5x

This spec estimated an in-region GSI query at 10-15ms and concluded PR-2 would
"recover roughly a quarter of the stage". **That was wrong, conservatively.**

`preamble.ownership` is exactly one GSI query and nothing else. It measures
**53-55ms**, dead stable across all three turns. `preamble.files` on a turn with
*no attachments* is also exactly one read (`pop_pending_attachments`) and also
measures **53ms**. So a DynamoDB GSI query from inside an AgentCore Runtime
container costs **~53ms**, not 10-15ms — 4-5x the in-region figure assumed here.

That single number closes the arithmetic that previously did not:

| Sub-stage | Reads of the META row | Measured | Implied per read |
|---|---:|---:|---:|
| `ownership` | 1 | 53 | 53 |
| `files` | 1 | 53 | 53 |
| `session_state` | 5 | 272-278 | ~55 |
| `quota` (session notice) | 1 | 62 | 62 |
| **total** | **8** | **~445 of the 451-459ms preamble** | |

**The warm preamble is almost entirely DynamoDB round trips reading one item
eight times.** The "~300ms unexplained" this spec worried about does not exist;
it was the per-read cost being four times higher than assumed. The spec offered
two readings and said the sub-marks would decide between them — the first
("DynamoDB latency from an AgentCore Runtime container is much worse than
in-region typical, in which case `session_state` is larger than modelled and
PR-2 is worth more") is correct.

### What this does to PR-2

Originally scoped as "collapse reads #3-#8". The measurement says go further:
**one read at the top of the handler can answer all four consumers**, because
`ownership`, `files`, `session_state` and the quota session-notice all read the
same item.

| | Now | After | Saved |
|---|---:|---:|---:|
| `ownership` | 53 | ~53 (the one read) | 0 |
| `files` | 53 | ~0 | 53 |
| `session_state` | 275 | ~0 | 275 |
| `quota` notice | 62 | ~0 | 62 |
| `skills` | ~10 | ~10 | 0 |
| **preamble** | **~455** | **~65** | **~390** |

A ~455ms stage becomes ~65ms, against an original estimate of 50-90ms saved.
**Still to be proven, not assumed** — the rule that produced this table applies
to the next one just as much.

### Caveats on this measurement

- **n=3, one session, one container.** Enough to size a 275ms effect against a
  +/-5ms spread; not enough for a distribution. The EMF percentiles are what
  make the post-PR-2 comparison sound, and they now have a baseline accruing.
- `skills` is 171ms cold vs 5-17ms warm — the RBAC/catalog caches working. Not
  a PR-2 target.
- `agent_build` 2951ms cold, then 47ms and 1ms warm. The cache works; the cold
  build remains the largest single number in the table and is the MCP pre-flight
  item below, untouched by PR-2.
- `StreamSetupMs` appeared as a metric nobody listed, because metric names are
  derived from marks rather than hand-kept. A listed table would have omitted it
  silently.

## PR-2 — read the session row once (BUILT)

**As built.** `load_session_meta()` performs one `SessionLookupIndex` query and
returns a `SessionMetaSnapshot` carrying two answers: the caller's own `META`
row, and whether the session belongs to someone else. Both were always in that
one response — `_get_session_by_gsi` simply discarded the half the ownership
probe needed, which is why the probe was a second query of the same index for
the same key.

The route reads once and threads the snapshot into `pop_pending_attachments`,
`ensure_session_metadata_exists` and the four `clear_*` helpers. Each takes
`snapshot: Optional[SessionMetaSnapshot] = None`; `None` keeps the original
per-call read, which is what every non-preamble caller still gets.

**Why the snapshot object and not the row dict.** `row is None` is a real
answer — "this user has no `META` row yet" — not "nothing was prefetched". Had
the helpers taken the row alone, a brand-new session would be indistinguishable
from an absent prefetch and would silently fall back to re-reading, so the first
turn of every conversation would keep the old cost. Pinned by
`test_a_snapshot_with_no_row_is_an_answer_not_a_cache_miss`.

**Why explicit and not a per-request memo.** Memoising inside
`_get_session_by_gsi` would be a smaller diff and the wrong shape. CLAUDE.md's
"never cache session state; re-read per turn; never move backwards" rule exists
because this repo shipped that bug twice (#741 conversation history, #751
compaction state). A snapshot a caller opts into cannot leak into a caller that
needs a fresh read; a request-scoped memo can, and the failure is silent.

**Scope.** `ownership`, `files` and `session_state` — the three sub-stages that
read this item. The quota session-notice read is deliberately NOT included; see
PR-2b.

Expected effect on the warm preamble, from the measured per-read cost:

| Sub-stage | Before | After |
|---|---:|---:|
| `ownership` | 53 | ~53 (now the only read) |
| `files` | 53 | ~0 |
| `session_state` | 275 | ~0 |
| `quota` (PR-2b) | 62 | 62 |
| `skills` | ~10 | ~10 |
| **preamble** | **~455** | **~125** |

Why it is safe: every one of those helpers is already best-effort and fail-open,
and the GSI is eventually consistent, so re-reading was never buying consistency
— two reads 50ms apart see the same stale-or-not row. The session's single-flight
lease already excludes a concurrent turn on the same session. The `SK` is static
(`S#{session_id}`), so the key the conditional writes target cannot drift between
the read and the write. And the atomic parts stay atomic: `pop_pending_attachments`
and `clear_interrupted_turn` still do their `ReturnValues=UPDATED_OLD` update, so
the snapshot only replaces the *gate* read, never the write.

The ownership guard keeps its 404, now answered from the same snapshot instead
of its own query.

**That gate is now passed.** `session_state` is where the time is (272-278ms of
a 451-459ms preamble), so the refactor of the marker helpers — load-bearing for
interrupt resume, the truncation "Continue" path and unconsumed-attachment
recovery — is paid for by a measured 275ms rather than by a guess. Scope widens
to include `ownership`, `files` and the quota session-notice, which read the
same item.

## PR-2b — the quota session-notice read (NOT STARTED)

The eighth read, worth a measured **62ms**. Left out of PR-2 because it is not
the same shape as the others.

`QuotaChecker._resolve_session_notice` calls `get_session_metadata`, which
returns a `SessionMetadata` model and performs a **lazy backfill** of the
cost aggregates when `totalCost` is missing from a legacy row. Handing it the
raw snapshot row would skip that backfill, and a legacy row without `totalCost`
would silently stop producing a session notice — turning a latency fix into a
quiet regression of the feature that exists to catch a conversation eating a
user's month.

The safe shape: use the snapshot when it already carries `totalCost`, and fall
back to `get_session_metadata` when it does not. Same saving in the common case,
backfill preserved in the uncommon one. Worth its own PR and its own test.

## PR-3 — `asyncio.to_thread` the blocking DynamoDB calls (NOT STARTED)

Independent of PR-2 and independent of the measurement: whatever the reads cost,
doing them on the event loop is wrong under concurrency, and the codebase already
uses `asyncio.to_thread` for exactly this in ~20 places. The session-metadata
layer never got it.

Worth its own PR rather than riding PR-2, because its win is throughput under
load and would be invisible in a single-user latency measurement — the two need
different evidence.

## Beyond the preamble

Found on the same sweep. Recorded so they are not re-derived; none is committed to.

- **The agent build scales linearly with MCP servers.** `load_external_tools`
  iterates enabled servers in a serial `for` loop, each doing a
  `repository.get_tool` plus a blocking `list_tools_sync()` round trip to an
  external MCP server — N cross-internet hops, one after another, inside the
  1478–2728ms cold build. Concurrent fetch plus a short-TTL cache of each
  server's listing is the largest cold-turn win available. **Constraint:** the
  merged tool order reaches `toolConfig`, which is the prompt-cache prefix, so
  the *merge* must stay deterministic even when the fetch is not. See CLAUDE.md
  § Prompt-cache stability.
- **A new `httpx.AsyncClient` per turn** in the BFF proxy — fresh DNS, TCP and
  TLS to `bedrock-agentcore.<region>` on every message, never pooled. Measured
  76–135ms from a laptop; in-region it is smaller but nonzero, and it sits inside
  the ~478ms the recap already knows it cannot see. The catch is the lifecycle
  comment already in `proxy_routes.py`: the client is deliberately closed in the
  generator's `finally` because closing it via `async with` buffers the whole SSE
  stream. A pooled module-level client has to outlive the request without
  reintroducing that.
- **A shared cached-boto3-client module.** 89 construction sites at ~1.5ms each.

## The other two open items from `agent-state-feedback.md`

Both were left open there. Neither is a bug, and neither needs a fix of its own:

- **The recap's ~1.5s shortfall** *is* the pre-container window — app-api hop,
  auth, Runtime routing. It is not a measurement error to correct but a real cost
  to remove: the pooled-client item above and the cold-start path are what shrink
  it. Measuring it instead would mean measuring on the client, which the
  "always on / survives reload" trade already declined.
- **The missing footer on older conversations** is the deliberate "no number
  beats a number nobody measured" rule, consistent with the tool-rail durations
  and with `agent_status`'s non-persisted timings. Backfilling it would mean
  inventing the number.

## Non-goals

- **Narrating the preamble.** `agent-state-feedback.md` PR-3 settled this: on a
  warm turn no stage dominates and 41% of the wait is not even in the handler, so
  there is no honest phase label. Making it shorter is the only lever.
- **Restructuring the handler wholesale.** The measurement is what decides how
  much structure has to move; deciding that first is how PR-3's original plan
  turned out to be unbuildable.
- **Deferring the preamble into the stream generator** (the structural version of
  this fix, which would take TTFB to near zero). Genuinely attractive, and
  explicitly out of scope until PR-1 says how much is left to win after the cheap
  fixes. It would require converting the remaining HTTP-status guards to SSE
  errors — which is the house rule anyway — but it is a much larger change than
  anything above and should not ride a measurement PR.
