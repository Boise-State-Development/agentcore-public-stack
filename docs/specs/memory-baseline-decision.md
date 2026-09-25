# AgentCore Memory baseline: decision record (Shared Projects Phase 0)

**Status:** **Decided: C (hybrid).** Dev evidence 2026-09-25; the step-5 re-test passed after Phase 0.2 (see "Re-test after Phase 0.2"). Prod read path and record census checked 2026-09-25 (see "Production (read-only)").
**Spec:** `shared-projects.md` §1 (procedure §1.2, options §1.3), PR plan §7 Phase 0.
**Tool:** `scripts/memory-audit/audit.py` (`inventory` and `probe`), plus a manual two-chat test on dev.boisestate.ai.
**Privacy:** every figure below is an aggregate. Record text, actor ids and account-specific identifiers stay in the auditor's scratch directory.

## Answer in one paragraph

Long-term memory in dev is **written, extracted and stored correctly, and never used.** Events carry the right actor (the Cognito `sub`). Extraction runs in about 70 seconds. 60 of 68 actors have records, and retrieval queries the namespace the records live in. But the runtime discards every retrieved record scoring below **0.7**, and a natural question about a stored fact scores **0.57–0.67**. Only a near-verbatim restatement of the fact clears the cut (0.86–0.91). Dev logged **zero** turns with injected memory context in 7 days, and the two-chat test failed. The fix is one configuration value, not a redesign. The recommendation is **C (hybrid)**, conditional on re-running the behavioral test after that fix (Phase 0.2).

## Evidence (dev, per §1.2 step)

### 1. Inventory

| | |
|---|---|
| Memory status | ACTIVE, event expiry 90 days |
| Strategies | `SEMANTIC`, `SUMMARIZATION`, `USER_PREFERENCE`, all ACTIVE, names as in `memory-construct.ts` |
| Namespace templates (AWS defaults; CDK sets none) | `/strategies/{memoryStrategyId}/actors/{actorId}/` for semantic and preference; `…/actors/{actorId}/sessions/{sessionId}/` for summaries |
| What the backend queries (`session_factory.py`) | The same paths **without the trailing slash**, via `namespacePath` |
| Byte-for-byte match | **No** (trailing slash only) |
| Effect of the mismatch | **None.** The probe got identical hits and scores with and without the slash, because `namespacePath` is hierarchical. It is not the cause of the failure below. |

### 2. Write path

- 68 actors and 1,187 sessions.
- 66 actor ids are UUIDs in the Cognito `sub` shape.
- 2 are non-UUID ids, both known dev test identities.
- **0 actors match the `user_id or session_id` fallback** (`base_agent.py:97`): no actor owns a session named after itself.

### 3. Extraction

