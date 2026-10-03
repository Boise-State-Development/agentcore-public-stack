# Conversation rewind and fork

**Status:** DRAFT spec, written 2026-10-03 against `develop` @ `ac4212a7`. Nothing built yet.
**Tracking issue:** #1421
**Flags (in development, default OFF):** `SESSION_REWIND_ENABLED` / `CDK_SESSION_REWIND_ENABLED` /
`features.sessionRewind`, and `SESSION_FORK_ENABLED` / `CDK_SESSION_FORK_ENABLED` / `features.sessionFork`.
Two flags because the two halves ship separately (§8).

## 1. What the user gets

Two actions on every **user** message bubble, next to Copy, matching Claude Code's desktop app:

- **Rewind to here.** "Removes this message and everything after it." The conversation is cut just
  before the message, and the message's text goes back into the composer so it can be edited and
  resent. The session keeps its id, URL, title and sidebar position.
- **Fork from here.** "Starts a new session from this point. This one stays as it is." A new session
  is created holding everything *before* the message. The SPA navigates to it with the message's
  text in the composer. The original is untouched.

Both cut at the same point: just before a user message. A fork is a rewind into a new session. That
symmetry is deliberate. It gives both actions one set of rules about history, and the fork's first
turn is cache-identical to the rewind's (§3).

Out of scope for v1: rewinding to a point the compaction checkpoint has already summarized (§5.3),
undoing a rewind after something new has been sent (§4.4), forking from an assistant message,
voice sessions, and steering bubbles (a mid-turn steer is a text block on a tool-result message,
not a cut point).

## 2. The question this spec answers first: do rewinds and forks reuse the prompt cache?

**Yes, within limits that are set by Bedrock's cache rules and by how history is restored.**

Bedrock's prompt cache is not session-scoped. It is an exact-prefix match within one account,
region and model. A rewind or fork reads from cache whenever it sends the same prefix bytes the
original conversation sent, and the cache entry for that prefix is still alive.

### 2.1 What is cached on each request

Three cache points (`agents/main_agent/core/model_config.py:457`), four with a bound Memory Space:

| Point | Covers | TTL |
|---|---|---|
| `toolConfig` tail | tool specs | 5 min, or 1 h with `AGENTCORE_PROMPT_CACHE_STATIC_PREFIX_TTL=1h` |
| system tail | platform floor + instructions + date (day-stable, `utils/timezone.py`) | same as above |
| last user message (`strategy="auto"`) | the conversation history | 5 min, always |

The message-level point only matches entries within Bedrock's ~20-content-block lookback of the
new breakpoint (see the fan-out note in `model_config.py`).

### 2.2 Which history entries exist, and when they expire

Each model call writes one history entry: the prefix ending at that call's last user-role message
(the user text or the latest tool-result message). The **first** call of turn *k+1* reads, via
lookback, the entry the **last** call of turn *k* wrote, which refreshes it. Nothing touches that
entry again, because later turns read longer prefixes.

So: **a cut before user message *k* reads its history from cache if turn *k* — the turn being
replaced — started less than 5 minutes ago.** The new request is
`history[0 .. k-1] + new message`. The newest live entry ends at turn *k-1*'s last user-role
message, and the only blocks not covered are turn *k-1*'s final assistant reply plus the new
message. That is a few blocks, well inside the lookback.

| Cut | History | Tools + system |
|---|---|---|
| Before the latest user message ("edit my last message"), within 5 min | **Hit.** Writes only the last reply + the new message. | Hit |
| Further back, turn being replaced started < 5 min ago | Hit | Hit |
| Further back, older than that | Re-written at 1.25× | Hit if inside its TTL |
| At or before the compaction checkpoint | Different prefix (raw turns vs. summary), always re-written | Hit if inside its TTL |

Illustration at the default Haiku 4.5 Regional rates in CLAUDE.md, for 60k tokens of history: a
read costs about $0.007, a re-write about $0.083. The worst case is the same cost as resuming an
idle conversation today, so rewind and fork add no new class of spend. They make the cheap case
easy to reach.

### 2.3 Warm vs. cold restore: rewind is cache-safe, fork is best-effort

This is the part that decides the design.

The cache entries were written from the **live, in-memory** message list. A rewind on a warm
container can cut that same list in place, so the bytes match exactly. A fork, or any restore on a
cold container, rebuilds history **from stored events**, and that rebuild is not byte-identical to
the live list today:

1. **Long-term-memory context is sent but not stored.** `retrieve_customer_context` inserts a
   `<user_context>` block at the head of the user message
   (`turn_based_session_manager.py:488`). That runs after the message was persisted, and
   `tests/agents/main_agent/session/test_memory_retrieval_prefetch.py:118` asserts the block is
   *not* in the stored event. Every cached entry built from a user message that received LTM context
   therefore contains bytes no cold restore can reproduce.
