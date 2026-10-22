# Admin cost drill-down: user → conversations → conversation diagnostics

**Status:** PR-1 (API) built · PR-2 (SPA) next · PR-3 (behavioral census) after.
**Scope:** `admin.costs` (delegated admin scope). **Schema:** additive attributes only; **no new table, no GSI operation.**

## 1. Why

Every quota investigation on this platform so far was done by hand-querying DynamoDB (the
`5801a350` write:read audit, 2026-07-27; the compaction spiral, 2026-08-05 — see
`compaction-over-threshold-cache-spiral.md`). Each ended in a *named, computable* classification
and a concrete cost-effectiveness fix, and each took an afternoon. The admin console could not
start the investigation from the place it always starts — **a user who hit their quota** — and it
recorded *what a conversation cost* but never *what the user was doing* in it.

This feature adds the missing path and makes the classifications rules:

```
/admin/costs  Top Users (now with email + quota %)
   └─ /admin/users/:userId            Conversations section (content-free list)
        └─ /admin/costs/sessions/:id  Anatomy (existing) + Profile + Diagnoses (new)
```

## 2. The content-free contract

An admin diagnoses a conversation's spend **without reading it**. That is a property of the data
path, enforced in code:

- `apis/shared/observability/content_policy.py` — `CONTENT_BEARING`, the denylist of every
  attribute on the `sessions-metadata` and `user-file-uploads` row families that carries user
  text or model-generated prose (title, tags, custom prompt, compaction summary, pending
  interrupts, paused turn, export receipts, citations, display text, tool summaries, App cards
  and HTML, steer queue, scheduled prompts, run errors, filenames and S3 keys). A path denies
  everything beneath it; a dotted path denies only that leaf. Matching recognises a denylisted
  path anywhere in a longer one (`calls.citations.text`, `sessions.title`).
- Three allowlisted projections (`SESSION_ROW_PROJECTION`, `CALL_ROW_PROJECTION`,
  `FILE_ROW_PROJECTION`) rendered with every segment aliased, so DynamoDB reserved words never
  need case-by-case handling.
- `compaction.summary` is the one path a reader may *request* but never *return*: its **length**
  is a diagnostic (a summary that is 40% of the window guarantees the spiral). Readers measure
  it into `compaction.summaryChars` and drop the string — the `scan_fleet_prefix_spend.py`
  discipline.
- `tests/apis/app_api/admin/costs/test_content_policy.py` walks every response model in
  `admin/costs/models.py` against the denylist, with **exactly one named exemption**:
  `TopSessionCost.title` (the "Most Expensive Conversations" table keeps titles by decision).
  `tests/costs/test_content_free_projections.py` seeds rows carrying every kind of content and
  proves none of it comes back through the readers.
