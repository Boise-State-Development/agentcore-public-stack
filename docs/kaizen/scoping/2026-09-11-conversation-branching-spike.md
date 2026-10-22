# Conversation branching spike — findings (six questions answered)

**Date:** 2026-09-11 · **Status:** spike complete (code read + local probe) · **Proposal:** `docs/kaizen/reviews/2026-09-11.md` ▸ Proposal #6 · **Queue entry:** `docs/kaizen/review-queue.md` ▸ `[2026-09-08] Strands Snapshots`
**Method:** read our session-persistence, cost and SPA code on `develop`; read the installed `strands` and `bedrock_agentcore` sources; ran a throwaway probe against a **real** `strands.Agent` (no model call, no network) to measure the two claims the proposal rests on — snapshot byte-stability and the alias break. Every line reference below was resolved against this checkout.
**Not built:** nothing. No product code, no dependency change. The probe lived in a scratch directory and is not in this PR.
**Not re-run:** the four-candidate subtraction audit recorded in the `[2026-09-08]` queue entry (`PausedTurnSnapshot`, compaction state, `PreviewSessionManager`, marketplace agent-version snapshots — zero replaceable). It is cited where relevant and was not re-investigated. Snapshot-as-store-of-record over AgentCore Memory stays rejected; §Q2 adds an independent reason it could not have been adopted anyway.

---

## Verdict at a glance

| # | Question | Finding |
|---|---|---|
| 1 | What does a fork actually copy? | **Full history copy, or nothing.** Pointer-only yields an empty conversation in *both* readers. Copy-on-read is not expressible — the model's history comes from a Strands `SessionManager` whose restore contract is one `(memory, actor, session)` triple. |
| 2 | Where does copied history come from? | **A snapshot, or it is not byte-stable.** AgentCore Memory is the only store of model-shaped messages, but every read passes through transforms the live list never sees. `GET /messages` is a lossy display projection and cannot be fed back. |
| 3 | Does Phase 1 reach the product goal? | **No.** As specified it ships a sidebar breadcrumb on an empty chat. Regenerate and edit-and-resend need *history truncation*, which no session-metadata field can express. The review over-credited the cheap route. |
| 4 | What breaks the alias? | **`load_snapshot` itself** — measured. `agent.py:1607` rebinds `self.messages` to a fresh `deepcopy`. The guarding test cannot see it: it exercises `get_agent` only, and a per-turn load sits downstream of `get_agent`. |
| 5 | Prompt-cache impact | Round-trip byte-stability **confirmed by measurement** — and it is stability *with respect to the snapshot*, not with respect to Memory. A design that mixes both sources flips `historyHash` on an arbitrary turn. |
| 6 | Storage and quota | A fork gets its **own `totalCost`, starting at zero** — which silences the session notice on the exact conversation shape it was built to catch. A copy also re-runs LTM extraction into an **actor**-scoped namespace. |

