# Mid-turn steering

**Status:** in progress — PRs 1–5 built (end to end); PR-6 open, pending a dev evaluation
**Follow-up to:** PR #916 (`feature/queue-followup-instead-of-interrupt`)
**Refs:** `docs/kaizen/reviews/2026-08-28.md` proposal #5

## Problem

PR #916 made Enter mean "say this" while a response is streaming: the follow-up
is queued in the composer and flushed on the turn's falling edge. That fixed the
destructive part of the old behaviour (Enter routed to Stop, killing a run the
user was waiting on) but it leaves the follow-up sitting until the whole turn
finishes. A user who sees the agent open the wrong file, search the wrong index,
or start down a plainly wrong path still has exactly two options:

1. **Wait.** Pay for the rest of a turn whose direction is already known to be
   wrong, then correct it on the next one.
2. **Stop and resend.** Discard a partially generated turn, and re-establish
   against a prefix that the abandoned tail has now changed.

Both are pure waste, and (2) is the more expensive of the two — the pattern the
cost-effectiveness tenet exists to catch. What the user wants is what every
mature agent harness does: the follow-up lands at the **next tool boundary**, so
the agent reads it before choosing its next action.

The scope note on #916 said this "needs the backend to accept an injection into
a running turn, which it has no path for today." That is true of the *request*
path — `/invocations` is one-shot and the Runtime data plane proxies nothing
else — but both halves of the mechanism already exist in this codebase for other
reasons. They have simply never been connected.

## What already exists

**A side channel into a running turn.** Stop already does this. `request_session_cancel`
stamps `cancelRequestedFor` on the session's single-flight lease row
(`apis/shared/sessions/session_lease.py:298`); the container running the turn
observes it on its next heartbeat (`_heartbeat_session_lease`,
`apis/inference_api/chat/routes.py:145`) and flips `session_manager.cancelled`.
The arming endpoint is app-api's `POST /sessions/{id}/interrupt`
(`apis/app_api/sessions/routes.py:644`). The hard part — an owner-scoped,
cross-container signal that survives the Runtime routing the arming request to a
different container than the one streaming — is built, in production, and
already proven by the Stop path.

**An injection point in the agent loop.** Strands 1.51 fires `AfterToolsEvent`
with the assembled tool-result message **before** it is appended to history:

```
strands/event_loop/event_loop.py:857   after_tools_event = AfterToolsEvent(agent=agent, message=tool_result_message, ...)
strands/event_loop/event_loop.py:886   await agent._append_messages(tool_result_message)
```

A hook that appends a `{"text": ...}` block to `event.message["content"]` puts
the user's words into the same user-role message that carries the tool results.
That is a valid Bedrock Converse shape, it persists through the normal
`append_message` path (`turn_based_session_manager.py:125`), and it is
append-only against the cached prefix.

Async hook callbacks are supported (`HookRegistry.invoke_callbacks_async` awaits
coroutine callbacks), so the hook can read the inbox itself rather than waiting
on the 10 s heartbeat.

## Decision summary

| Question | Decision |
|----------|----------|
| Transport into the running turn | **Lease-row inbox** — same item, same owner-scoping, as `cancelRequestedFor` |
| Arming endpoint | **app-api** `POST /sessions/{id}/steer` (mirrors `/interrupt`) |
| Injection point | **`AfterToolsEvent`** — append a text block to the tool-result message |
| Injection granularity | **Next tool boundary.** Not mid-generation, not per-token |
| Pickup latency | **Read in the hook** (one GetItem per tool boundary), heartbeat as backstop |
| Consumption semantics | **Commit-on-append** — the inbox entry is cleared only once the message is actually in history |
| Turns with no tool call | **Fall back to #916's end-of-turn flush.** Both paths stay |
| Client contract | New SSE event **`steering_applied`** acknowledges the injection |
| Feature flag | `MID_TURN_STEERING_ENABLED`, **default on with a kill switch** (house style) |
| Failure posture | **Fail-soft** — any error degrades to #916 behaviour, never drops the user's text |

