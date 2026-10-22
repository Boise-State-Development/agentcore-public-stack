# Agent state feedback

**Status: CLOSED 2026-09-19.** Every state shipped and observed working on
dev. PRs #1159, #1160, #1163, #1165, #1167, #1170, #1173, #1176, #1178, #1180.
See "Closing summary" at the foot of this document.
**Follow-up to:** `d2ee13e2` (emit agent_status and tool-batch summaries), `9bc9bc6b` / `5f0cd52a` / `67234329` (loading-indicator series)
**Related:** `docs/specs/mid-turn-steering.md` (the other consumer of the drain), CLAUDE.md § SSE Event Types → `agent_status`,
`docs/specs/turn-latency-preamble.md` — which opens the `preamble` stage this spec
measured and then left closed. That stage is the largest remaining pre-stream wait
and is deliberately NOT a status problem: no label makes it shorter.

## Problem

The live indicator shipped in `9bc9bc6b` replaced twenty invented phrases with
facts, and that was the right trade. But it settled at two states — `Thinking…`
and `Running <tool>…` — and a turn has more shape than that. Time the user
currently cannot account for:

* **Agent build.** Tool-registry assembly, MCP `tools/list` pre-flight round
  trips, session restore from AgentCore Memory, Runtime cold start. On an
  agent-cache miss this is the longest unexplained pause in the product.
* **Head-of-turn context work.** `apply_pending_compaction` and
  `apply_document_offload` both run before the first model call
  (`stream_coordinator.py:283`). The `compaction` SSE that describes them fires
  *after* `metadata`, purely retrospectively.
* **Model generation vs. tool execution.** Both read `Thinking…` today unless
  the content stream happens to carry an unresolved `toolUse` block.
* **Reasoning.** A reasoning block's header is the static string `Thinking`
  with no duration, live or afterwards.
* **Queued behind the single-flight lease.** Indistinguishable from a hang.

## What already exists — and is thrown away

Two findings shape everything below.

**1. `thinking` and `tool_start` cross the wire and are dropped on the floor.**
`ToolInsightService.recordStatus` writes every `agent_status` transition into a
`statusBySession` signal. Nothing reads it — `statusFor()` has zero callers in
the SPA. Only `tool_end` is consumed, for the per-tool durations on the rail.
The loader's label is derived from the *content* stream instead
(`message-list.component.ts:331`).

**2. That is not a style choice. It is a latency bug.**
`_drain_agent_status_events` runs between yields of the agent stream
(`stream_coordinator.py:1051`). During tool execution Strands yields nothing, so
a `tool_start` sits queued for exactly the silence it exists to explain, and
arrives bundled with its own `tool_end`. The measurement recorded in
`message-list.component.ts:313` — a three-tool browse turn — read `Thinking` for
all 4.5s and never showed a tool name.

**Every phase added through `AgentStatusHook` inherits this.** Decoupling the
drain is therefore a precondition for PR-3, not a cleanup task.

## Non-goals

* **A `responding` phase.** The SPA knows text is streaming from the deltas
  first-hand. A backend-derived duplicate would only disagree at the edges.
  (Already settled in `agent_status.py`; restated here so it is not re-litigated.)
