# AgentCore Memory baseline: decision record (Shared Projects Phase 0)

**Status:** **Decided: C (hybrid).** Dev evidence 2026-09-25; the step-5 re-test passed after Phase 0.2 (see "Re-test after Phase 0.2"). The prod census is pending (see "Not yet covered").
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

## Not yet covered

- **Prod record census.** It needs the `bedrock-agentcore` data-plane API, which the workstation's AWS CLI lacks, and prod scripting is blocked by the read-only guard.
  - Either an operator runs `audit.py … inventory` with prod credentials (read-only), or the CLI gets upgraded.
  - A read-only prod log check (7 days) showed context injected on about 90 turns against roughly 3,000 agent builds, plus about 1,300 retrieval failure or throttle lines not yet analysed.
- **Consolidation of directly written records.** Whether the service ever consolidates records written straight into a strategy-less namespace (§1.3 residual) was not probed. It matters only for the Phase 3 derived index.