- Hardening of an existing path: `get_session_cost_records` (the anatomy's read) previously
  fetched whole `C#` items including `citations[].text`; it now projects the twelve attributes
  the anatomy consumes.

## 3. Endpoints (both `require_costs_admin`)

### `GET /admin/costs/users/{userId}/sessions?period&allTime&sort&limit`

`UserSessionsResponse { userId, period?, userPeriodCost?, sessions[], total, unknownCostCount }`.

Each `UserSessionSummary`: `sessionId, createdAt, lastMessageAt, status, messageCount, modelId,
enabledToolCount, agentBound, lastContextTokens, contextWindow, contextShare, totalCost,
costKnown, shareOfUserPeriod, cacheEfficiency, wastedUsd, partialMissUsd, summarizedTurns,
summaryApproxTokens, toolCallCount?, toolErrorCount?, compactionCount?, diagnosisCount,
topDiagnosisSeverity`.

- Period semantics match `/top-sessions`: `period` selects sessions *active in it* and supplies
  the share denominator; `totalCost` is the conversation's **lifetime** cost.
- **`costKnown=false` means unrecorded, not free.** 20% of session rows fleet-wide have never
  had a cost aggregate written. They are listed, trail under cost-sort, and are counted in
  `unknownCostCount` so a total over the list is read as a floor.
- Sorts: `cost` (known first, desc; unknown trailing by recency), `recent`, `context`, `messages`.
- Reads the base table (`PK=USER#<id>`, `SK begins_with S#`) — the same bounded query
  `/top-sessions` fans out over. No index.

### `GET /admin/costs/sessions/{sessionId}/profile`

`SessionProfile { sessionId, userId, session, callCount, peakContextTokens,
compactionThreshold, writeReadRatio, attachments {count, totalBytes, byMime}, contextTrajectory[]
{callIndex, timestamp, contextTokens, cacheStatus, modelId, cost, toolCalls?}, modelMix,
fingerprintChanges {systemPrompt, toolConfig, explainedByAgentSwitch}, toolCensus {name: {calls,
errors}}, enabledToolIds, diagnoses[], dataCoverage {toolCensus, compactionCount, fingerprints,
cost} }`.

- Resolves the session row by session id through `SessionLookupIndex` (`GSI_SK = META`) — an
  admin holds the session id, not the owner's user id.
- `contextTrajectory[].contextTokens` is the disjoint three-bucket sum
  (`inputTokens + cacheReadInputTokens + cacheWriteInputTokens`) — true context occupancy.
- `dataCoverage` says which optional signals this session *has*; a session written before a
  counter shipped reads "not tracked", never "0".
- 404 when the session has no metadata row.

### `GET /admin/costs/top-users` (existing, enriched)

`email`, `tierName`, `quotaLimit`, `quotaPercentage` are now populated (previously hard-coded
`null`). Best-effort with bounded concurrency: a fork without the users table, an unlimited
tier, or one failing lookup each degrade to `null` on that row.

## 4. Diagnosis rules (`admin/costs/diagnoses.py`)

Pure functions `ProfileFacts → Optional[Diagnosis]`; `Diagnosis = {code, severity, headline,
evidence, suggestion, ref}`. Evidence is the numbers the rule compared and the threshold it
compared them to. Thresholds are imported from where they live (the runtime's compaction
threshold via the same env resolution as `CompactionConfig.from_env`; the shipped
`partial_miss` classifier's write:read ratio); values that exist only as a proposal say so.

| code | severity | fires when | ref |
|---|---|---|---|
| `PREFIX_SPIRAL` | high | peak context > threshold **and** cacheWrite:cacheRead > 3 (or writes with no reads) | spiral spec §1 |
| `PARTIAL_MISS_HEAVY` | high | `partialMissUsd ≥ 50%` of known cost | spiral spec D1 |
| `OVER_COMPACTION_THRESHOLD` | warn | peak context > `AGENTCORE_MEMORY_COMPACTION_TOKEN_THRESHOLD` (100k) | spiral spec |
| `SUMMARY_OVER_BUDGET` | warn | `summaryApproxTokens > 8 000` (PR-2's proposed budget) | spiral spec D2 |
| `SYSTEM_PROMPT_MUTATED` | warn | > 1 distinct `systemPromptHash` among non-agent-switched calls | spiral spec D4 |
| `TOOLCONFIG_MUTATED` | warn | > 1 distinct `toolConfigHash` among non-agent-switched calls | fleet one-pager |
| `ATTACHMENT_HEAVY` | warn | ≥ 5 uploads or ≥ 5 MB | roadmap |
| `TOOL_ERROR_RATE` | warn | ≥ 25% of ≥ 4 tool calls failed (needs PR-3 census) | roadmap |
| `COST_UNKNOWN` | info | no cost aggregate on the row | fleet one-pager |
| `ANCHOR_MISMATCH` | info | `truncationAnchor ≠ checkpoint` | spiral spec D3 |
| `AGENT_SWITCH_CHURN` | info | ≥ 3 `@`-mention switches | fleet one-pager |
| `AGENT_CACHE_BYPASS` | info | enabled ids ∩ `INJECTED_TOOL_IDS` − `KEY_DESCRIBED_INJECTED_TOOL_IDS` ≠ ∅ | bypass spec |
| `LARGE_TOOLSET` | info | ≥ 8 enabled catalog ids | roadmap |
| `DOMINANT_SESSION` | info | ≥ 50% of the user's period spend | spiral spec |
| `TOOL_HEAVY` | info | ≥ 40 tool calls (needs PR-3 census) | roadmap |

The list view diagnoses on the session row's rollups; the profile refines with per-call data
(true peak, distinct hashes, attachments, census) and its findings are the authoritative ones.

## 5. Decisions

- **Titles stay on Most Expensive Conversations.** The new list and profile are content-free; the
  one exemption is named in the test so it cannot widen silently.
- **Section scope is `admin.costs`, not `admin.users`.** It is cost data and its drill-down
  target is `admin.costs`-gated. The SPA renders the section on the user page only for admins
  holding that scope; a users-only delegate sees the page without it rather than a 403 inside it.
- **No new index.** `get_user_session_costs`'s base-table query already served the need; a
  content-free sibling (`get_user_session_diagnostics`) shares its body.
- **Read endpoints carry no feature flag.** Admin-only, read-only, degrade to "not tracked". Only
  PR-3's writers get a kill switch.
- **The tool census will live on the `C#` row per model call** (PR-3), not as a map on `S#`:
  one write that already happens, no parent-map `ADD` problem, and the trajectory gets "which
  tools ran on which call" for free.

## 6. Verification

- Backend: `cd backend && uv run python -m pytest tests/apis/app_api/admin/costs/ tests/costs/test_content_free_projections.py tests/architecture/`.
- Live, against dev data, without touching Phil's `:8000`: `.claude/launch.json` defines
  `app-api-branch` (uvicorn on `:8010`, cwd `backend/src`). The signed-in in-app browser's BFF
  cookie is host-scoped, so `http://localhost:8010/admin/costs/users/<id>/sessions` authenticates
  as-is. Verified 2026-09-13: 93 sessions listed for September's top user, zero content-bearing
  keys, shares computed; a 13-call profile with trajectory, coverage flags and two diagnoses;
  top-users rows carrying email, tier and quota share (51.2% for the top user).

## 7. Follow-ups (not in this epic)

- "Users who hit their quota this period" — needs a scan or an index on quota events.
- Fleet-level diagnosis rollup (which rule fires most, weighted by dollars) — the real
  cost-effectiveness backlog view; trivial once rules exist, needs a scan or a nightly job.
- Per-sub-tool token cost / a tools-page "enabled tools = tokens" meter — decide from the
  `LARGE_TOOLSET` and `AGENT_CACHE_BYPASS` frequencies this ships.