## Design

### D1 — Transport: the lease row is the inbox

`POST /sessions/{id}/steer` on app-api, body `{"text": "...", "clientId": "<uuid>"}`.
It reads the lease item's current `leaseOwner` and conditionally writes:

```
SET steerFor = :owner, steerQueue = list_append(if_not_exists(steerQueue, :empty), :entries)
ConditionExpression: leaseOwner = :owner
```

Each entry is `{"id": clientId, "text": text, "at": iso8601}`.

- **Owner-scoped, exactly like cancel.** If the turn ended between the read and
  the write, the condition fails, the endpoint returns `409`, and the SPA falls
  back to sending the message as a normal turn. That race is the *correct*
  outcome, not an error to paper over.
- **No new item, no new GC.** `release_session_lease` deletes the whole lease
  row at turn end, so an unconsumed inbox cannot outlive its turn.
- **Why app-api, not inference-api.** The Runtime data plane proxies only
  `/invocations` and `/ping`; a steer route on inference-api would 404 in cloud.
  Same reasoning that put `/interrupt` on app-api.

Size: composer text is small and the row is deleted per turn, so the 400 KB item
limit is not a live concern. Cap the queue at a small N (say 5 entries / 8 KB
total) and reject beyond it, so a pathological client cannot grow the row.

### D2 — Injection: `AfterToolsEvent`, not the alternatives

A new `SteeringHook` registered in `BaseAgent._create_hooks`
(`agents/main_agent/base_agent.py:285`) alongside `StopHook`:

```python
async def _inject(self, event: AfterToolsEvent) -> None:
    if not event.message["content"]:      # nothing committed this batch
        return
    pending = await self._inbox.peek()    # does NOT consume
    if not pending:
        return
    event.message["content"].append({"text": _wrap(pending)})
```

`_wrap` frames the injection so the model reads it as the user speaking, e.g.

```
<user_message_during_turn>
{text}
</user_message_during_turn>
```

Alternatives considered:

- **Interrupt / resume** (`BeforeToolCallEvent` → `event.interrupt(...)`, SPA
  resumes with the steering text as the interrupt response). Reuses fully
  sanctioned machinery — the same path OAuth consent and tool approval take —
  and is the safest option on paper. Rejected as the primary design because it
  costs a stream teardown, a re-invocation, and a `PausedTurnSnapshot` rebuild
  *per steer*, on a path whose whole point is being faster than waiting for the
  turn to end. Kept as the documented fallback if D2 proves unstable (see Risks).
- **Mutating `agent.messages` directly from a `BeforeModelCallEvent` hook.**
  Racier and less well-defined: the tool-result message is already committed by
  then, so the injection becomes a second user message, breaking the
  user/assistant alternation Bedrock expects after a `toolUse` turn.
- **A synthetic tool the model can call to check for user input.** Costs a
  `toolConfig` entry on every turn for every user, forever — exactly the kind of
  always-on prefix growth the cost tenet forbids — and only fires when the model
  chooses to look.

**SDK-boundary caveat.** `HookEvent.__setattr__` is write-guarded by
`_can_write`, which for `AfterToolsEvent` allows only `end_turn`. Mutating the
message dict *in place* is not blocked, but it is also not explicitly sanctioned.
Pin `strands-agents` (already exact-pinned per house rules) and add a contract
test that asserts the mutation still reaches `agent.messages`, in the same spirit
as `tests/agents/main_agent/core/test_bedrock_cache_points.py`. That test is the
canary for an SDK bump; the interrupt/resume variant is the escape hatch.

### D3 — Consumption: commit-on-append, never commit-on-read

`AfterToolsEvent` fires from a `finally` block, so it also fires on the cancel,
error, and **interrupt** paths. On the interrupt path `_stop_for_interrupts`
runs and `agent._append_messages(tool_result_message)` is **never reached** — the
message the hook just mutated is discarded and the turn pauses.