2. **Restore runs sanitizers and pairing repair; live accumulation does not.** The docstring of
   `_adopt_session_conversation` (`apis/inference_api/chat/service.py:208`) records this as the
   reason re-restoring is rejected there: "the same conversation can serialize differently
   depending on the path that produced it."

Consequences:

- **Rewind on a warm agent:** history hit, per §2.2.
- **Rewind on a cold container:** history miss if (1) or (2) applies. Any cold restore misses the
  same way today, so this is not a regression.
- **Fork:** always a cold rebuild. Tools and system prompt hit. **History hits only when no kept
  user message received LTM context and the sanitizers changed nothing.** With LTM retrieval on in
  prod, expect a history re-write on most forks of conversations past their first few turns.

Making fork history byte-stable means fixing (1) for every cold restore, not only forks. One option
is to persist the injected LTM text as a sidecar keyed by message index and re-insert it at restore.
That is a change to the turn path with a TTFT cost and is a decision in its own right (§9, Q1). v1
ships forks without it and **measures** the hit rate (§6) before anyone pays to improve it.

## 3. Invariants

1. **A retained message's bytes never change.** Cutting removes a suffix. Nothing before the cut
   point is re-serialized, re-truncated or re-ordered.
2. **Message indices are never reused.** Message ids are positional (`msg-{sessionId}-{index}`,
   `apis/shared/sessions/messages.py:212`), and cost rows, feedback, `displayText`, tool summaries,
   UI resources and artifact provenance (`producedByMessageIndex`) all key on that index. A rewind
   that let a new message take a rewound message's index would attach the old message's feedback
   and metadata to the new one.
3. **Spend is never un-spent.** Cost rows of rewound turns stay, and keep counting toward quota
   and the session's lifetime `totalCost`. A fork does not inherit the parent's cost; its own calls
   are its own.
4. **Nothing is added to the turn path.** Rewind state rides the session row the restore already
   reads for compaction state; filtering is in-memory. Fork's copying runs in app-api, off the turn
   path. (TTFT budget, CLAUDE.md.)
5. **Per-session state is re-read per turn, never cached on an agent instance.** A rewind made
   while an agent is cached must reach that agent on the next turn (CLAUDE.md, #741, #751).

## 4. Rewind

### 4.1 Storage: tombstoned spans, not deletion

AgentCore Memory events are append-only per session. A rewind does **not** delete events. It adds a
span to a new `rewind` attribute on the session's metadata row (static SK, the same item that holds
`compactionState`):

```
rewind: {
  epoch: 3,                      # bumped on every rewind
  spans: [[6, 10], [12, 14]],    # half-open [start, end) in PHYSICAL event order
  updatedAt: "…"
}
```

**Physical** index = position in `list_messages` order, the same order `GET /messages` and the
per-turn message-index base (`_start_history_count`, `stream_coordinator.py:3248`) already use.
**Logical** index = position after removing spanned events, which is what the agent sees.

A new span always runs from the cut point to the current physical end. That keeps every logical
index *below the cut* unchanged across any number of rewinds, which is why compaction state, kept in
logical indices, stays valid (§5).

Why not delete: deletion makes indices reusable (breaks invariant 2), gives up undo, and still has to
clean feedback/metadata rows by index. Why not fork-and-replace: the session id changes, which
breaks links, project task crumbs and anything else holding the id. It also pays for a full copy on
every edit, and inherits fork's cold-restore cache problem (§2.3) on the most common action.

### 4.2 The endpoint

`POST /sessions/{session_id}/rewind` in app-api (`get_current_user_from_session`), body
`{ messageId }`. The message must be a user text message (a valid cutoff,
`_find_valid_cutoff_indices`, `turn_based_session_manager.py:2037`) and visible.

1. Take the session's single-flight lease, or answer 409 while a turn is streaming.
2. Append the span, merge overlaps, bump `epoch`, and apply the compaction rules (§5) — one
   conditional `UpdateItem`.
3. Clear any `PendingInterrupt` breadcrumb and paused-turn snapshot whose turn falls inside the
   span. The paused turn no longer exists.
4. Return `{ nextMessageIndex, prefillText, rewoundMessageIds }`. `nextMessageIndex` is the physical
   event count; `prefillText` is the message's `displayText` (not raw content, which may carry the
   `[Attached files: …]` marker).

### 4.3 Restore and the warm agent

- **Cold restore:** right after `super().initialize()` loads the events, before compaction is
  applied, drop spanned events. The cut is always just before a user message, so the retained tail
  ends on a complete assistant message and pairing repair has nothing new to fix. *Verify:* the
  SDK's repair runs inside `super().initialize()` on the unfiltered list. It must not insert or
  remove messages in a way that shifts physical positions. If it does, filter at the load call
  instead.