- **2,425 records:** 1,749 summaries, 439 semantic facts, 237 preferences.
- **Coverage:** 60 of 68 actors have at least one record; 33 have semantic facts and 57 have preferences.
- **Records per actor:** most actors hold 5–9. Five heavy dev users hold 100+.
- **Record age:** median 24 days; 256 records created in the last 7 days.
- **Extraction latency:** the probe's first records landed about 70 seconds after the events were written. The preference record can trail the others by a minute or more.
- **`ListMemoryExtractionJobs`:** 0 jobs eligible to restart, i.e. no failed jobs.
- **Quality**, from 10 random records per strategy (categories only):
  - *Semantic:* mostly usable durable facts: program of study, recurring technical interests, personal routines, course history. A minority are ephemeral (the state of one QA session) or unverified self-descriptions. Some copy **third-party details or academic records out of tool results** (for example an advisor's contact details, a course grade). Retrieval stays scoped to the same actor, but this belongs in the Phase 2 retention and review design.
  - *Preferences:* reasonable, but often generalized from one session's topic ("asked about caching" becomes "prefers performance topics").
  - *Summaries:* per-session narratives of 900–1,800 characters. They are not retrieved per turn (`summary_namespace_retrieval_enabled` is off by default).

### 4. Read path

- **Runtime log, last 7 days:**
  - `No memory strategies found`: **0**. Discovery works.
  - `Long-term memory: Enabled`: about 150 agent builds (2 namespaces; a handful with 3).
  - `Retrieved N customer context items`: **0**. No turn received memory context.
  - Retrieval failures and throttles: **0**.
- **Alarms:** `agentcore-memory-throttles` and `agentcore-memory-system-errors` are OK.

### 5. Behavioral test

**Service probe** (`audit.py probe`, synthetic actor, cleaned up afterwards). The runtime's own retrieval parameters (`topK=10`, relevance ≥ 0.7) were replayed:

| Query | Semantic top score | Preference top score | Kept by the 0.7 cut |
|---|---|---|---|
| Indirect ("which room should I go to…") | 0.567 | 0.566 | no |
| Direct question ("where is my … office?") | 0.624 | 0.645 | no |
| The fact restated verbatim | 0.907 | 0.859 | yes |

The stored fact was the only hit in each case. The ranking is right; the cut is wrong.

**App test** on dev.boisestate.ai, as a real user:
- Chat A stated a synthetic fact. The semantic record was extracted within about 90 seconds.
- Chat B asked for it. **The model said it did not know. Fail.**
- Replaying chat B's question against that user's namespace: the fact ranked **#1 at 0.671**, and the next-best record scored 0.395. A cut near 0.5 would have injected exactly the right record and nothing else.
- The "fresh container" rerun was skipped. The failure is a deterministic filter, not the in-process agent cache, so a cold start cannot change it.

### 6. Cost and latency (dev, 30 days)

| Line item | Usage | Cost |
|---|---|---|
| Short-term memory (events) | 9,478 | $2.37 |
| Long-term memory retrieval | 5,456 calls | $2.73 |
| Long-term memory storage | — | $1.36 |
| **Total** | | **$6.46** |

- Each retrieval call took 230–310 ms. The two namespaces are queried in parallel, so a turn pays about 0.3 s.
- Today that time and money buy nothing: every result is discarded, and the `<user_context>` block adds 0 tokens.
- After a fix, expect a few hundred tokens on turns that hit. They land on the user message, after the system prompt's cache point, so the cached prefix is untouched.

## Decision

| Option | Selected when (§1.3) | Verdict |
|---|---|---|
| A. Fix and keep, composite actor for projects | Step 5 passes, records usable, cost acceptable | Step 5 fails today. Records and cost are fine. Rejected for project scope anyway: a composite `"{userId}::{projectId}"` actor splits history restore and doubles retrieval calls. |
| B. Unify on files | Step 5 fails **or** records are low quality | Step 5 fails for a configuration reason, not a quality reason. Moving personal global memory would throw away a pipeline that works end to end except for one constant. |
| **C. Hybrid** | Step 5 passes for global memory | **Recommended.** Keep AgentCore for short-term events and personal *global* extraction (after the 0.2 fix). Use Memory Spaces for project and personal-in-project scopes, which need the browse/edit/delete UI anyway. |

**Condition met:** the step-5 app test passed on dev after Phase 0.2 lowered the relevance cut (below). C stands.

**Unchanged by this audit:** AgentCore Memory is not the system of record for project memory (§1.3 "Fixed regardless").

## Phase 0.2 (as built)

1. **Relevance cut.** The runtime default for `AGENTCORE_MEMORY_RELEVANCE_SCORE` drops from 0.7 to **0.5** (`agents/main_agent/config/constants.py`).
   - The 0.5 is backed by dev scores: correct records 0.57–0.67, unrelated records ≤ 0.40.
   - **Deviation:** not set in CDK. The AgentCore Runtime is at 47 of its 50 environment variables, and the existing variable already overrides the default wherever it is set. Adding it to CDK is a one-line follow-up if a per-environment value is ever wanted.
   - This changes what users' turns contain: turns with a relevant record gain a `<user_context>` block on the user message, after the prompt-cache point. The step-5 re-test after deploy passed; see "Re-test after Phase 0.2".
2. **Session delete purges extracted summaries.** `SessionService.delete_agentcore_memory` now also deletes the SUMMARIZATION records under `…/sessions/{sessionId}/` (exact session match; batches of 100). This runs even when the session's events have already expired.
   - **Semantic facts and preferences are left alone.** Records carry only type and timestamp metadata, no source session, and those strategies consolidate across sessions, so no record is attributable to one session. Removing them stays a user action on the memory dashboard.
3. **Share-fork stops feeding extraction.** Every event the fork writes under the forking user carries `extractionMode="SKIP"`. The events stay in short-term memory, so the fork's history loads, but they never become the forker's long-term records.
   - The SDK's `create_message` has no extraction argument, so the fork wraps its own session manager's data-plane `create_event`.
   - A test runs the pinned SDK's `MemoryClient.create_event` to catch an upgrade that bypasses the wrapper.
4. **IAM parity.** Both Runtime memory statements (`AgentCoreMemoryAccess`, account-wide, and `MemoryAccess`, scoped to this memory) now use one list, `RUNTIME_MEMORY_ACTIONS`. The scoped statement had lacked `GetMemory` under a comment wrongly saying it was not a real IAM action.
5. **Stale lines corrected** in `user-markdown-memory.md`, `agentic-platform-primitives.md` and the `app_context_dispatch.py` docstring, which cited a deleted analysis doc.
6. **CDK test** (`infrastructure/test/agentcore-memory.test.ts`) asserts:
   - the three strategy names by type;
   - that no strategy sets `namespaces` (the backend depends on AWS defaults);
   - the 90-day event expiry;
   - identical Runtime memory action sets, including `GetMemory`.

## Re-test after Phase 0.2 (dev, 2026-09-25)

Run against the Runtime version that shipped 0.2, which has no relevance override, so the new 0.5 default applies.

| Check | Result |
|---|---|
| New cut is live | Agent builds log `Retrieval: top_k=10, relevance_score=0.5` |
| Chat A states a synthetic fact | 1 semantic record + 1 summary extracted within about 100 s |
| Chat B (a new session) asks for it | **Recalled correctly.** The answer quoted the fact. |
| Runtime log for chat B's turn | `Retrieved 1 customer context items`: one item, the right one, no unrelated records. No retrieval failures or throttles. |
| Session delete purges summaries (0.2 fix 2) | Deleting chat A removed its summary record. The semantic fact stayed, as designed, and was then removed by hand. |

Chat B was a new session with a newly built agent, so the fact could only have come from long-term memory, not from the in-process agent cache or the conversation history.

## Production (read-only, 2026-09-25)

Read-only checks against the production account: runtime logs (`FilterLogEvents`, 7 days to 2026-09-25 20:00 UTC), CloudWatch metrics and alarm history, and a census run by an operator with `audit.py … inventory`. Aggregates only.

**What prod runs.** Prod has release 1.24.0, not Phase 0.2:
- **Relevance cut 0.7.** Every one of 2,878 agent builds logged `Retrieval: top_k=10, relevance_score=0.7`.
- **Bounded retrieval client (#1157)**, live since release 1.23.0 reached prod on 2026-09-20 at about 21:00 UTC. It makes one attempt with a 2 s timeout. Before that, retrieval used the SDK's client and boto's default retries.

### Record census (`audit.py inventory`, 2026-09-25)

| | Prod | Dev (§3 above) |
|---|---|---|
| Memory | ACTIVE, 90-day event expiry, same three strategies, all ACTIVE | Same |
| Namespace templates vs backend queries | Trailing slash only, as in dev (harmless) | Same |
| Actors | **2,770**; 11,286 sessions with live events | 68; 1,187 sessions |
| Actor id shape | 2,745 UUIDs (version 7); 25 non-UUID; **0 match the `user_id or session_id` fallback** | 66 UUIDs (version 4, Cognito `sub`); 2 test ids; 0 fallback |
| Records | **85,382**: 44,627 summaries, 28,377 semantic facts, 12,378 preferences | 2,425: 1,749 / 439 / 237 |
| Actors with any record | 2,340 (84%); 2,206 with semantic facts, 2,097 with preferences | 60 of 68 (88%) |
| Records per actor | Most hold 5–24 (58% of actors with records); 175 hold 100+ | Most 5–9; five hold 100+ |
| Record age | Median 28 days, p90 70, max 93; 10,363 created in the last 7 days | Median 24 days; 256 in 7 days |
| Record length (median / p90 characters) | Summary 1,409 / 2,867; semantic 253 / 1,644; preference 516 / 2,660 | Summaries 900–1,800 |
| Failed extraction jobs | **7**, all `LTM_RATE_EXCEEDED` (3 semantic, 2 preference, 2 summary) | 0 |

**Prod writes and extracts like dev, at about 35 times the scale.** Every actor id is a UUID or one of the legacy ids below, and none falls back to a session id. Prod's user ids use a different UUID version than dev's Cognito subs. The retrieval hits in the read-path table show that the records and the retrieval namespace agree on the actor.

**Legacy actors.** The 25 non-UUID actors are 9-character ids in an older user-id format, and none has live sessions. 13 of them still hold 112 records, all created in June 2026. The backend now queries by UUID, so these records are never retrieved. Deleting them is a prod write, so it stays an operator decision; it is not needed for correctness.

**Extraction throttling.** Seven extraction jobs failed with `LTM_RATE_EXCEEDED`. Their events date from 2026-09-18, 09-23 and 09-24, all between 18:00 and 23:00 UTC. That is daytime use, not the load-test nights. Seven failed jobs against about 10,400 records extracted in the same week is negligible; the service lists them as restartable (`StartMemoryExtractionJob`, a write). This is a rate limit inside the extraction pipeline. It is separate from the `RetrieveMemoryRecords` quota, which saw no throttling.

**Census log counts agree** with the analysis below: 81 hit lines (about 40 turns, each logged twice) and 0 throttle lines.

### Read path (runtime logs, 7 days)

| | Prod | Dev (same check, before 0.2) |
|---|---|---|
| Agent builds with long-term memory | 2,878 (2,143 with 2 namespaces, 735 with 3) | ~150 |
| Turns | 6,249 (5,531 since the bounded client; 63% served by a cached agent) | — |
| Turns with injected context (`Retrieved N customer context items`) | **43 (0.7%)**. Items per turn: 1 ×24, 2 ×5, 3 ×9, 4 ×3, 5 ×2 | 0 |
| Retrieval throttles | **0** | 0 |
| Retrieval failures | **560 turns (10.1% of turns since the bounded client)**; see below | 0 |

Prod clears the 0.7 cut now and then, where dev never did. The dev probe explains it: only near-verbatim restatements score above 0.7. The 0.5 default should raise the hit rate in prod as it did in dev.

### The ~1,300 failure lines

The earlier count was 1,470 lines. That is **745 events, each logged twice** (plain text and OTEL JSON). **None is a throttle.**

| Kind | Level | Events | Turns | Cause |
|---|---|---|---|---|
| `Failed to retrieve customer context: 'NoneType' object has no attribute 'get'` | ERROR | 473 | 473 | Dead pooled connection, masked by a handler bug (below) |
| `memory retrieval failed … SSL: UNEXPECTED_EOF_WHILE_READING` | WARNING | 104 | 102 | Dead pooled connection |
| `ValidationException`: `searchCriteria.searchQuery` longer than 10,000 characters | WARNING | 168 | 84 | User message over the API's query limit |
| `memory retrieval throttled` | INFO | 0 | 0 | — |

Turns with at least one failure (560) is less than the sum of the Turns column because 99 of the SSL turns also carry the masked error from another namespace. Connection failures hit 476 turns in all.

**Per day** (turns with any failure / turns):

| 09-20 (from 21:00) | 09-21 | 09-22 | 09-23 | 09-24 | 09-25 (to 20:00) |
|---|---|---|---|---|---|
| 17 / 123 | 106 / 1,331 (8.0%) | 109 / 1,005 (10.8%) | 104 / 921 (11.3%) | 138 / 1,481 (9.3%) | 86 / 670 (12.8%) |

**By hour.** Failures follow the daytime usage curve. 72% fall between 15:00 and 24:00 UTC, and none between 08:00 and 11:00 UTC.

**Not the load tests.** The load-test bursts were on 09-09 to 09-11, outside this window, and their data was cleaned from prod on 09-23. These failures come from real sessions at a steady daily rate, with human-scale pauses between turns.

**Connection failures are a stale-connection problem.** Evidence:
- **Only cached agents fail.** 475 of the 476 turns with a connection failure were served by a cached agent, which reuses its session manager's retrieval client. None was on a session's first turn.
- **They fail almost instantly.** In sampled traces the error lands 13–18 ms after the user message is written. The service's own `RetrieveMemoryRecords` latency is p50 211–227 ms and p99 325–660 ms. The request never reached the service, so this is not the 2 s timeout.
- **The idle time since the session's previous turn decides it.** Rows are cached-agent turns only:

  | Idle before the turn | Turns | Connection failures |
  |---|---|---|
  | under 3 min | 1,859 | 1 (0%) |
  | 3–5 min | 462 | 5 (1%) |
  | 5–6 min | 136 | 15 (11%) |
  | 6–15 min | 529 | 431 (81%) |
  | over 15 min | 26 | 21 (81%) |

  Something on the path drops a connection that has sat idle for about 6 minutes; a 350 s idle timeout (a NAT gateway's) would fit, but this is not confirmed. The next request on that connection then fails.
- **They started with #1157.** It set `total_max_attempts=1` so a throttle never adds retry backoff to first-token latency. That also turned off boto's retry of connection errors, which used to reconnect silently. In the 48 hours before release 1.23.0, the SDK client logged **0** connection errors (and 69 validation warnings, the same query-length class).

**Handler bug.** `ConnectionClosedError` (and `ReadTimeoutError`) carry `response = None`. The per-namespace handler calls `getattr(e, "response", {}).get(...)`, which raises on `None`, and the exception escapes to the outer handler. So a single dead connection throws away **every** namespace's results for the turn, not just the failed one. The log also loses the real exception class.

**Throttle alarm and service metrics.**
- `agentcore-memory-throttles` was OK for the whole window. It last fired on 2026-09-11 from 04:04 to 04:23 UTC, a load-test night. `agentcore-memory-system-errors` was also OK.
- `RetrieveMemoryRecords` published **no** `Throttles` or `SystemErrors` data points in the window. `Errors` equals `UserErrors` at 17–46 a day, which is the validation class.
- Peak load on the busiest day was 38 calls a minute (0.6/s), about 50 times below the 30/s default quota. **No quota increase is needed** at organic load.

**Impact today.** Small. At the 0.7 cut only 0.7% of turns inject anything, and a failed retrieval adds no latency (it fails in milliseconds). **After 0.2 reaches prod**, the 0.5 cut makes retrieval matter. These failures would then drop memory on about 10% of turns, mostly the first turn after a pause, which is when recall helps most.

### Recommended fix (not implemented)

Land Fixes 1 and 2 before, or together with, the release that carries Phase 0.2 to prod.

1. **Handler.** Use `(getattr(e, "response", None) or {})`, so a connection error stays a per-namespace warning and the other namespaces' results are kept.
2. **Retry connection errors once, but not throttles.** In `retrieve_for_namespace`, retry once with no backoff on botocore `ConnectionClosedError`, `SSLError` and `EndpointConnectionError`. Leave `total_max_attempts=1`, so a throttle still costs nothing.
   - **Latency cost:** one new TCP and TLS handshake, only on turns whose pooled connection is dead (about 14% of cached-agent turns today). This is an estimate, not measured; expect tens of milliseconds before the model call on those turns.
   - **Alternative:** close the client's pool when it has been idle for more than about 5 minutes. That pays the same handshake up front, on the same turns.
3. **Query length.** Truncate `searchQuery` to the API's 10,000-character limit. Affects 1.5% of turns (long pasted text). The SDK path had the same failure.

Other data-plane calls (`CreateEvent`, `ListEvents`) go through the SDK client with default retries, so any dead connections there are retried and never show up as failures.

## Not yet covered

- **Prod after Phase 0.2.** Hit rate and failure rate once a release carries the 0.5 cut, and Fixes 1–2 above, to prod.
- **Legacy-actor records and failed extraction jobs in prod.** Both need a prod write (delete 112 unreachable records; restart 7 jobs). Neither affects correctness; an operator decides.
- **Consolidation of directly written records.** Whether the service ever consolidates records written straight into a strategy-less namespace (§1.3 residual) was not probed. It matters only for the Phase 3 derived index.
