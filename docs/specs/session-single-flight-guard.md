# Session single-flight concurrency guard

**Status:** implemented (branch `fix/session-single-flight-guard`, off `develop`)
**Follow-up to:** PR #653 (`fix/tool-pairing-restore-sanitizer`)

## Problem

A client-side abort (Stop button, tab switch, dropped socket, transport retry)
does **not** propagate through the AgentCore Runtime data plane to the backend
agent — the agent runs to completion server-side regardless. If a second
`POST /invocations` for the same session arrives while the first turn is still
running (genuine double-click, two tabs/devices, or an HTTP retry), the Runtime
can route the two invocations to **different containers**, so two agent loops
run concurrently against the same AgentCore Memory session. Both persist
`toolUse`/`toolResult` events, producing duplicate + interleaved
(assistant/assistant/user/user) history that Bedrock Converse rejects on every
subsequent turn:

> The number of toolResult blocks at messages.N exceeds the number of toolUse
> blocks of previous turn.

That was the prod incident that bricked session `f761f59b`. PR #653 closed the
frontend tab-switch vector (`openWhenHidden: true` so backgrounding a tab no
longer aborts + reopens the stream) and added a restore-time
`_repair_tool_pairing` sanitizer. This change closes the remaining **server-side
race**: two live invocations for one session.

## Decision summary

| Question | Decision |
|----------|----------|
| Reject-new vs supersede-old | **Reject-new** with HTTP **409** |
| Lock mechanism | **Distributed lease** — DynamoDB conditional write on `sessions-metadata` |
| Which service owns the lock | **inference-api `/invocations`** (the true turn-start chokepoint) |
| Lease validity window | **90 s**, renewed by heartbeat |
| Heartbeat | Background asyncio task, renews every **30 s** while the turn streams |
| TTL backstop | DynamoDB `ttl` attribute at **now + 1 h** (auto-reap orphans) |
| Resume / continuation carve-out | Bypass the conflict check; acquire the lease with `force=True` |
| Failure posture | **Fail-open** — any non-conflict DynamoDB error proceeds without a lease |

### Reject-new vs supersede-old

**Reject-new (chosen).** On an active lease, the duplicate gets `409 Conflict`;
the first turn finishes uninterrupted and the SPA surfaces "already streaming".

Supersede-old matches the SPA's "newest stream wins" UX, but superseding
requires *signalling the in-flight agent loop to stop* — which is exactly the
thing that does **not** propagate through the Runtime data plane (the whole
premise of the bug). The session manager's `cancelled` flag and `StopHook`
(`turn_based_session_manager.py`, `session/hooks/stop.py`) only reach the agent
that shares the *same in-process* session manager instance; a loop on another
container never sees the flag. So supersede cannot actually stop the old writer
and both loops would still corrupt history. Reject-new is the only option that
holds without a cross-container stop signal. It is also strictly safer: the
worst case is a spurious 409 (recoverable by retrying after the first turn),
never a corrupted session.

### Which service owns the lock

The lease lives in **inference-api `/invocations`**, not the app-api
`/chat/stream` BFF proxy:

- `/invocations` is the true turn-start chokepoint and the exact point where a
  second container also begins. The harm (AgentCore Memory tool-pairing
  corruption) is inference-api's domain — the agent loop it owns is the writer.
- An in-process lock is insufficient (two containers), so the guard must be
  distributed regardless of which service holds it.
- Per the CLAUDE.md inference-api boundary rule and the "respect service
  boundaries" principle, the guard belongs where the writes happen.
- The app-api proxy already relays a `>= 400` upstream status verbatim
  (`proxy_routes.py`), so a 409 from inference-api reaches the SPA unchanged —
  **no app-api change is required.** Putting a second lease in app-api would
  duplicate state and couple the BFF to turn semantics it deliberately doesn't
  own (the body is opaque bytes there).

The lease **storage helper** lives in `apis/shared/sessions/` (like
`metadata.py`), since that package is the shared home for session-row access;
only inference-api *uses* it.

### Lease shape & storage

Dedicated item on the existing `boisestateai-v2-sessions-metadata` table — no
new table, no CDK change (the table already has `timeToLiveAttribute: 'ttl'` and
PAY_PER_REQUEST billing):

```
PK  = USER#{user_id}
SK  = LEASE#{session_id}
Attributes:
  leaseOwner      : uuid4 hex, unique per invocation
  leaseExpiresAt  : epoch seconds — the app-level validity check
  ttl             : epoch seconds (now + 3600) — DynamoDB auto-reap backstop
  updatedAt       : ISO8601
```