A hook that deletes the inbox entry when it reads it therefore destroys the
user's message on every steer that happens to land on the same tool batch as an
OAuth consent or an approval prompt. Silent data loss, low frequency, very hard
to reproduce.

So: the hook **peeks**. The entry is cleared only when the message is confirmed
in history — on the `MessageAddedEvent` for that same message object, which is
the first point at which the injection is real. The clear is a conditional
DynamoDB write scoped to both the lease owner and the entry id, so a re-delivery
after a lost ack is idempotent rather than duplicated.

If the turn ends (or is cancelled) with entries still in the inbox, the row is
deleted with the lease and the SPA's un-acked queue entries flush the #916 way.
Fail-soft in every direction: the user's text is either injected exactly once or
sent as a normal turn — never both, never neither.

### D4 — Latency: read in the hook, heartbeat as backstop

The heartbeat polls every 10 s (`LEASE_HEARTBEAT_SECONDS`). Riding it alone
would mean a steer typed 1 s after a tool started could miss that tool boundary
and several after it.

Instead the hook does its own `GetItem` at each tool boundary (async callbacks
are supported), which is a single-digit-millisecond read against a hot key,
bounded by the number of tool boundaries in a turn — negligible next to the tool
call that just ran. The heartbeat additionally caches "an inbox exists" on the
session manager, so the hook's read can be skipped entirely for the overwhelming
majority of turns where nobody is steering:

- heartbeat sees `steerFor == owner` → set `session_manager.steering_pending = True`
- hook reads the inbox only when that flag is set, **or** when the SPA's steer
  POST is newer than the last heartbeat (a cheap always-read for the first N
  boundaries after the arming write is not observable — simplest correct version
  is: read when the flag is set, and have the arming endpoint's `409`/`204`
  answer tell the SPA whether it landed).

Start with the unconditional per-boundary read (simple, provably correct) and
only add the flag gate if the read shows up in latency telemetry.

### D5 — No tool boundary, no steering

A pure-text turn fires no `AfterToolsEvent`. #916's end-of-turn flush therefore
stays permanently as the fallback, and both behaviours coexist. This is a
product constraint, not a temporary one — every harness with mid-turn steering
has it.

Consequence for the UI: the composer cannot promise "this goes in mid-turn"
unconditionally. The placeholder introduced in #916 becomes conditional on
whether a tool is currently running:

- tool running → "Send a follow-up — it goes in at the next step"
- otherwise → "Send a follow-up — it goes out when this response finishes"

The SPA already knows which, from the live `tool_use` / `tool_result` events.

### D6 — Client contract: `steering_applied`

New SSE event, added to the table in `CLAUDE.md`:

| Event | Purpose |
|-------|---------|
| `steering_applied` | A queued follow-up was injected into the running turn at a tool boundary — payload `{type, sessionId, entryId, text}`. Emitted from the stream coordinator once the mutated tool-result message is committed to history (never on the cancel/interrupt path, where the message is discarded). The SPA drops the matching entry from the composer queue and renders it as a user message in the live thread. Gated by `MID_TURN_STEERING_ENABLED` |

Emitted after the `message` / `tool_result` events for that batch, so the thread
renders in the order the model will see. Added to the SPA parser's event switch
(`stream-parser-core.ts`) next to `compaction` and `session_title`.

### D7 — Feature flag

`mid_turn_steering_enabled()` in `apis/shared/feature_flags.py`, **default on
with a kill switch** (`MID_TURN_STEERING_ENABLED=false` to opt out), matching
`SKILLS_ENABLED` / `SCHEDULED_RUNS_ENABLED`. Watch the empty-string
workflow-variable case the house pattern already guards for.

While off: the hook is registered but returns immediately, the steer endpoint
returns `404`, and the SPA never POSTs — #916 behaviour exactly.

## Prompt-cache and cost analysis

Required by the cost-effectiveness tenet: *what does this add to the prompt, on
every turn, for the life of every session?*