* **"Almost done thinking…".** Claude can say this because it knows the
  thinking budget and how much is spent. Deriving it from elapsed time is an
  inference wearing a fact's clothes. The existing stall ladder — `Still
  working…` → `…longer than usual` — gives the same reassurance and claims only
  what it knows.
* **Per-tool invented phrasing** ("Searching the catalog…", "Browsing…").
  A tool's own name is the most accurate label available and invents nothing.
* **Persisting durations.** Unchanged from the `agent_status` contract: a
  reloaded conversation shows summaries without timings rather than a number
  the user cannot trust.

## Cost

Nothing in this spec reaches the model. No phase, label, or duration is appended
to the conversation, so the cacheable prefix is untouched — the same standing
this feature has had since `d2ee13e2`. PR-2 adds SSE frames but no model tokens.

## PR-1 — "Thought for 17s" (SHIPPED, #1159)

**Scope:** SPA only. No backend change, no dependency on the drain fix.

The reasoning block header (`reasoning-content.component.ts:48`) says `Thinking`
forever, including on a block that finished nine minutes ago. It becomes
`Thought for 17s` the moment the model stops reasoning.

**Measurement.** Client-observed, first-hand: the span from the first reasoning
delta to the moment the model demonstrably switched to output. This differs from
the tool durations on the rail, which are measured inside Strands' event loop
and shipped over `agent_status`. It is stated as such in the code, because the
two are not the same kind of number and should not be presented as though they
were.

**Why a client-side span is sound here.** The agent loop starts a new Bedrock
message at every tool round trip (`assistant-message.component.ts:310`), and
`handleReasoning` merges into the first reasoning block of the *current*
message. A reasoning block therefore never spans a tool call, so first-delta to
last-output-switch cannot silently absorb tool time. The undercount is the
reasoning's own time-to-first-token plus the trailing gap before output — both
sub-second, both absorbed by whole-second rounding.

**End of reasoning** is stamped at the first of:
1. a non-reasoning `content_block_start` in the same message,
2. a non-reasoning `content_block_delta` in the same message,
3. `message_stop`.

(1) and (2) are the accurate moments; (3) is the backstop for a cycle that
produced reasoning and nothing else.

**Live-only, by construction.** The duration rides `ContentBlock`, which only
the stream parser populates. `GET /messages` never sets it, so a reloaded
conversation falls back to `Thinking` with no extra code and no persisted
number — the same posture as the tool rail's `totalDurationMs`.

**Placement on the model.** `reasoningDurationMs` sits at the top level of the
SPA's `ContentBlock`, deliberately *not* inside `reasoningContent`. That nested
object is Bedrock's Converse shape; a display string there would be the mistake
CLAUDE.md calls out for `tool_group_summary`.

**Formatting.** `<1s`, `3s`, `17s`, `1m 12s`. Never rounds a sub-second block up
to `1s`.

## PR-2 — decouple the drain, then trust `agent_status` (SHIPPED, #1160)

**Backend.** `AgentStatusHook` pushes to an `asyncio.Queue` instead of a list,
and the coordinator merges that queue with the agent stream rather than polling
it between yields. The queue caps (`_MAX_QUEUED_STATUSES`) and the
per-turn reset at `BeforeInvocationEvent` carry over unchanged; the reset is
what keeps an interrupted turn's transitions out of the next one.

**SPA.** Wire the loader's label to the now-live `statusFor()`:

| Phase | Label |
|-------|-------|
| `thinking` | `Waiting for the model…` |
| `tool_start` | `Running <tool>…` |
| batch of >1 | `Running <tool> and 2 more…` |

**Also fixes parallel batches.** `loaderStatusTool` returns the *first*
unresolved `toolUse` on the streaming message, so three tools in flight names
one of them arbitrarily. With a trustworthy `tool_start`/`tool_end` pair the
count is known.

**Keep the content-stream derivation as the fallback.** It is strictly more
current when it fires, and it is the only source that works if a future SDK
change starves the queue.

**Found while building it.** The loader stays mounted for the WHOLE turn —
`isChatLoading` clears at stream close, not at the first token — so a label
keyed on the `thinking` phase would contradict text the user can already read.
"Waiting for the model" is therefore gated on the answer still being silent,
falling back to the previous wording once text arrives. The existing
`loaderStatus` docstring asserts the loader "is gone anyway" at that point;
that comment is wrong about the code and was deliberately left alone rather
than widen this PR. Worth revisiting on its own: the right fix is probably that
the loader should stop being mounted under a streaming answer at all, which is
a change to when it renders, not to what it says.

## Verified on dev (2026-09-19)

Measured by teeing the SSE body in the browser and timestamping frames from the
click. Recipe: patch `window.fetch`, `res.body.tee()`, split on `\n\n`.

**The drain works.** `tool_start` landed at 4396ms against `tool_result` at
4769ms — the status frame beat the event it describes by 373ms, which the old
between-yields drain could not do by construction. The 100ms poll cadence is
visible as quantization in the frame timestamps.

**The batch count was dead code and has been removed.**
`agent_factory.py` pins `tool_executor=SequentialToolExecutor()` so concurrent
browser tools cannot start two Playwright sessions. A three-tool batch therefore
emits strictly interleaved `start,end,start,end,start,end` and more than one
tool is never in flight. PR-2's description claimed "a parallel batch is finally
legible"; that was wrong as shipped. `ToolInsightService.runningTools` keeps the
list shape, which costs nothing and is what a concurrent executor would need.

**The content-stream fallback still drives the visible label**, because it fires
when the model starts streaming a tool's ARGUMENTS while `tool_start` fires when
the tool starts EXECUTING — a measured ~640ms gap in which the UI named a tool
that was not yet running. Which is preferable is a product call, not a bug.

**Where the time actually goes** (warm container, artifact turn, from click):

| From click | Event |
|-----------:|-------|
| 2ms | request dispatched |
| **3750ms** | **first SSE byte** |
| 4757ms | `agent_status thinking` |
| 5483ms | `message_start` |
| 6111→6311ms | `tool_start` → `tool_end` (225ms of actual execution) |
| 7506ms | `done` |

A cold turn measured **6.7s** before the first status frame.

## PR-3 — narrate the cold agent build (BUILT)

The original plan — emit a phase per pre-stream stage from the chat route —
could not be built as written, and the measurement that proved it also made it
unnecessary. What is left is much smaller.

### Why the original plan was impossible

FastAPI flushes response headers when the handler returns its
`StreamingResponse`, and `inference_api/chat/routes.py` awaits `get_agent(...)`
— along with model resolution, RAG retrieval and tool building — *before* that
return. During the whole window the plan wanted to narrate, **no SSE channel is
open**. app-api cannot cover it either: `chat/proxy_routes.py` awaits the
upstream response before constructing its own `StreamingResponse`.

### What the measurement says

`turn_prelude` on dev, four turns (2026-09-19):

| Turn | total | preamble | rag | tools | agent_build |
|------|------:|---------:|----:|------:|------------:|
| cold agent cache | 2542 | 802 | 0 | 261 | **1478** |
| warm | 641 | 494 | 0 | 146 | 0 |
| warm | 644 | 460 | 0 | 149 | 34 |
| warm | 678 | 484 | 0 | 154 | 38 |

Against a client-side click→first-byte of **1156ms** on that last turn, the warm
budget is ~478ms outside the handler (app-api hop, auth, Runtime routing), 484ms
`preamble`, 154ms `tools`, 38ms `agent_build`.

Two conclusions:

1. **A warm turn does not need narrating.** ~1.2s to first byte with no stage
   dominating, and 41% of it not even in the handler. There is no honest phase
   label that helps, and the loading indicator already covers it.
2. **A cold agent build is the whole problem.** 1478ms in one stage, and the
   6.7s cold turn measured earlier is worse. It is the only place in the
   prelude where a phase label earns its keep.

That second conclusion is about *narration*, and it should not be read as a
verdict on `preamble`. On a warm turn `preamble` is the largest stage in the
table — 460–494ms of a 641–678ms total — and nothing here looked inside it. It
is taken up in `docs/specs/turn-latency-preamble.md`, as a latency problem
rather than a status one.

### The plan

**Wrap `get_agent` only.** Move that one call inside the stream generator and
emit `Getting ready…` before it. Leave `preamble`, `rag` and `tools` eager where
they are. This sidesteps most of what made a handler restructure frightening:
the quota check, the session-ownership 404 and every other HTTP-error-capable
guard live in `preamble`, which stays ahead of the stream.

**Feasibility check: PASSED.** The only `raise HTTPException` between
`get_agent` and the `StreamingResponse` return is guarded by `if is_resume:`
(interrupt-id validation). Resume keeps its eager build, so nothing on the
deferred path can need an HTTP status after the first byte.

**As built**, three things the plan did not anticipate:

1. **The frame is emitted unconditionally; the SPA decides whether to show it.**
   A warm build is 0-38ms, and "Getting ready" for 38ms is a flicker — landing,
   worse, *after* the generic "Thinking" the client shows from the moment the
   user hits send, which reads as going backwards.

   The first attempt raced the build against a 250ms timer **in the
   generator** and emitted only if it was still running. **That cannot work,
   and dev proved it:** `create_agent` is synchronous
   (`agent_factory.py`), so a cold build occupies the runtime's event loop for
   its whole duration and `asyncio.wait` never gets to fire its timeout — it
   returned only once the build was already finished, `finished` was non-empty,
   and the frame was never sent. A 1548ms build, six times the threshold,
   emitted nothing.

   A timer only works where the clock actually runs, which is the client. The
   backend now always announces the build; `message-list.component.ts` holds
   the phase for 250ms before rendering it, so a warm build is superseded by
   `thinking` and never reaches the screen. Cost of the change: one extra SSE
   frame per non-resume turn.

   (Peeking `_agent_cache` to predict a miss was considered and rejected both
   times: it means rebuilding its key out in the route, and a key that drifts
   from the real one is a bug this repo has paid for.)

      **Re-verified end to end (2026-09-19, session `9786b58b`)** on a cold
   container, after the turn-duration fix:

   | Measure | Value |
   |---------|------:|
   | Client stopwatch (click → `done`) | 7821ms |
   | Footer / `turnDurationMs` | 6291ms |
   | `metrics.latencyMs` (model only) | 2364ms |
   | `turn_prelude` handler total | 3862ms — preamble 883 · tools 250 · **agent_build 2728** |

   The label sequence was `Thinking… → Getting ready… → Waiting for the model…`
   with no step backwards, and `turnDurationMs` is plainly distinct from
   `latencyMs`, which is what proves it is no longer the post-build remainder.

   **The suppression did not work in production, and a third fix was needed
   (2026-09-19).** A controlled four-turn session on one container:

   | Turn | `agent_build` | Cache | "Getting ready…" |
   |------|--------------:|-------|------------------|
   | 1 | 1631ms | miss | correct |
   | 2 | **1ms** | hit | **wrongly shown** |
   | 3 | **40ms** | hit | **wrongly shown** |
   | 4 | **0ms** | hit | **wrongly shown** |

   `preparing` was never a state with an end — it was just the latest event.
   The build finished in 1ms, but `thinking` does not arrive until the
   head-of-turn work and the event loop's startup have also run, hundreds of
   ms later, so the client's settle timer fired on a build that was long over.
   The unit tests passed because they fed `thinking` 40ms after `preparing`,
   a sequence the backend never emits.

   The backend now emits an explicit **`prepared`** frame when the build ends,
   carrying its measured duration. The SPA treats it as the end of the wait.
   The lesson generalises past this feature: a phase that only ever means
   "most recent event" cannot express a wait that has finished, and a test
   that invents the next event will agree with whatever the code does.

   **Verified on dev (2026-09-19), and it found one more bug.** Five turns,
   builds of 1504ms / 549ms / ~650ms×3, all narrated correctly — turn 1 showed
   "Getting ready…" for 2.1s of a 2.3s wait. But the label stepped *backwards*
   on the way out — "Getting ready…" → "Thinking…" (~40ms) → "Waiting for the
   model…" — on all five. The settle flag was cleared when the phase left
   `preparing`, which opened a propagation window where the label read the old
   phase and the new flag. It is now armed on the way IN and never cleared on
   the way out; the fast-build case is suppressed by cancelling the pending
   timer, which is what was doing that work anyway.

   **Still unverified in a browser:** fast-build suppression. Every turn in the
   session missed the agent cache (~650ms builds), so the warm path was never
   observed live. The logs prove warm builds exist (`agent_build` of 1ms and
   41ms on other sessions) and the suppression is unit-tested.
2. **A failed build needs its own error path.** The handler has already
   returned by then, so neither `except` arm can see it; without an in-generator
   catch a build failure is a silent hung stream. It now surfaces as a
   conversational error (the house rule) and the `finally` still releases the
   lease.
3. **The SPA validator had to be relaxed.** It required
   `typeof cycle === 'number'`, and `preparing` precedes the event loop so it
   carries no cycle — every frame would have been dropped silently. The
   relaxation is scoped to `preparing`; the other phases still require a cycle,
   because the SPA uses it to tell event-loop passes apart.

**Kill switch:** `AGENT_PREPARING_PHASE_ENABLED` (default on). Off restores the
eager build exactly. Its own flag rather than riding `AGENT_STATUS_ENABLED`,
which gates narration — this changes when the agent is built.

**Cost:** the label is one SSE frame. Nothing reaches the model.

### Declined

- **A client-side label for the pre-first-byte window.** Unnecessary once the
  narrow fix lands, and it could only name a window, not a phase.
- **Restructuring app-api's relay.** It would cover the ~478ms outside the
  handler, which is not where the pain is, and it would force upstream HTTP
  errors to become SSE `stream_error` frames.
- **Restructuring the whole inference-api handler.** The measurement says three
  of its four stages are not worth narrating, so the risk buys nothing.

### Original target (superseded, kept for the record)


These are not Strands hook events; they happen before the loop exists, so they
need emit points in the invocation path.

| Phase | Label | Emit point |
|-------|-------|------------|
| Agent build | `Getting ready…` | inference-api chat route, around `_create_agent` |
| MCP pre-flight | `Connecting tools…` | around the pre-flight `tools/list` |
| Head-of-turn context work | `Reorganizing context…` | `stream_coordinator.py:283–300` |
| Queued behind the lease | `Finishing your previous message…` | single-flight acquire |

`Connecting tools…` is worth its own label rather than folding into `Getting
ready…`: it is the phase that fails (401, timeout) and a user who saw it named
has a chance of connecting the notice that follows to the pause that preceded it.

**Open question for PR-3.** These precede `message_start`, and the SPA's loader
currently starts on the request's falling edge. Whether they arrive as
`agent_status` frames with new phase values or as a distinct pre-turn event is
not yet decided; `agent_status` is preferred if the drain refactor in PR-2 makes
a pre-loop emitter practical.

## Turn recap (BUILT)

When a turn ends, everything describing it disappears: the loading line goes
and takes the elapsed timer with it. The rail keeps per-tool durations, but
nothing said how long the turn took. A finished turn now carries a one-line
footer — `9.6s · 4 tools` — anchored to the END of the turn, because that is
what it describes: a turn spans several assistant messages and the number
covers all of them plus the tools and the agent build between.

**It needed a new field, and that is the interesting part.** The obvious
source, `latency.endToEndLatency`, is not the number it appears to be: on the
persist path it prefers the provider's own API-call time
(`stream_coordinator._store_message_metadata`), so summing it across a turn
drops tool execution and the pre-stream agent build — a turn the user watched
for 9s reads as 3s. It also disagrees with itself: the LIVE `metadata` event's
`metrics.latencyMs` IS the whole turn, so the same field would show one number
during the turn and a smaller one after a refresh.

So `turnDurationMs` is explicit: measured server-side from the invocation
arriving to the stream ending, emitted on the live event AND persisted on the
turn's last message. One field, one meaning, identical live and reloaded.

**Known limits, both deliberate:**

- It starts when the invocation reaches inference-api, so it **excludes
  everything before the container**: the app-api hop, auth, and Runtime
  routing. That shortfall is **~1.5s on a cold path**, not the ~478ms first
  recorded here — that earlier figure came from a warm turn, and routing to a
  cold container costs substantially more. Measured end to end on dev
  2026-09-19: a turn whose client stopwatch read 7821ms reported **6291ms**,
  short by 1530ms, or 20% of the turn.

  So the recap is honest about the server's turn and reads visibly under the
  wait the user feels. Matching the stopwatch means measuring on the client,
  which cannot survive a reload — the trade "always on" already decided.
- **It must be measured from the handler, not from `stream_response`.** Those
  were the same thing until PR-3 deferred the agent build into the stream
  generator, which runs BEFORE that generator is iterated — so the
  coordinator's own `stream_start_time` now excludes the build. Shipped that
  way and caught on dev: a turn the user waited 7.8s for reported **2.1s**,
  exactly the post-build remainder. The route hands the coordinator
  `TurnPrelude.started_at` instead. Note that property is deliberately WALL
  clock while the stage marks are `perf_counter`: the coordinator subtracts it
  from a `time.time()` reading, and mixing domains gives a meaningless number
  rather than a slightly wrong one. The alternative — a client-measured
  click-to-`done` — is truer to the felt wait but cannot survive a reload,
  which "always on" requires.
- Turns written before the field show **nothing** rather than a zero. Same
  rule as the tool-rail durations: no number beats a number nobody measured.

---

## Closing summary

### What a turn says now

| When | What the user sees | Source |
|------|--------------------|--------|
| Model call being retried | `The model is busy. Retrying…` (amber) | `model_retry` |
| 30s / 90s of silence | `Still working…` → `…longer than usual.` (amber) | client timer |
| Tool executing | `Running <tool_name>…` | `agent_status` `tool_start`, content stream as fallback |
| Agent being built (>250ms) | `Getting ready…` | `agent_status` `preparing` / `prepared` |
| Model call in flight | `Waiting for the model…` | `agent_status` `thinking` |
| Anything else | `Thinking…` | fallback |
| Throughout | elapsed timer | client |
| After a reasoning block | `Thought for 17s` | client-measured span |
| After a tool batch | per-tool durations + a model-written summary | `tool_end`, `tool_group_summary` |
| After the turn | `9.6s · 4 tools` | `turnDurationMs` |

### Declined, with the measurement that settled it

- **`Connecting tools…`** — `tools` measured 250ms. Nothing to explain.
- **`Reorganizing context…`** — `rag` measured 0ms on a plain turn.
- **A full inference-api handler restructure** — three of its four pre-stream
  stages do not need narrating, so the risk bought nothing.
- **Restructuring app-api's relay** — would cover the ~1.5s outside the
  container, which is real but is a latency problem, not a narration one.
- **`Almost done thinking…`** and a **`responding`** phase — see Non-goals.

### What this cost, and what it taught

Four defects shipped to dev and were caught there. **All four passed CI**, and
all four were about timing or interaction, which the unit tests could not see:

1. **A server-side timer that could not fire** (#1167). `create_agent` is
   synchronous, so a cold build owns the event loop and `asyncio.wait` never
   reaches its timeout. The feature was inert in production from the moment it
   shipped — silently. The test faked the build with `asyncio.sleep`, which
   yields; the real one does not.
2. **A label that stepped backwards** (#1170). A flag cleared before the phase
   did, so `Getting ready…` → `Thinking…` → `Waiting for the model…` on every
   turn, ~40ms of it.
3. **A turn duration that excluded the wait it described** (#1176). Deferring
   the agent build moved it outside the window `stream_response` measures. A
   7.8s turn reported 2.1s.
4. **A label that overstayed** (#1180). `preparing` had no end, so the client
   inferred one from `thinking` — which arrives hundreds of ms later. Builds of
   0ms, 1ms and 40ms all rendered it. The test sent `thinking` 40ms after
   `preparing`, **a sequence the backend never emits**.

Three of the four share one root cause: **treating "most recent event" as if it
were "current state"**. An event stream says what just happened, not what is
still true, and a phase with no explicit end cannot express a wait that is over.

The practical lesson for anyone extending this: a test that invents the next
event agrees with whatever the code does. Model the real frame order, including
the gaps — and for anything whose correctness IS its timing, treat green CI as
weak evidence and go watch a turn.

### One thing measurement killed

`#1171` was filed claiming the agent cache missed on most consecutive turns.
It does not. That data was gathered while repeatedly testing immediately after
deploys, when containers were cycling. A session sticks to its container and
the cache hits from the second turn on (measured: 1631ms, then 1ms / 40ms /
0ms). Closed as not reproducible.

The one real residual is that the **first** turn of every session pays a full
build, because `session_id` is part of the cache key. That is by design and is
a different, narrower question than the one that issue asked.

### Left on the table, deliberately

- **`preamble` costs 450-900ms on every turn** and nobody has looked inside it.
  It is the largest remaining avoidable wait. Not a status gap — narrating it
  would explain nothing — but worth its own investigation.
- **The recap reads ~1.5s under the user's stopwatch**, excluding everything
  before the container. Fixing that means client-side measurement, which
  cannot survive a reload.
- **Conversations from before the recap** show no footer until their next turn.