A **deterministic key** (`LEASE#{session_id}`, no dynamic `lastMessageAt`
suffix) means acquisition is a single atomic conditional write with **no GSI
read first** — eliminating the read-before-write race that a marker on the META
row would have. The item is invisible to session listings (`list_user_sessions`
queries `SK begins_with 'S#ACTIVE#'`) and to `SessionLookupIndex` (it has no
`GSI_PK`/`GSI_SK`).

Why both `leaseExpiresAt` *and* `ttl`: DynamoDB TTL deletion lags up to 48 h, so
it can't be the correctness mechanism. The **application** compares
`leaseExpiresAt < now` in the acquisition `ConditionExpression`; `ttl` only
stops crashed-container orphans from accumulating forever.

**Acquisition (fresh turn):**

```
UpdateItem
  ConditionExpression = attribute_not_exists(PK) OR leaseExpiresAt < :now
  SET leaseOwner, leaseExpiresAt, ttl, updatedAt
```

`ConditionalCheckFailedException` ⇒ an unexpired lease is held ⇒ raise
`SessionBusyError` ⇒ `/invocations` returns 409. Any other `ClientError`
(throttle, transient) ⇒ log and **proceed without a lease** (fail-open: never
block a legitimate turn on lock-infra failure).

**Renewal (heartbeat)** and **release** both carry
`ConditionExpression = leaseOwner = :owner` so a container that already lost the
lease (its window lapsed and another turn took over) can neither extend nor
delete the new owner's lease. Both are best-effort and swallow errors.

### Heartbeat & window sizing

- Window **90 s**, heartbeat every **30 s** ⇒ tolerates one missed renewal
  (network blip) before expiry.
- A background asyncio task (not inline on SSE-event cadence) renews on a wall
  clock, so it keeps the lease alive even across a **long silent tool call**
  (code-interpreter / browser can run > 90 s between yielded events).
- After a genuine container crash the heartbeat stops; the lease self-expires in
  ≤ 90 s and the session is usable again. This is the recovery time; it is well
  under the 600 s stream timeout, so a normal long turn never self-evicts.

### Resume / continuation carve-out

Resume (`interrupt_responses` present, `is_resume`) and max-tokens continuation
(`continue_truncated`, `is_continuation`) re-enter a turn whose original agent
loop has **already ended** (it paused/truncated and its SSE stream closed).
There is no concurrent writer to guard against, and blocking them would strand
the user. They call `acquire_session_lease(..., force=True)`:

- **`force=True`** does an *unconditional* write — it always succeeds, so an
  authorized resume can never be rejected (even against a stale lease the paused
  turn failed to release).
- It still *installs* a lease, so a fresh duplicate that arrives *during* the
  resume is itself rejected — the session stays single-flight end to end.

Two concurrent resumes for one session is not a real vector (resume is an
explicit post-OAuth user action), so `force` overwrite between them is
acceptable.

### Placement & release wiring

Acquire at the **top of the streaming `try:`** in `invocations` — after the
pre-flight checks (quota, model access, RAG validation, file resolution) and
immediately before agent construction. This keeps release management to three
contiguous sites with no early `return` in between:

1. **Happy path:** the SSE generator (`stream_with_quota_warning`) starts the
   heartbeat before `agent.stream_async`, and in its `finally` cancels the
   heartbeat and releases the lease.
2. **`except HTTPException`** (e.g. resume/interrupt 400s raised after acquire):
   release, then re-raise.
3. **`except Exception`** (agent build error → conversational error stream):
   release, then return the error stream.

FastAPI runs the generator *after* the handler returns, so no single `finally`
can cover both the streaming and non-streaming exits — hence the three sites.
They are mutually exclusive (reaching the generator means no exception reached
the outer handlers), and release is idempotent (owner-conditional delete), so
there is no double-release hazard.

Preview sessions (`is_preview_session`) and the local/no-DynamoDB path (table
env var unset) skip the guard entirely — they don't persist and can't corrupt
shared Memory.

### Out of scope

- **App-initiated invocations** (`app_tool_call`, `app_context_update`) are
  short-circuited above the guard. They do write synthesized tool events, but
  they are inert behind the MCP-Apps host flag (no live App), synchronous, and
  fast. Guarding them is deferred.
- **Scheduled/headless runs** go through the same `/invocations` and get the
  guard for free; a schedule that fires twice for one session is exactly the
  kind of duplicate this rejects.

## Known limitations of the lease alone