**Nothing, on a turn with no steering.** No `toolConfig` entry, no system-prompt
text, no extra history. The hook is inert.

**On a turn with steering:** one text block appended to a user message that was
about to be written anyway. The three cachePoints are tools-tail, system-tail,
and the `strategy="auto"` message-level point at the last user message
(`core/model_config.py:365`). The injection lands *inside* the segment that
point covers, behind both static points — it is append-only against the cached
prefix, so the next model call still reads the stable ~28 k-token prefix from
cache. No prefix rewrite, no `partial_miss`.

**Net effect is a saving, not a cost.** The behaviour it replaces is Stop +
resend: a discarded partial generation, an abandoned tail that changes history,
and a re-established prefix. Measure it the way the tenet says to — compare
`cacheStatus` and `wastedUsd` on sessions that steer against sessions that
stop-and-resend, via `GET /admin/costs/sessions/{id}/calls`.

## Persistence, history repair, and multi-agent safety

- **Reload survival is free.** The steering text rides inside the persisted
  tool-result message, so it is in AgentCore Memory and comes back on restore
  with no separate hydration path (unlike pending interrupts).
- **History repair.** `_drop_abandoned_turn_tail` and `_repair_tool_pairing` now
  see a user message with mixed `toolResult` + `text` content. Neither should
  care — both key on tool pairing — but this needs explicit coverage in
  `agents/main_agent/session/tests/test_history_repair.py`, because a repair
  helper that strips the message strips the user's words with it.
- **Compaction.** The injected block is deterministic and append-only, so the
  byte-stability contract holds. Confirm the truncation anchor still lands on a
  message boundary when the anchored message is a mixed one.
- **One session, more than one agent.** Per the CLAUDE.md rule, an `@`-mention
  turn runs a second `Agent` with its own session manager. The inbox must not be
  cached on an agent instance: it is read per tool boundary and cleared by
  conditional write, both owner-scoped to the lease. Two agents cannot both
  consume an entry, because the lease has exactly one owner.
- **Cancel beats steering.** If `cancelRequestedFor` and `steerFor` are both set
  for our owner, the cancel wins and the inbox is left alone — the SPA's queue
  entry survives the stop and the user can resend it.
- **Paused turns.** A steer that arrives while the turn is paused for OAuth
  consent or tool approval has no running loop to receive it. The resume request
  goes through `/invocations`, so the resume path can simply carry the pending
  entries in its payload and prepend them to the resumed turn. Phase 3.

## Frontend changes

#916 already owns the queue (`chat-input.component.ts`, `queuedMessages`). The
changes are additive:

1. On queueing while a tool is running, POST to `/sessions/{id}/steer` and mark
   the entry **pending-ack** (still visible, still removable — removal also
   DELETEs the inbox entry).
2. On `steering_applied` with a matching `entryId`, drop the entry from the
   queue and let the message-list render it as a user message inside the turn.
3. On the turn's falling edge, any entry that is still un-acked flushes exactly
   the way it does today. This is the whole fallback: the edge-triggered effect
   from #916 is unchanged, it just sees a shorter list.
4. A `409` from the steer endpoint (turn already ended) leaves the entry queued
   for that same falling edge — no toast, no error state.
5. Conditional placeholder per D5.

Turn grouping (`message-list.component.ts:360`) starts a new group on every
user-role message. Confirm how a mid-turn user bubble renders inside a turn that
is still streaming — this is the one piece of visual design work in the change.

## PR breakdown

| PR | Scope | Status |
|----|-------|--------|
| 1 | `session_lease` inbox helpers (`request_session_steer`, `peek_steer_queue`, `clear_steer_entry`, `remove_steer_entry`) + unit tests. No callers. | **built** |
| 2 | `SteeringHook` + registration + commit-on-append clearing + the SDK contract test. Behind the flag, no client path yet. | **built** |
| 3 | app-api `POST /sessions/{id}/steer` + `DELETE .../steer/{entryId}`, `get_current_user_from_session` auth per the house rule. | **built** |
| 4 | `steering_applied` SSE event: coordinator emit, parser case, CLAUDE.md table row. | **built** |
| 5 | SPA: pending-ack queue state, POST/DELETE wiring, conditional placeholder, mid-turn user bubble rendering. | **built** |
| 6 | Paused-turn carry-through on the resume path (D "Paused turns"). Optional, ships after the rest is live in dev. | open |