**Net recommendation: re-cut the phases.** Ship **regenerate first**, in place, in the existing session — it is the affordance users ask for most *and* the cheapest, because it needs no new session, no copy and no hierarchy. Fork-to-a-new-session is the expensive one, not the cheap one. See [Recommendation](#recommendation).

---

## Q1 — What does a fork actually copy?

Three candidate semantics. Tracing each through the two readers that matter.

**The two readers are independent code paths in different services.**

1. **The model's history** — `TurnBasedSessionManager.initialize()` (`turn_based_session_manager.py:151`) calls `super().initialize()`, which restores through `AgentCoreMemorySessionManager.list_messages` against `(memory_id, actor_id=user_id, session_id)`. There is no other input. The docstring at `:203–212` is explicit that restore "carries no in-process state" — it is a pure function of that triple.
2. **The SPA's transcript** — `get_messages_from_cloud` (`apis/shared/sessions/messages.py:359`) builds its own `AgentCoreMemoryConfig` against the same triple, then joins four session-scoped side stores: `C#`/`D#` metadata rows, pending interrupts, MCP-App UI resources, and tool summaries.

### (a) Pointer only — `parentSessionId`, display-only

Neither reader knows about the pointer. The child session id is new, so `list_messages` returns `[]` in both. Result: **a blank conversation with a breadcrumb**. The model has no history; the transcript is empty. This is "New chat, with a link to where it came from" — it is not a fork, and it is what Proposal #6's Phase 1 literally specifies ("no snapshot round-trip, no rebinding of `agent.messages`").

### (b) Pointer plus copy-on-read

Would require *both* readers to walk the parent chain and concatenate. The SPA side is a normal refactor. The model side is not: it lives inside a Strands `SessionManager` subclass whose contract is single-session, and the walk would have to happen before `super().initialize()` returns — i.e. a second, independent implementation of chain resolution, inside the one method the byte-stability contract is written about.

It also breaks the metadata join outright. `D#` rows are keyed `D#{session_id}#{message_id}` (`metadata.py:171`) and joined **positionally** — `metadata_index.get(str(idx))` at `messages.py:480`, where `idx` is the message's index in the restored list. Artifacts use the same shape (`msg-{session_id}-{index}`, `artifacts/models.py:57`). A concatenated view re-indexes every message, so every child-session row would have to be written against an offset that changes whenever the parent grows.

### (c) Full history copy at fork time

Write the parent's messages as events into the child's AgentCore Memory session, and copy the five side stores (`C#`, `D#`, UI resources, tool summaries, artifacts) plus `preferences` (`SessionPreferences` carries `assistant_id`, `agent_type`, `last_model`, `enabled_tools` — a fork without them runs as a different agent).

This works, and it is the only one that does. It is also not cheap, and it has a cost the proposal does not mention: **copied events re-run long-term-memory extraction, and the namespaces are actor-scoped, not session-scoped.** `session_factory.py:209` and `:218` build `/strategies/{id}/actors/{actorId}` for preferences and semantic facts; only the summary namespace (`:228`) carries `/sessions/{sessionId}`. So forking a conversation extracts the same facts a second time into the *same user's* long-term memory. The duplicate is not confined to the fork.

**Answer to Q1: a fork copies everything or it copies nothing.** There is no useful middle.

---

## Q2 — Where does the copied history come from?

This is the crux, and it has a clean answer: there are exactly three possible sources and two of them are unusable for a resumable fork.

### AgentCore Memory — the only store of model-shaped messages, and not byte-stable against the live list

Restore is not a read; it is a read plus a pipeline. `initialize()` runs, unconditionally and in order:

- `_strip_document_bytes` (`:233` → `:954`) — replaces any `document` block carrying inline `source.bytes` with `{"text": "[Document placeholder: name=…, format=…, original_size=… bytes]"}`.
- `_sanitize_restored_content_blocks` (`:246` → `:916`) — drops blocks without a recognized Bedrock discriminator, and drops messages left empty.
- compaction slicing / truncation (`_apply_compaction`, `:270`).
- `_repair_restored_history` → `_repair_tool_pairing` (`:1098`).

Below that, the SDK's own converter runs `_filter_empty_text` on every restored message (`bedrock_converter.py:82`).

The live, accumulated list goes through **none** of this — `append_message` (`:135`) filters on the *write* side only, and after construction the list is append-only. So for any session that ever carried an inline attachment, the Memory-derived form and the live form differ at that index, by construction and by design. That is the divergence `_adopt_session_conversation`'s docstring already names as the reason re-restoring on a stale hit was rejected (`service.py:163–168`).

### `GET /messages` — a display projection, lossy by construction

`_convert_content_block` (`messages.py:131`) is an `if/elif` chain on `"text" / "toolUse" / "toolResult" / "image" / "document" / "reasoningContent"`: **only the first recognized key on a block survives**. `_ensure_image_base64` and `_ensure_document_base64` reshape `{"source": {"bytes": …}}` into `{"format": …, "data": …}`. The output is `MessageResponse`, not a Bedrock `Message`. It cannot be fed back to the model.

Worth naming because it is a near-miss: **we already ship a point-in-time conversation copy.** `shares/service.py:87` snapshots `get_messages()` output plus session metadata plus pinned artifacts into S3, and `models.py:101` states the point-in-time promise explicitly. It is a working precedent for copying a conversation — and it is deliberately *not resumable*, for exactly the reason above.

### A Strands snapshot — the only byte-stable source

`Agent.take_snapshot` deep-copies `agent.messages` verbatim (`agent.py:1570`); `load_snapshot` deep-copies them back (`:1607`). Measured in §Q5: identical `historyHash`, including through a full JSON transport. This is the proposal's "in the feature's favour" note, and it holds.

> ⚠️ **A structural finding the queue entry does not have.** strands 1.55.0 also ships `strands/session/snapshot_session_manager.py` — a full `SnapshotSessionManager` with append-only immutable checkpoints, `list_snapshot_ids`, and `restore_snapshot(snapshot_id=…)` time travel. It is *not* an option for us, and the reason is structural rather than a judgement call: an `Agent` has exactly one `session_manager`, and ours is `AgentCoreMemorySessionManager`. Adopting `SnapshotSessionManager` **is** snapshot-as-store-of-record, which was already rejected for breaking LTM extraction. The manual `take_snapshot` / `load_snapshot` pair, used alongside our session manager with storage we own, is the only usable surface — which is what the queue entry said, now with an independent reason.

**Answer to Q2: from a snapshot. Any other source is either lossy or not byte-stable against the live list** — and it is a prompt-cache problem before it is a correctness one.

### On the 1.55.1 fix

Proposal #6 notes `fix(session): filter malformed immutable snapshot IDs (#4199)` as "a fix landing on exactly this API". Checked: it lands on `SnapshotSessionManager.list_snapshot_ids`, whose 1.55.0 body is `sorted(match.group(1) for key in keys if (match := _SNAPSHOT_REGEX.search(key)))` with no `is_uuid7` filter — a class we cannot adopt. The manual pair is **byte-identical between 1.51.0 and 1.55.0** (diffed `types/_snapshot.py` and the `take_snapshot`/`load_snapshot` bodies from the uv cache: no differences). **The spike does not depend on the 1.55.1 fix, and no bump is needed.** (The same diff is what makes the probe below faithful to the pin — see §Q5.)

---

## Q3 — Does Phase 1 actually reach the product goal?

**No.** Three independent reasons, in increasing order of how hard they are to design around.

### 1. Phase 1 as written produces an empty conversation

Per §Q1(a). "`parentSessionId` + a branch label, no snapshot round-trip, no rebinding of `agent.messages`" describes a metadata row and nothing else. To make it a real fork you must add the full copy — which is the expensive work the phase was defined to avoid, and which drags in LTM double-extraction (§Q1), five side stores, and a positional re-index.

### 2. Regenerate and edit-and-resend need a primitive Phase 1 has no way to express

Both mean: **discard the tail of an existing conversation and continue from before it.** The invoke contract carries `session_id` and `message` and nothing else — `InvocationRequest` (`inference_api/chat/models.py:104`) has no history override and no start index. History is 100% server-derived from the session id. No field on the session-metadata row can say "turn 7 onward is dead", because the two readers never consult that row for history; they read AgentCore Memory.

This is the honest core of the answer. **Phase 1 delivers fork and only fork**, and fork is the affordance users ask for *least* of the three.

### 3. The sidebar cannot render hierarchy cheaply

`session-list.ts:89` groups sessions into **recency buckets** (Today / Yesterday / Last 7 Days / Last 30 Days / Older) over a **paginated** list backed by GSI4 (`metadata.py:1078`, `GSI4_SK = {lastMessageAt}#{session_id}`). "Forks as indented siblings" fights both: a fork and its parent routinely land in different buckets, and after a few days of use the parent is frequently not on the loaded page at all. Making the indent correct means either loading the whole chain out of band or abandoning recency ordering for forked rows.

A "forked from *<parent title>*" line in the conversation header is strictly cheaper and probably better; the indent is a design question this spike does not settle.

### Why the Claude Code `/fork` analogy does not carry

`/fork` is metadata-cheap there because the transcript is a **local JSONL file** that can be copied with the filesystem. Ours is a managed remote event log with four positional side stores hanging off it, an actor-scoped LTM extractor reading it, and a denormalized cost aggregate on a separate row. The cheapness is a property of the storage, not of the idea.

---

## Q4 — What breaks the alias, and what does a correct design do?

### Measured, not inferred

`Agent.messages` is a plain instance attribute — there is no property or setter on the class. `load_snapshot` does:

```python
if "messages" in data:
    self.messages = copy.deepcopy(data["messages"])   # agent.py:1607
```

A **rebind**. Probe result against a real `strands.Agent`:

```
load_snapshot rebinds messages: True
after rewind: A=5 B=4 alias_intact=False
```

Instance B — standing in for the second cached agent that adopted A's list by reference — stays at 4 messages while A appends its post-rewind turn. **The alias is broken by `load_snapshot` itself, by construction.** This is precisely the hazard `_adopt_session_conversation` warns about (`service.py:150–157`: "A future compaction that rebinds mid-life would silently break the alias") and precisely why `_drop_abandoned_turn_tail` (`stream_coordinator.py:105`) mutates in place and says so in its docstring.

`load_snapshot` also rebinds `self.state` (`AgentState(data["state"])`), `self._interrupt_state` and `self._model_state`. Nothing aliases those today; a design that starts to would inherit the same class of bug.

### The test cannot catch it — and that is structural, not stylistic

`test_second_cache_key_for_a_session_shares_the_conversation` (`backend/tests/apis/inference_api/test_chat_service.py:292`) is well written and deliberately future-proofed: its docstring says "a fix may reuse one instance, hand the message list between instances, or re-restore on a stale hit, and this test should pass either way."

But it patches `service.create_agent` and asserts on the return of `service.get_agent`. It exercises **the `get_agent` boundary only.** A snapshot design loads at the head of a turn — in `stream_coordinator` / `chat/routes.py`, *after* `get_agent` has returned. The test would stay green while the behaviour it protects is gone.

**Re-reasoned version:** the assertion has to move to the turn boundary. Run two turns under two different cache keys *through the code path that would call `load_snapshot`*, and assert the second key's instance observes the first's appends. That test does not exist today, and **it should be written before any snapshot work**, not after — otherwise the regression it guards is unobservable in CI.

### What a correct design does

| Option | Mechanism | Verdict |
|---|---|---|
| **Splice, don't rebind** | Restore everything *except* messages via `load_snapshot` on a `Snapshot` whose `data` has `messages` removed, then `live[:] = snapshot.data["messages"]` on the existing list object. Verified expressible: `Snapshot(scope="agent", schema_version="1.0", data={k: v for k, v in snap.data.items() if k != "messages"}, app_data={})` loads cleanly. | **Recommended.** Preserves the alias; same discipline `_drop_abandoned_turn_tail` already uses. |
| **Retire the alias** | One session = one `Agent` instance regardless of configuration; move system prompt / tools / model off the cache key onto per-call parameters. | The "structural answer to the `CLAUDE.md` rule" the queue entry hopes for — and much larger than Phase 2, since the cache key is what makes a tool or skill edit take effect at all. Not a spike-sized change. |
| **Re-restore every turn** | Drop the alias, restore from Memory on every turn. | Rejected in the docstring on prompt-cache grounds; §Q5 measures why. |

---

## Q5 — Prompt-cache impact of each route

### The probe

Run against a real `strands.Agent` with a `MagicMock` model (no network), on a four-message history containing a `toolUse`/`toolResult` pair. `historyHash` computed the way `PrefixFingerprintHook` does (`prefix_fingerprint.py:93` — canonical JSON over `agent.messages`).

```
snapshot data keys: ['conversation_manager_state', 'interrupt_state', 'messages', 'model_state', 'state']
schema_version: 1.0 | scope: agent
byte-stable round-trip        : True e3b4d926d02b4c7e
byte-stable via JSON transport: True
load_snapshot rebinds messages: True
rewound to 1 message(s): ['user']
```

The local venv is strands **1.51.0** while the pin is **1.55.0**; this is faithful because the snapshot API is byte-identical across that boundary (§Q2). `app_data` survives the transport untouched, confirming the entry's "a bag Strands never reads".

**The claim holds: a snapshot round-trip is byte-stable by construction.**

### The half the review does not say

It is byte-stable *with respect to the snapshot*, not with respect to AgentCore Memory. Per §Q2, restore rewrites document blocks to placeholders and filters content blocks; the live list does not. So a design that snapshots from the live agent but **falls back** to a Memory restore — cold container, snapshot miss, container hop, snapshot-store outage — alternates between two byte-forms of the same conversation. `historyHash` flips on an arbitrary turn, `toolConfigHash` and `systemPromptHash` hold, and the whole 35k–150k prefix is re-written at the cache-write premium. That is the same failure mode the byte-stability contract at `turn_based_session_manager.py:15–23` exists to prevent, arriving through a new door.

The `C#`-row fingerprints are the right instrument and need no new work: `historyHash` diverging while the other two hold *is* the signature of a history-source flip.

### Per route

- **Fork with a full copy.** The child's first turn re-sends the whole copied prefix. Whether that is a write or a read is an open question with a real upside: Bedrock's cache is keyed on prefix **content**, not on our session id, so a byte-identical copy could *read* the parent's live entry. #1012 already proved a cache entry surviving a change on our side of the wire (a version boundary). It has never been measured across session ids, and TTL bounds the win — a fork minutes later could hit, a fork the next day cannot. **Only available if the copy comes from a snapshot**; a Memory re-derive is a different byte-form and guarantees a write.
- **Regenerate / edit-and-resend.** These *shorten* the prefix, so everything up to the cut is unchanged and should still read. Rewinding is the cache-friendly operation. The risk here is not the cache; it is the store of record (§Q1, §Q6).
- **Per-turn `load_snapshot` as the general fix for the agent-cache rule.** Byte-stable, but see Q4: it must splice rather than rebind, and it adds a storage round-trip to every turn — including cache hits, which today do no I/O at all. That trade needs measuring before it is called an improvement.

---

## Q6 — Storage and quota

### A forked session gets its own `totalCost`, starting at zero

The session row is `PK=USER#{user_id}`, `SK=S#{session_id}` (`metadata.py:1072`), and `_bump_session_aggregates` (`metadata.py:1629`) does `ADD totalCost :c` against the row addressed by *that* session id. A new session id is a new row is a fresh zero.

The quota **session notice** reads exactly that field: `QuotaChecker._resolve_session_notice` (`quota/checker.py:212–254`) fetches the session metadata and compares `metadata.total_cost` against `session_notice_threshold_usd(limit, tier)`. `TopSessionCost` (`admin/costs/models.py:182`) documents the same field as the session's **lifetime** cost and explains why: "a runaway conversation is usually a single long thread that spans period boundaries (the incident session opened 2026-07-30 and blocked a quota on 2026-08-04)".

**So forking a runaway conversation resets the notice to zero while carrying the entire expensive prefix forward.** The user's monthly quota is unaffected — that is `user-cost-summary`, keyed on user — so this is not quota evasion. It is worse in a subtle way: it silences the one signal built specifically to catch a single runaway thread, on the exact conversation shape most likely to be forked.

Mitigation, and this must be a deliberate decision rather than a default:

- **Seed the child's `totalCost` with the parent's at fork time.** Recommended. The notice's dedupe is keyed `(user, "session_notice", session_id)` within 60 minutes (`event_recorder.py:130–141`), so a seeded child fires its own notice correctly and immediately.
- *Not* a chain-walk at notice time: that is a per-turn read amplification on the hot quota path for a signal that fires rarely.

### Storage

A copy duplicates: AgentCore Memory events, `C#` + `D#` rows, MCP-App UI resources (`ui_resource_store.py:190`, `GSI_PK=SESSION#{id}`), tool summaries, and artifact rows — plus a second LTM extraction pass into an **actor**-scoped namespace (§Q1). Admin cost attribution then double-counts by construction: `TopSessionCost` lists parent and child at their lifetime totals, and the fleet already has a known over-count in `AdminUsageAggregates`. Adding a legitimate duplication vector on top of that deserves an explicit decision.

**Cheap mitigation worth naming:** copy lazily, on the fork's **first turn**, not at fork creation. A fork the user opens and abandons then costs one metadata row.

---

## Recommendation

**Do not ship Phase 1 as specified.** Re-cut the phases so the cheap thing is the thing users actually ask for.

### Phase A — regenerate, in place, in the existing session · **Med, ~3–5 days**

No new session, no copy, no sidebar hierarchy, no alias break. What it needs is one new primitive: **truncate a session's history by N trailing messages.** Three parts:

1. **In memory** — pop the trailing assistant turn off `agent.messages` *in place*. `_drop_abandoned_turn_tail` (`stream_coordinator.py:105`) already does exactly this shape, in place, for a different reason.
2. **In AgentCore Memory** — delete the corresponding events. `gmdp_client.delete_event` is already used by `update_message` (`session_manager.py:710`), so the capability exists. ⚠️ See the unknown below — addressing them is the hard part.
3. **In DynamoDB** — delete the `C#`/`D#` rows (and artifact rows) at and above the cut, *before* the replacement turn writes. Because the `D#` join is positional (`D#{session_id}#{message_id}`), truncate-then-append **collides** new rows with orphans if you overwrite instead of deleting.

Cache-friendly: it shortens the prefix, so everything before the cut still reads.

### Phase B — edit-and-resend · **Low on top of A, ~1–2 days**

The same primitive with the cut one message earlier, plus a composer affordance. Nothing new structurally.

### Phase C — fork to a new session · **Med–High, 2+ weeks, gated**

Worth doing only after A and B prove the truncation primitive. Uses a snapshot as the copy source (§Q2), copies lazily at the fork's first turn (§Q6), seeds `totalCost`, and needs an answer on suppressing LTM extraction for copied events. The sidebar question (indent vs. a header line) is a separate design decision, not a blocker.

### Sequencing notes

- **Write the turn-boundary alias test first** (§Q4). It is a precondition, not a follow-up.
- **Do not bump to 1.55.1 for this.** The #4199 fix lands on a class we cannot adopt; the API we would use is byte-identical across 1.51.0→1.55.0 (§Q2). `CLAUDE.md` forbids installing without explicit approval, and there is no reason to ask.
- **Do not revisit the subtraction audit.** Recorded in the `[2026-09-08]` queue entry, four candidates, zero replaceable.

---

## What remains unknown

1. **Can a single message be addressed for deletion in AgentCore Memory?** *The most important unknown, and it gates Phase A.* `batch_size` defaults to 1 and we never override it (`session_factory.py:28` says so, `config.py:93` confirms the default), so it is one `create_event` per message — 1:1, good. But `append_message` (`session_manager.py:832–843`) persists `SessionMessage.from_message(message, 0)` — **message id `0` in the payload** — and keeps the real `eventId` only in the in-process `_latest_agent_message`. On restore, `events_to_messages` (`bedrock_converter.py:81`) rebuilds each `SessionMessage` from that payload, so **restored messages do not carry their event ids**. Truncation therefore has to drop to `gmdp_client.list_events` and pair events to messages by order — feasible at `batch_size=1`, but the pairing must survive blob events and the `_filter_empty_text` drop on read, which can make the two counts disagree. **Needs a live read of a real dev-ai session's event list before Phase A is committed to.** If the pairing turns out to be unreliable, in-place regenerate is off the table and every affordance routes through a new session, which changes the whole recommendation.
2. **Does writing copied events into a new session trigger LTM extraction, and can it be suppressed per event?** Not answerable from our code or the pinned SDK. Needs a dev-ai probe against a memory with strategies configured. Gates Phase C's real cost.
3. **Does a Bedrock cache entry written under session A get read under session B with a byte-identical prefix?** Almost certainly yes — the cache is keyed on content — but never measured across session ids. One salted two-arm probe using the #1012 method would settle it, and it is the difference between a fork's first turn being free and being a full-prefix write.
4. **Should the sidebar show hierarchy at all?** Not settled here. The recency-bucket + pagination conflict (§Q3) is real; a header line inside the conversation may dominate the indent on both cost and clarity.

---

### Refs

| Thing | Where |
|---|---|
| The alias and why it is by-reference | `backend/src/apis/inference_api/chat/service.py:133` |
| Rebinding mid-life breaks it (in-place, deliberately) | `backend/src/agents/main_agent/streaming/stream_coordinator.py:105` |
| The guarding test | `backend/tests/apis/inference_api/test_chat_service.py:292` |
| Restore pipeline + byte-stability contract | `backend/src/agents/main_agent/session/turn_based_session_manager.py:15`, `:151`, `:916`, `:954` |
| SPA transcript read + positional metadata join | `backend/src/apis/shared/sessions/messages.py:359`, `:480` |
| Display projection is lossy | `backend/src/apis/shared/sessions/messages.py:131` |
| Existing point-in-time conversation copy | `backend/src/apis/app_api/shares/service.py:87` |
| Static SK + GSI4 recency keys | `backend/src/apis/shared/sessions/metadata.py:1072`, `:1078` |
| `totalCost` aggregation | `backend/src/apis/shared/sessions/metadata.py:1629` |
| Session-notice threshold read | `backend/src/agents/main_agent/quota/checker.py:212` |
| Invoke contract has no history override | `backend/src/apis/inference_api/chat/models.py:104` |
| Prefix fingerprints | `backend/src/agents/main_agent/session/hooks/prefix_fingerprint.py:93` |
| Sidebar recency grouping | `frontend/ai.client/src/app/components/sidenav/components/session-list/session-list.ts:89` |
| Message actions today (Copy + Continue only) | `frontend/ai.client/src/app/session/components/message-list/components/message-actions.component.ts` |
| `take_snapshot` / `load_snapshot` | `strands/agent/agent.py:1543`, `:1591` |
| `SnapshotSessionManager` (not adoptable) | `strands/session/snapshot_session_manager.py` |
| `append_message` persists message id `0` | `bedrock_agentcore/memory/integrations/strands/session_manager.py:832` |