The lease is a **safety floor** — it guarantees "never corrupt Memory," not
"Stop actually stops." Two rough edges remain, both stemming from the same root
cause (client aborts don't reach the running turn), and both are the motivation
for the distributed-cancel follow-on below:

- **Best-effort, not a hard lock.** Fail-open on a DynamoDB brownout, a
  heartbeat-failure window (~3 missed renewals ≈ the lease window), and reliance
  on NTP-level clock agreement between containers each leave a narrow window
  where a duplicate could still slip through. Acceptable for a net; named so it
  isn't mistaken for a hard guarantee.
- **Stop → immediate resend returns 409.** Because Stop doesn't end the server
  turn, the old turn keeps running and holds the lease (heartbeated, up to the
  600s stream timeout). The resend is a genuine second concurrent loop, so 409
  is *correct* — but it changes UX. This is strictly better than today (where
  the same action corrupts the session), but the SPA must handle 409 as "prior
  response still finishing," and the clean fix is to make Stop genuinely end the
  turn (below).

## Follow-on: distributed turn cancellation (make Stop real)

**Status:** proposed, not built. Separate, larger change — it touches the agent
step loop, not just the `/invocations` entry point. Complementary to the lease,
not a replacement.

### Why Stop can't propagate today

Two stacked reasons, both confirmed in code:

1. **The AgentCore Runtime data plane only proxies `/invocations` and `/ping`**
   (`apis/app_api/sessions/routes.py` — *"a custom inference-api route would 404
   in cloud"*; `apis/shared/harness/runner.py` builds
   `POST /runtimes/{ARN}/invocations?qualifier=DEFAULT`). There is no
   "abort this invocation" operation, so closing the downstream HTTP connection
   never signals the container to cancel its running coroutine —
   `agent.stream_async` runs to completion, still writing to Memory.
2. **You can't address the container running the turn.** The Runtime routes
   invocations to containers opaquely (the same reason a duplicate lands on a
   *different* container). A "stop session X" request would likely hit the wrong
   container.

Consequently the existing `cancelled` flag + `StopHook`
(`session/turn_based_session_manager.py`, `session/hooks/stop.py`) are **dead
code**: nothing in `src/` ever sets `cancelled = True`. Today's Stop is a client
abort plus a `user_stopped` beacon that only writes an interrupted-turn *marker*
via app-api (`set_interrupted_turn`) for the "Continue" UX — it never reaches
the running turn.

### Design: poll a distributed cancel flag from inside the loop

Reuse the exact distributed-state pattern the lease already relies on. The stop
signal and the running turn coordinate through the session row, not through the
Runtime:

1. **Signal (any container).** The `POST /sessions/{id}/interrupt`
   `user_stopped` path *additionally* sets a `cancelRequested` marker on the
   session's lease/META row — e.g. `cancelRequestedFor = <leaseOwner>` (scope it
   to the current lease owner so it can't cancel a later, unrelated turn), plus
   a timestamp. Cheap conditional write; app-api already owns this endpoint and
   already talks to this table.
2. **Observe (the running container).** Wire `StopHook` (and, ideally, a check
   between agent steps / before each model call) to consult that flag instead of
   the in-process boolean. The natural, low-latency read is to **piggyback on
   the lease heartbeat** (`_lease_heartbeat_loop`, every 30s): when a renew sees
   `cancelRequestedFor == our owner`, set `session_manager.cancelled = True`.
   `StopHook.check_cancelled` (already registered on `BeforeToolCallEvent`) then
   cancels the next tool call, and the loop unwinds.
3. **Release.** The turn ends → the generator `finally` releases the lease as it
   does now → the user's resend acquires cleanly. No 409-on-resend.

### Trade-offs & open questions

- **Granularity.** Heartbeat-cadence polling (~30s) bounds worst-case
  stop-to-effect latency. A tighter loop-level check (between steps / before each
  Bedrock call) makes Stop feel instant but adds a DynamoDB read per step — likely
  gate it behind "only read when a cheap in-process hint is unset," or accept the
  heartbeat cadence for v1.
- **Mid-tool cancellation.** `StopHook` cancels at `BeforeToolCallEvent`
  boundaries; a long-running tool already in flight (browser, code interpreter)
  finishes before the cancel is seen. Genuinely interrupting an in-flight tool is
  a deeper change (cooperative cancellation inside the tool executor) and
  probably out of scope even for the follow-on.
- **Partial-write integrity.** When a turn is cancelled mid-stream, its partial
  `toolUse`/`toolResult` must remain a valid pairing in Memory — this is exactly
  what PR #653's `_repair_tool_pairing` restore-time sanitizer already guards, so
  cancellation rides on machinery that already exists.
- **Cost upside.** Real cancellation stops burning Bedrock tokens and tool
  invocations on output nobody will read — a side benefit beyond UX.

### Relationship to the lease

The lease prevents *corruption* (never two live writers); distributed
cancellation makes Stop *actually stop* (one writer, ended on demand). Ship the
lease first as the safety floor; the cancel follow-on removes the 409-on-resend
rough edge and reclaims wasted compute. Neither supersedes the other.
