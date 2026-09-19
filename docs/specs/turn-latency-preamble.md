# Turn latency: inside the preamble

**Status:** PR-1 (sub-stage instrumentation) BUILT. PR-2+ NOT STARTED — they are
deliberately blocked on PR-1's measurement.
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

## PR-2 — read the session row once (NOT STARTED, blocked on PR-1)

Sketched, not committed to. Read the `META` row once at the top of the handler
and pass the item to the five marker helpers, which all need the same two things:
the row's `SK`, and their own attribute. Collapses reads #3–#8 to one.

Why it should be safe: every one of those helpers is already best-effort and
fail-open, and the GSI is eventually consistent, so re-reading was never buying
consistency — two reads 5ms apart see the same stale-or-not row. The ownership
guard (#1) stays its own eager read because it gates a 404.

Why it is still gated on PR-1: if `session_state` is not where the time is, this
buys ~nothing and spends a refactor of the marker helpers — which are load-bearing
for interrupt resume, the truncation "Continue" path, and unconsumed-attachment
recovery — on a guess.

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
