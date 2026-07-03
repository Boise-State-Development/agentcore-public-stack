# Interrupted-Turn Context

**Status:** Backend implemented (this branch); frontend follow-ups scoped below
**Date:** 2026-07-03
**Related:** max_tokens truncated-turn flow (`set_truncated_turn`), paused-turn / pending-interrupt flow (`set_paused_turn`), interrupt-resume streaming.

## 1. Problem

When an assistant response is interrupted before it completes — the user clicks **Stop**, the browser is refreshed, or the connection drops — the turn dies silently:

- The **user message was already persisted** at turn start (Strands' MessageAddedEvent hook fires when the user turn is added, before the model streams).
- The **assistant turn is only persisted on natural completion** — `TurnBasedSessionManager.append_message` (`session/turn_based_session_manager.py:123`) persists each message *as it completes*; nothing persists a message still streaming.
- A raw teardown surfaces inside the coordinator generator as `asyncio.CancelledError` or `GeneratorExit` — both `BaseException` subclasses that slip past every `except Exception` in the stream path.

**Result:** persisted history ends on a dangling user turn. On reload the user sees their message and nothing else. On the next turn the *model* sees malformed user→user history, has no idea it was cut off, and — critically — cannot tell a deliberate Stop (strong feedback) from a dropped connection (no signal).

## 2. Goals

- Record that a turn was interrupted, durably, surviving refresh.
- Preserve the **in-flight partial assistant text** the user already saw.
- Give the **model** legible, reason-aware context on its next turn.
- Distinguish **`user_stopped`** (deliberate; don't barrel onward) from **`connection_lost`** (technical; user probably still wants the answer). The transport can't make this distinction — both look like a dead stream — so intent is captured out-of-band (§5).

**Non-goals:** no bidirectional SSE channel; no changes to the max_tokens or paused-turn flows; no permanent synthetic "[interrupted]" system turn re-fed into context forever.

## 3. Design decisions (the three contested calls)

### 3.1 Third parallel marker — deliberately NOT unified

We keep `lastTurnInterrupted*` as a third sibling of `lastTurnContinuable` (truncated) and `paused_turn`/`pending_interrupts`, rather than folding all three into a `lastTurnDisposition`. Rationale:

- The three are **different shapes**, not three copies of one shape: paused carries a full resume snapshot + pending-interrupt list; truncated is a bare bool; interrupted is bool + reason + timestamp. A unified record still needs per-state variant payloads and per-state precedence — unification renames the problem.
- `lastTurnContinuable` and `paused_turn` are **live cross-package contracts** consumed by shipped SPA code; renaming them forces a coordinated frontend migration (CLAUDE.md contract rule) for zero user-visible value and real regression risk in two working flows.
- The lifecycles genuinely coincide (all cleared at next non-resume turn start), and that is already expressed by three adjacent calls in the invocations route — cheap to read, cheap to keep consistent.

Revisit only if a fourth marker appears; then extract the shared "turn-end disposition" machinery once, with evidence.

### 3.2 The partial IS persisted to Memory as a synthetic assistant message

The alternative considered — display-only metadata (a `displayText`-style side channel) + purely described interruption — was rejected:

- **Text-only is not a "lossy corner-cut"; it's the only valid choice.** A dangling `toolUse` block or an unsigned reasoning block *cannot* be replayed to Bedrock; any persistable partial is necessarily the text. The "reconstruction diverges from what the user saw" objection reduces to blocks that could never be persisted anyway.
- It **repairs role alternation in the model's actual history** — the whole point of the model-facing half. A display-only partial leaves user→user in Memory and outsources the problem to SDK history-repair.
- It's the **established mechanism**: error paths already persist synthetic assistant turns via `persist_synthetic_messages` (`session/persistence.py:37`; call sites in `stream_coordinator.py`). Reload hydration then works through the normal Memory read path with zero new storage or merge logic.
- It's how the sibling flow works: max_tokens partials live verbatim and unannotated in history today, and age out via compaction. Interruption partials behave identically; the *reason* context is delivered separately (§6) because it isn't knowable at persist time.

**Correctness constraint (fixed in this branch):** because `append_message` persists each completed message immediately (flush is a no-op — `turn_based_session_manager.py:949`), the coordinator accumulates **only the in-flight message's** text deltas: the accumulator resets at assistant `message_start` and clears at `message_stop`. Accumulating across the whole stream would duplicate mid-turn messages already committed to Memory.

**Empty partial:** a placeholder assistant turn (`"[Response interrupted before any content was generated]"`) is persisted **only when the history tail is a user message** — that's the dangling-turn case the placeholder exists to repair. On continuation/resume teardowns (tail = assistant) nothing needs repair, so only the marker is written.

### 3.3 Intent capture is primary; server-side detection is the backstop

The original PR-1 framing ("server-side CancelledError is load-bearing, beacon comes later") is inverted:

- **Cloud deliverability of the cancellation is unverified.** The chain is Browser → CloudFront → app-api BFF proxy (`app_api/chat/proxy_routes.py:148`, whose `stream_relay` `finally` closes the upstream httpx stream) → **AgentCore Runtime data plane** → inference-api container. Whether the data plane propagates a caller disconnect into the container's request task is undocumented. Local dev (`localhost:8001`) proves nothing about it. If it doesn't propagate, the container finishes the turn and persists the full response — losing nothing — but the server-side arm never fires.
- The server-side arm also only ever knows the **low-value reason** (`connection_lost`). The high-value signal — *the user chose to stop* — can only come from the client.

So: the client stop signal is the authoritative carrier of `user_stopped`; the coordinator's teardown arm is a best-effort backstop that contributes the partial text and the `connection_lost` fallback. Both are shipped in this backend change; neither waits on the other.

**`navigator.sendBeacon` is ruled out** — a correction to the earlier draft. App-api's `CSRFMiddleware` (`apis/shared/middleware/csrf.py:56`) rejects unsafe-method cookie requests without a matching `X-CSRF-Token` header, and `sendBeacon` cannot set headers. The SPA must use `fetch(url, { method: 'POST', keepalive: true, headers: { 'X-CSRF-Token': … } })` — keepalive survives page teardown and carries headers. Do **not** exempt the path from CSRF to accommodate sendBeacon.

## 4. Reason taxonomy

| Reason | Source | Signal | Next-turn note (§6) |
|---|---|---|---|
| `user_stopped` | `POST /sessions/{id}/interrupt` (client-attested only) | Strong — deliberate | "user stopped you; don't resume on your own" |
| `connection_lost` | Coordinator teardown arm (server-inferred only) | Weak — technical | "cut off by connection, not the user; continue if asked" |
| `unknown` | Unclassifiable writes | Weak | treated as `connection_lost` |

The request model (`SessionInterruptRequest`, `apis/shared/sessions/models.py`) accepts **only** `user_stopped` from the client — `connection_lost` from a client would let it downgrade a deliberate stop.

## 5. As built — backend (this branch)

### Marker (`apis/shared/sessions/metadata.py`)

- `set_interrupted_turn(session_id, user_id, reason, source)` (line 2207) — writes `lastTurnInterrupted` / `lastTurnInterruptReason` / `lastTurnInterruptedAt` on the session row. **Precedence, not ordering,** resolves the two racing writers: `user_stopped` writes unconditionally; `connection_lost`/`unknown` writes carry a `ConditionExpression` (`#ltr <> "user_stopped"`) so the fallback can never downgrade the settled reason regardless of arrival order.
- `clear_interrupted_turn(session_id, user_id) -> Optional[str]` (line 2294) — an atomic **pop**: `REMOVE` with `ReturnValues=UPDATED_OLD` returns the reason it cleared, so a single read+write both enforces the lifecycle *and* feeds the model note. Called at the start of every non-resume turn (beside `clear_truncated_turn` / `clear_paused_turn`, `inference_api/chat/routes.py:1043–1067`).

`SessionMetadata` / `SessionMetadataResponse` (`apis/shared/sessions/models.py`) carry the three aliased optional fields, so the SPA's existing metadata reads surface them with no new endpoint.

### Stop-signal endpoint (`apis/app_api/sessions/routes.py:603`)

`POST /sessions/{session_id}/interrupt`, body `{"reason": "user_stopped"}` → 204. Uses `Depends(get_current_user_from_session)` (app-api auth rule). Lives on **app-api**, not inference-api: the Runtime data plane only proxies `/invocations` + `/ping` (inference-api boundary rule). Ownership enforcement falls out of the user-scoped GSI lookup inside `set_interrupted_turn` — another user's session is a silent no-op.

### Cancellation backstop (`agents/main_agent/streaming/stream_coordinator.py:922`)

`except (asyncio.CancelledError, GeneratorExit)` around the stream loop — teardown surfaces as either, depending on whether the generator was at an inner `await` (cancellation) or a `yield` (`aclose()`). The arm calls `_persist_interruption` (line 1011) and **re-raises**; the MCP-broker `finally` still runs.

`_persist_interruption`:
- persists the in-flight partial (or the gated placeholder, §3.2) assistant-only via `persist_synthetic_messages` — the user turn is already committed (canonical invariant in `session/persistence.py`);
- writes the `connection_lost` fallback marker;
- wraps both in `asyncio.shield(asyncio.ensure_future(...))` so the writes complete even as the request task unwinds mid-`await` — a bare await inside a cancellation handler would itself be cancelled;
- is best-effort throughout: failures log, never mask the original teardown exception.

### Model-facing note (`apis/inference_api/chat/routes.py:499`, `:1676`)

On the next non-resume, non-continuation turn, the popped reason drives `_build_interruption_note(reason)`, prepended to `final_message` at the same seam as the MCP Apps `pending_ctx_block`. Properties:

- **Why next-turn, not persist-time:** the reason isn't knowable when the partial is persisted — the client signal races the backstop and precedence settles in DynamoDB. By the next turn the marker is authoritative. (This is also why annotating the persisted partial inline was rejected.)
- The prepend makes `message_will_be_modified` true, so the raw user text is stored as `displayText` — the note is **invisible to the user** but remains an honest part of the persisted user message in Memory, aging out via compaction like everything else. Cache-prefix-safe (appends to the newest message only).
- Continuation turns skip it structurally (Strands ignores the message; the model simply continues the persisted partial — which is exactly the right behavior for a `connection_lost` "Continue").

## 6. Known residual races (accepted)

- **Stop lands but the container finishes anyway** (data plane didn't propagate): full response persists, marker says `user_stopped`. The note still conveys true intent ("the user clicked stop"); the reload chip may sit next to a complete response. Rare, honest, self-clearing on next turn.
- **Signal arrives after the next turn already popped the marker:** the stale marker is cleared at the turn after. Harmless.
- **Hard network loss drops the keepalive fetch:** correctly degrades to `connection_lost` via the backstop (when it fires) or to nothing (when the turn actually completed server-side — in which case nothing was lost).

## 7. Follow-up PRs (frontend)

1. **Stop signal (small):** in `chat-http.service.ts` `cancelChatRequest` (line 257), fire the keepalive fetch with the CSRF header *before* `abortRequest`, best-effort/fire-and-forget. Optionally also from the SPA's unload teardown — but note that an unload signal can only honestly say `user_stopped` for an explicit Stop click; don't send it for refresh/close.
2. **Reload UX:** session hydration already merges Dynamo metadata; surface `lastTurnInterrupted` + reason:
   - `connection_lost` → "Response interrupted" chip on the partial + a Continue affordance reusing the truncated-turn continuation path (`continue_truncated` works unchanged against the persisted partial).
   - `user_stopped` → "You stopped this response" chip, **no** Continue.
   - Sanity-gate the chip on the last message actually looking interrupted (guards the completed-anyway race in §6).

## 8. Test coverage (this branch)

- `tests/shared/test_sessions_metadata.py` — set/pop lifecycle, pop returns reason, idempotent pop, `user_stopped` precedence in both arrival orders, missing-session no-op, response-model round trip.
- `tests/agents/main_agent/streaming/test_interrupted_turn_persistence.py` — CancelledError **and** GeneratorExit arms fire + re-raise; partial scoped to the in-flight message (completed mid-turn text excluded); assistant-only write; placeholder gated on user-tail; assistant-tail → marker-only.
- `tests/routes/test_sessions.py::TestSignalTurnInterrupted` — 204 + recorded `user_stopped`/`client_signal`; 422 for non-client-attested reasons; 401 unauthenticated.
- `tests/apis/inference_api/test_interruption_note.py` — reason-appropriate guidance, `unknown` treated as technical drop.