Three implementation notes worth carrying forward:

* **The ack drain runs before each SSE event is yielded, not after.** An
  injection confirmed on the turn's *final* tool batch would otherwise be
  stranded behind `done`, which the SPA's stream-state gate drops.
* **`_repair_tool_pairing` needed a fix, not just coverage.** It rebuilds
  result turns from the toolUseId map rather than copying them, so it dropped
  every non-toolResult block — the injection included. It now carries the
  residual across exactly once. The concern the spec raised as "confirm this is
  fine" was real.
* **"Reload survival is free" was half true.** The text does come back with no
  separate hydration path, but it comes back *raw*: a user message of
  `[toolResult…, text]` whose text is still wrapped in the tags written for the
  model, and which turn grouping would treat as the start of a new turn. The
  SPA normalizes both on load (`normalizeSteeringMessages`) so a reloaded
  steered turn reads exactly as it did live.

## Testing

- **Backend unit.** Hook injects into `event.message["content"]`; does *not*
  clear the inbox on read; clears on `MessageAddedEvent`; no-ops on an empty
  tool-result batch; no-op when the flag is off.
- **Backend integration (the one that matters).** A turn that interrupts on the
  same tool batch as a steer must leave the entry in the inbox. Build it on the
  local MCP stub harness that produces a genuinely paused turn — a mocked
  interrupt proves nothing here.
- **Lease.** Steer against an ended turn fails the owner condition; steer from a
  different user is a no-op; cancel + steer armed together resolves to cancel.
- **Prompt cache.** Assert the cachePoint positions are unchanged with a mixed
  message present (extend `test_bedrock_cache_points.py`).
- **History repair.** Mixed `toolResult` + `text` message survives
  `_repair_tool_pairing` and `_drop_abandoned_turn_tail` intact.
- **SPA.** Ack drops exactly the matching entry; no ack → falling-edge flush
  still fires once (the #916 edge-trigger test must keep passing); `409` leaves
  the entry queued; removal DELETEs.
- **Manual, in dev.** A long tool-using turn, steered once at boundary 1 and
  once at boundary 3, then reloaded — both injections present in restored
  history, in order.

## Risks and open questions

1. **In-place mutation of a write-guarded event.** The main technical risk.
   Mitigated by the exact pin, the contract test, and the interrupt/resume
   escape hatch. Re-check on every `strands-agents` bump.
2. **Model compliance.** A steering block appended after tool results is a
   less-conventional shape than a fresh user turn; the model may under-weight it.
   Needs a real evaluation in dev before the flag defaults on in prod — this is
   the item most likely to change the design.
3. **Two behaviours, one affordance.** Users will not reliably predict whether a
   follow-up lands mid-turn or at the end. D5's conditional placeholder is the
   mitigation; whether it is enough is a product question.
4. **Steering into a tool batch that is about to fail.** The injection lands
   next to error tool results. Probably fine — it is exactly when a user is most
   likely to steer — but worth watching in the dev evaluation.
5. **Quota and cost attribution.** A steered turn is longer than an unsteered
   one and its cost lands on a single `C#` row set. No change needed, but the
   session-notice thresholds will fire slightly differently.

## Out of scope

- Steering mid-generation (between tokens, with no tool boundary). Requires
  aborting and re-issuing the model call; strictly more expensive than waiting
  for the turn to end.
- Editing or retracting a message the agent has already read.
- Steering another user's session, or steering from a second device. The lease
  is user-scoped; cross-device steering would need its own design.
- Queueing steers across turns (an entry that misses its turn is sent as a
  normal turn, per D3).