- **Warm agent (the important path):** the per-turn re-read that adopts compaction state
  (`_adopt_persisted_compaction_state`, `:1029`) also reads `rewind.epoch`. If it moved, cut the
  live list **in place** (`messages[:] = messages[:k - live_offset]`). Slice assignment keeps the
  `_adopt_session_conversation` alias, so every cached agent for the session sees the cut, and it
  re-serializes nothing — invariant 1, and the reason a warm rewind is a cache hit. That function's
  length guard ("a live instance trails what was restored") must treat a shorter list after an
  epoch change as expected.
- *Verify:* whether Strands' own `message_id` counter (used on append) is read back for ordering
  or de-duplication. With spans, newly appended events get message ids that collide with spanned
  ones. That is harmless if the id is write-only.

### 4.4 Undo

Removing a span is safe only while nothing has been appended after it. Logical indices of later
messages would otherwise shift under the compaction state. v1 offers an **Undo** toast until the
next send; the endpoint refuses the undo (409) once the physical count has grown past the span.

## 5. Compaction state under a cut

Compaction indices (`checkpoint`, `truncation_anchor`, `pending_checkpoint`) are absolute indices
into the loaded list, which after §4.3 is the logical list. For a cut at logical index *k*:

1. **`truncation_anchor = min(anchor, k)`.** Required, not cosmetic: truncation applies strictly
   below the anchor (`_apply_compaction`, `:820`). An anchor left above *k* would truncate the
   *new* messages that take indices *k…* on a later restore, which breaks invariant 1. Lowering it to
   *k* changes nothing below *k*: those messages were already below the old anchor.
2. **Clear `pending_*`.** A parked cut computed over the rewound history is stale. Clearing costs
   nothing; the policy recomputes it after the next turn.
3. **Cut at or before `checkpoint` — refused in v1.** The summary covers messages up to the
   checkpoint, and the prior checkpoint is not stored, so there is nothing correct to roll back to.
   Resetting to raw history could exceed the context window before the post-turn policy runs. The
   SPA hides Rewind and Fork on summarized messages (the compaction badge already knows where the
   checkpoint is) and the endpoint answers 409. Phase 2 would keep a bounded checkpoint history,
   bearing in mind the 400 KB item limit.
4. `updatedAt`, `armed`, `policy`, `lastPrefixKey` are untouched.

## 6. Observability: make the cache claim provable

Without a marker, a rewound turn will be **misclassified as waste**. `classify_cache_status`
(`apis/shared/observability/prompt_cache.py:178`) compares against the session's previous call. After
a rewind past the history TTL, the call reads tools + system and re-writes history, so
write > 3 × read (`PARTIAL_MISS_WRITE_READ_RATIO`) inside the TTL of the *previous* call, which is
classified as `partial_miss` and priced into `wastedUsd`. That re-write is unavoidable.

- Stamp `rewound: true` (and the `epoch`) on the first `C#` row after a rewind, and `forkedFrom:
  <parentSessionId>` on a fork's first row. Treat them like `agentSwitched`: counted, and exposed
  as `rewindMissCount` / `rewindUsd` (and `fork…`), but subtracted from unexplained waste.
- These two fields are also how the §2.3 claims get checked on dev and prod: hit rate on the first
  post-rewind call split by warm/cold, and on forks split by whether LTM context was present.

## 7. Fork

### 7.1 Storage: copy, don't point

`POST /sessions/{session_id}/fork`, body `{ messageId }`. It creates a new session and **copies**
the visible events before the cut, rather than storing a pointer to the parent.

- A pointer would add a second `list_events` to every restore of the child (TTFT), make
  `GET /messages` and the message-index base two-session aware, and break when the parent is
  deleted: session delete hard-deletes its events (`session_service.py:397`).
- Copying costs one `CreateEvent` per event, once, in app-api. Run them concurrently with each
  event's **original `eventTimestamp`**, so list order and `count_created_before` are unchanged.
  The child's physical indices are then contiguous `0..k-1`: no spans, and logical equals physical.

What gets copied:

| Item | Copy? | Note |
|---|---|---|
| Message events before the cut | Yes | Payload bytes verbatim |
| Strands SESSION / AGENT state events | **Yes** | Without them the SDK takes its new-session branch and loads no history (`initialize` docstring, `:664`). Rewrite the embedded session id |
| `compactionState` | Yes | Verbatim, then apply §5 at *k*. A fresh state would mean checkpoint 0 (full history instead of summary) and anchor 0 (untruncated tool output): a guaranteed history miss |
| Agent / project / Memory Space binding, model | Yes | A different config is a different prefix, and a different agent |
| `displayText`, tool summaries (`TSUM#`), UI resources for indices < *k* | Yes | Display parity on reload |
| Session files (S3 + metadata) | Yes, server-side `CopyObject` | `document_read` resolves files per session, and the parent's delete would remove shared objects. Counts toward the user's file quota |
| Cost rows, feedback, pending interrupts | No | Invariant 3; feedback belongs to the parent's messages; a paused turn cannot be forked |
| Title | `"<title> (fork)"` | No Nova call |

Plus two child-only fields: `forkedFrom: { sessionId, messageIndex }` (sidebar "Forked from …"
link, §6 marker), and `rootSessionId`. Document offload buckets on `session_id`
(`document_offload.py:115`). At the default 100% rollout that is a no-op, but under a partial rollout
a fork could change arms and age documents differently. Bucket on `rootSessionId` instead.

### 7.2 Long-term memory side effect (open)

Copied events are new events to AgentCore Memory, so the semantic, preference and summary
strategies will extract from them again. Summary records are session-scoped, so the child getting
its own is correct. Semantic and preference records are actor-scoped and consolidated, so a
re-extraction probably merges into existing records, but that is unverified. **Measure on dev before
the fork flag turns on** (§9, Q2): count the actor's records before and after forking a long
conversation, and price the extraction against the AgentCore pricing page.

## 8. Delivery

| PR | Scope | Flag |
|---|---|---|
| 1 | Backend rewind: `rewind` attribute, endpoint, cold filter, warm in-place cut, §5 rules, `GET /messages` filtering + `nextMessageIndex`, §6 markers | `SESSION_REWIND_ENABLED` |
| 2 | SPA rewind: hover action on user bubbles, composer prefill (route-keyed draft), Undo toast, hide on summarized messages. **Message ids:** the SPA derives the next id from `currentMessages.length` (`message-map.service.ts:213`, `stream-parser.service.ts:825`); switch both to the server's `nextMessageIndex` | `features.sessionRewind` |
| 3 | Backend fork: copy (§7.1), `forkedFrom` / `rootSessionId`, offload bucketing | `SESSION_FORK_ENABLED` |
| 4 | SPA fork: action, "Forking…" state, navigate, sidebar provenance | `features.sessionFork` |

Rewind ships first: it is the more common action and the one with the strong cache story.

### 8.1 Tests that carry the invariants

- **Byte equality (the key test), rewind:** run a conversation through the live path, capturing the
  exact `messages` sent on turn *k*'s first call. Rewind to *k*, restore warm and cold, and assert
  `fingerprint_canonical_json(history[:k])` equals the captured prefix minus message *k*. The cold
  case runs with LTM retrieval returning nothing; a second cold case with LTM context asserts the
  known mismatch, so a later fix (§9 Q1) flips it deliberately.
- **Byte equality, fork:** the same, against the child's cold restore.
- **Anchor clamp:** anchor above *k*, rewind, append two turns, restore twice; the new turns are
  never truncated and the two restores are identical.
- **Index uniqueness:** rewind, send, and assert the new message's id, cost row and feedback key
  differ from every rewound message's.
- **Alias:** two cached agents for one session (`@`-mention), rewind, next turn on either sees the cut
  (extends `test_second_cache_key_for_a_session_shares_the_conversation`).
- **Smoke matrix (`scripts/smoke_turns.py`):** rows for rewind-last within TTL (expect `hit`),
  rewind-older after TTL (expect `rewound` and no `wastedUsd`), and fork (expect `forkedFrom`; record,
  don't assert, the history outcome until Q1 is settled). Add rewind/fork to
  `docs/testing/smoke-regression.md`.

## 9. Open questions

1. **Persist LTM context so cold restores are byte-stable?** This would make forks and cold rewinds
   hit, and it also fixes ordinary cold restores inside the TTL. It needs a sidecar read on the
   restore path, which has a TTFT cost and must be measured. Decide after §6 shows how often forks
   actually miss for this reason.
2. **LTM re-extraction on fork copies** (§7.2): duplicate records? Cost per fork?
3. **CreateEvent throughput** for copying a long conversation (a few hundred events) inside an
   interactive request: AgentCore TPS limits and wall time. If it is too slow, the fork becomes
   async with a "Preparing fork…" state.
4. **Rewound artifacts and files.** v1 keeps them: they exist, and the user may want them. Revisit if
   users expect rewind to remove them.
5. **Shared Projects sessions.** v1 is owner-only, like every session route. Whether a member may fork
   a project thread they can see is a Projects decision (§9.6 of that spec governs degraded
   harnesses, and a fork inherits the binding).
