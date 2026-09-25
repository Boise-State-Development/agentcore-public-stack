# AgentCore Memory baseline: decision record (Shared Projects Phase 0)

**Status:** Recommendation from dev evidence (2026-09-25). The prod census is pending (see "Not yet covered").
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

**Condition:** C stands if the step-5 app test passes after Phase 0.2 lowers the relevance cut. If it still fails, fall back to B for personal memory too.

**Unchanged by this audit:** AgentCore Memory is not the system of record for project memory (§1.3 "Fixed regardless").

## Phase 0.2 (next PR)

1. **Relevance cut.** Lower the runtime default for `AGENTCORE_MEMORY_RELEVANCE_SCORE` from 0.7 to about 0.5, and set it explicitly in CDK so the value is visible and tunable per environment. Then re-run the app test.
   - The 0.5 is backed by dev scores: correct records 0.57–0.67, unrelated records ≤ 0.40.
   - This changes what every user's turns contain, so it deserves its own review, like personal instructions got.
2. **Session delete purges extracted records.** Summaries sit under a per-session namespace and can be deleted directly. Semantic and preference records sit under the actor namespace, so that PR must first establish whether a record can be traced back to its source session (record metadata) before scoping the purge.
3. **Share-fork stops feeding extraction.** `CreateEvent` accepts `extractionMode="SKIP"`, which stores the event for history but excludes it from long-term extraction. This is the exact switch §1.3 asked for.
4. **IAM parity on `GetMemory`** across the two runtime statements.
5. **Correct the stale "write-only" lines** in `user-markdown-memory.md` and `agentic-platform-primitives.md`, and the dead analysis-doc citation in `app_context_dispatch.py`.
6. **CDK test** asserting the three strategy names.
   - Explicit namespace templates are **not** needed: the defaults match what the backend queries once the trailing slash is ignored.

## Not yet covered

- **Prod record census.** It needs the `bedrock-agentcore` data-plane API, which the workstation's AWS CLI lacks, and prod scripting is blocked by the read-only guard.
  - Either an operator runs `audit.py … inventory` with prod credentials (read-only), or the CLI gets upgraded.
  - A read-only prod log check (7 days) showed context injected on about 90 turns against roughly 3,000 agent builds, plus about 1,300 retrieval failure or throttle lines not yet analysed.
- **Consolidation of directly written records.** Whether the service ever consolidates records written straight into a strategy-less namespace (§1.3 residual) was not probed. It matters only for the Phase 3 derived index.
