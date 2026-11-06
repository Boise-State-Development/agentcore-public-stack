# Release ledger — cost and quality by version

**Status:** Written 2026-11-06. PR-1 (provenance) BUILT the same day: the build args, `VERSION` in every image's hash manifest, `apis/shared/build_info.py`, the stamps on both cost-row writers, both EMF emitters, the `turn_prelude` / `turn_first_token` lines, the turn-timing report (`spaVersion`) and the eval run's `meta.json`, and the `LEDGER#DEPLOY` marker (`apis/shared/costs/deploy_marker.py`). PR-2 (weight) BUILT the same day: `resultChars` / `inputChars` / `durationMs` (+ `timedCalls`) per tool in the census (`ToolCensusHook`), `outcome` and `emptyAnswer` on every turn's `C#` rows (`StreamCoordinator`), both in `CALL_ROW_PROJECTION` and the cost anatomy (a "Tools by payload" table and the call detail), and the committed per-tool spec tokens (`backend/evals/tool_spec_tokens.json`, generator `backend/scripts/measure_tool_spec_tokens.py`, staleness test `tests/test_tool_spec_tokens.py`). §9 is the build order; §10 holds the answers to the open questions.
**Refs:** `docs/one-pagers/cost-effectiveness-roadmap.md` (the arc this instruments; its north-star "unexplained-waste share" becomes a per-version number here), `docs/specs/admin-cost-drilldown.md` (the content-free contract every reader below inherits), `docs/specs/eval-harness.md` (§8 report shape, §12 phase 3 "admin readout" — this spec is where that readout lands), `docs/specs/response-feedback.md` §7 and §9 (fleet attribution and the `n` floor), `docs/specs/turn-path-ttft.md` §6E (the three latency instruments), `docs/specs/platform-cost-visibility.md` (the `system-cost-rollup` table and the "scheduled, never live" rule), `docs/one-pagers/conversation-usage-patterns.md` ("Data to collect next": two of its six items ship here), `docs/testing/smoke-regression.md`.

## 1. Problem

Every model call writes a `C#` row with its cost, token buckets, cache status, waste in dollars, the hashes that explain a miss, the tools it called, the agent and project it ran under, and the time to its first token. Thumbs land beside them as `F#` rows, provenance verdicts as `SRCV#`. The eval harness scores a golden set against a committed baseline. The smoke matrix proves the wire. Three CloudWatch dashboards hold cache, latency and platform health. Cost Explorer is synced nightly. By the standard of most agent platforms this is a lot of instrumentation.

It cannot answer the question that decides whether a release was safe: **did this version make the harness more expensive or worse, for the same work, than the one before it?**

Four reasons, each a gap rather than a bug:

1. **Nothing in the ledger knows what version produced it.** The Dockerfiles declare `ARG APP_VERSION=unknown`, nothing under `scripts/build/` or `.github/` passes it, and the image tag is a content hash that never reaches the container. So a `C#` row, a `turn_prelude` line and an EMF record are all dated but unversioned, and "cost per turn, 1.32.0 vs 1.33.0" is a question of looking up deploy times by hand and slicing by date, which the admin API cannot do and nobody does.
2. **Every readout is a different place.** Cost is `/admin/costs` and DynamoDB rollups; cache waste is the PromptCache dashboard; TTFT is the TurnLatency dashboard and an hourly probe; thumbs are `/admin/feedback`; eval results are a GitHub Actions artifact that expires; smoke results are another; usage patterns are a claude.ai artifact refreshed by hand. A release question is a tour of seven surfaces, and the answer is assembled in someone's head, which is the one place Claude cannot read.
3. **Tools are counted, not weighed.** `ToolCensusHook` records calls and errors per tool. It does not record how much each tool's result put into the context, so "which tools consume the most tokens" is inferred from context deltas (the usage-patterns skill's own caveat) and the Cost Patterns dashboard's tool panel is reliability, not tokens.
4. **The rollups are by day and model only.** `ROLLUP#DAILY`, `ROLLUP#MONTHLY` and `ROLLUP#MODEL` carry cost and four token buckets. No rollup carries cache status, waste, tools, agents, projects, document turns, compactions or TTFT, so every one of those is a scan of `C#` rows when asked — the usage-patterns skill reads 200 MB for 30 days.

The cost of these gaps is not hypothetical. The Strands 1.63.0 bump (PR 1850) was judged by running the smoke matrix twice and comparing by eye; the earlier 1.57.2 → 1.61.1 bump's six-turn comparison "has no power to detect anything under roughly 500 ms" (turn-path-ttft §6E). The Nova Micro summariser regression was found by a purpose-built harness a week later. The idle-reaper regression (#827) was found in the bill. Each was found; none was found by the thing that should have found it, which is a readout that every release gets for free.

## 2. Thesis

> **Stamp the facts we already write with the version that wrote them, roll them up where the rollups already are, and read them from one place.** The `C#` row is already a per-call fact table with a 365-day life, a content policy and an admin reader. It is missing one dimension (version) and one measure (tool payload size). Add those, bump a version-keyed rollup on the same fire-and-forget path that bumps `ROLLUP#DAILY`, publish the eval and smoke results into the same table, and one admin endpoint can answer the release question for a page and for Claude alike.

Three things follow from choosing this over the alternatives (§4 D1–D3):

- **No new infrastructure.** No table, no GSI, no Lambda, no OpenTelemetry pipeline. The stack sits at 432 of 500 CloudFormation resources on dev; the one IAM change is a statement on app-api's existing role.
- **Nothing on the TTFT path.** Every write happens after the stream closes, in `_update_system_rollups_async`, which already runs there. The one hook change measures the length of a string it already holds.
- **Claude reads what the page reads.** The readout endpoint returns a JSON bundle; the SPA renders it and a script saves it. There is no second data model for "the AI view".

## 3. What exists, by surface (so nothing here is rebuilt)

| Fact | Where it is today | Keyed by | Gap this spec closes |
|---|---|---|---|
| Per-call cost, tokens, `cacheStatus`, `wastedUsd`, `prefixFingerprints`, `toolCalls` (calls/errors), `turnAgentId`, `projectId`, `callKind`, `documentReads`, `compactionEvents`, `turnDurationMs`, first-token ms | `C#` rows, `sessions-metadata` (365-day TTL) | user, session, timestamp | no version; no tool payload size; no turn outcome |
| Daily / monthly / per-model totals | `ROLLUP#DAILY` / `#MONTHLY` / `#MODEL`, `system-cost-rollup` | date, model | no version; no cache, waste, tool, agent, project, document or latency counters |
| Project spend | project rollup (`_update_project_rollup_async`) | project | no version |
| Cache efficiency, misses, waste (sums, per call) | `AgentCoreStack/PromptCache` EMF + dashboard | time | no version property |
| Server TTFT and prelude stages (percentiles) | `AgentCoreStack/TurnLatency` EMF + dashboard; `turn_prelude` log line | time, `streamKind` | no version property |
| Client TTFT | `ClientFirstTokenMs` EMF via `POST /telemetry/turn-timing` | time, `modelId` | no SPA version property |
| Probe TTFT | `TtftProbe*Ms` hourly | time | fine as is; joined at read time |
| Thumbs, reasons, retry-with-correction | `F#` rows; `FEEDBACK#down`/`#up` fleet partitions; `/admin/feedback/fleet` arms (model, agent switch, compaction distance, document class) | time | no version arm |
| Provenance verdicts | `SRCV#` rows | session, tool use | no fleet counter |
| Eval harness run and gate | `meta.json` + `results.jsonl` on the runner; GitHub artifact (30 days) | model, `gitCommit`, `imageTags` | not persisted anywhere queryable; no version |
| Smoke matrix pass/fail, first-token line | GitHub artifact (30 days) | run | same |
| AWS bill by service | `PLATFORM#MONTHLY` / `#SERVICE#` via Cost Explorer sync | month, service | fine as is; joined at read time |
| Usage patterns (what users were doing) | `/usage-patterns-dashboard` skill, claude.ai artifact | run | stays a skill (§4 D9); its scan can use the ledger later |

## 4. Decisions

**D1 — The version is stamped on the row, never inferred from the date.** Every `C#` row, `turn_prelude` line, EMF record and turn-timing report carries `appVersion` (the `VERSION` file, e.g. `1.33.0`) and `build` (the 16-character image content hash). A deploy day has rows from both versions and the ledger keys on both, so no row is ever attributed to the wrong release by a boundary guess. `build` matters because on dev the version is constant between releases while every merge to `develop` is a new deploy; there, `build` is the deploy identity and `appVersion` is a label. On prod both move together at a release.

**D2 — The version is baked into the image, not sent as an environment variable.** `scripts/build/build-one.sh` already computes the content hash before the build; it passes `--build-arg APP_VERSION=$(cat VERSION) --build-arg APP_BUILD=$TAG`, and the Dockerfiles' existing `ARG APP_VERSION` gains `ARG APP_BUILD` beside it. `VERSION` joins each image's hash manifest (`--manifest VERSION`), so a release that bumps only the version still rebuilds; `APP_BUILD` is derived from the hash so it cannot change the hash. Cost on the AgentCore Runtime's 2,560-byte environment budget: zero, which is the reason not to use a Runtime variable for something every row needs. `/health` and `/ping` start reporting the real version as a side effect.

**D3 — The ledger is DynamoDB rollups bumped per call, not a log pipeline and not a nightly scan.** Three options were weighed:

| Option | Rejected or chosen | Why |
|---|---|---|
| OpenTelemetry spans / CloudWatch Logs as the system of record | **Rejected** | The Runtime redacts span content and runs with `AWS_AGENTIC_INSTRUMENTATION=disabled` by design; Logs Insights is paid per GB scanned, sampled under load, and minutes stale; and it would duplicate every field the `C#` row already holds. OTEL stays what it is: traces for debugging one turn |
| A nightly job that scans the day's `C#` rows and writes derived rollups | **Rejected for the ledger, kept as the backfill** | There is no fleet-wide date partition on `C#` rows (`UserTimestampIndex` is keyed `USER#…` on them) and this spec adds no GSI, so "the day's rows" is a filtered scan of the whole table, growing with the 365-day retention. A one-off backfill (§7) is exactly that scan, run once per environment |
| Version-keyed `ADD` rollups bumped beside `ROLLUP#DAILY` at turn end | **Chosen** | Zero infrastructure, zero read cost at query time, the same code path and failure semantics the existing rollups have had since the beginning, and a 90-day page read is at most a few hundred small items |

The trade is that an `ADD` rollup is not re-derivable: a counter added later starts at zero from that deploy. That is accepted, and §7's backfill exists so that *this* rollout does not start at zero.

**D4 — Tool weight is measured in characters at the hook, priced at the row.** `ToolCensusHook` gains `resultChars` and `inputChars` per tool (the `len()` of the text and JSON blocks in the tool result and of the serialised input — lengths, never contents; an image block counts a fixed 1,500 characters so it is not invisible) and `durationMs` (Strands' own measured figure, which `AgentStatusHook` already reads). The coordinator prices the result at `chars / 4 × the call's model input rate` into `estInputUsd` on the tool's ledger row. The estimate is labelled an estimate everywhere it is shown; the exact per-tool token count does not exist, because Bedrock reports usage per call, not per block. Tool *spec* cost (the schema in `toolConfig`) is a separate, cached, per-turn constant measured by the CountTokens script and is reported beside the payload figure, never added to it.

**D5 — Comparisons are between equal windows on the same model, never between raw totals.** A version's "cost per turn" moves with the model mix, the user mix and the day of the week before it moves with the code. The readout (§6.3) compares the last *N* full days of the baseline version with the first *N* full days of the candidate (default 7, capped by what each has), per model, and prints a **mix-shift warning** whenever the share of any model, or the active-user count, differs by more than 20% between the windows. A number without its window and its `n` does not leave the endpoint, which is the feedback spec's §9 rule applied to cost.

**D6 — Quality is a panel of signals, each with its own floor, and no composite score.** Per version the ledger exposes: the eval gate (per family: `n`, baseline, run, flips, McNemar `p`, pass/fail — the §8 report row, persisted), the smoke matrix (pass/fail/skip), the down-thumb rate as a fleet arm with `n ≥ 20` or `null`, retry-with-correction count, tool error rate per tool, empty-answer turns (a completed turn whose assistant message carries no text — the failure the eval harness found on dev 2026-10-30), provenance `quote_unsupported` share, forced compactions, and interrupted-turn share. A composite would hide which one moved, and which one moved is the diagnosis.

**D7 — Content-free, by the existing contract.** Ledger rows and every response carry ids (tool, model, agent, project, version, build) and numbers. No user id, session id, title, prompt, answer, tool argument or tool result. The content-policy test (`apis/shared/observability/content_policy.py`) walks the new response models and the ledger reader's projection like it walks the profile's. Agent and project ids appear exactly as the session profile already shows them.

**D8 — Feature-flagged, in development, off by default.** `RELEASE_LEDGER_ENABLED` / `CDK_RELEASE_LEDGER_ENABLED` / `features.releaseLedger`: app-api (the API) and the AgentCore Runtime (the rollup writes; copied to the Runtime only while on). The deploy marker is not behind it: one conditional put per build is the thing the backfill's version map needs from the first deploy, and it costs nothing after that. Only `"true"` enables; on in the `development` GitHub environment and `environment.development.ts`. The row stamps of D1 and the census fields of D4 are not behind it: they are attributes on rows that already exist, cost nothing to carry, and ride the switches those rows already have (`COST_DIAGNOSTICS_ENABLED` for the census). A **feature switch** once finished, because the rollup writes are a per-call cost a deployment may decline.

**D9 — The usage-patterns skill stays a skill.** It answers "what were users doing" from a classification that changes as patterns are discovered; that is research, not a ledger. Its 30-day scan can read `LEDGER#` rows for the fleet totals it currently derives, which is a follow-up, not a dependency.

**D10 — One readout for the page and for Claude.** `GET /admin/costs/ledger/readout` is the only place the comparison logic lives. The SPA tab renders its response; `scripts/release_readout.py` writes the same response to disk as JSON and a Markdown table; the `/release-readout` skill runs the script and reads the JSON with the interpretation rules of §6.4. There is deliberately no second computation of a delta anywhere, so the page and the AI can never disagree about a number.

## 5. The data

### 5.1 Stamps on existing rows (D1)

| Row or record | New fields | Written by |
|---|---|---|
| `C#` cost row | `appVersion`, `build` | `_store_message_metadata` reads `APP_VERSION` / `APP_BUILD` once at import |
| `C#` row `toolCalls[<tool>]` | `resultChars`, `inputChars`, `durationMs` beside `calls`, `errors`; `timedCalls` (how many calls reported a duration) beside `durationMs`, so a mean never divides by calls that had no timing (a call cancelled by a `BeforeToolCall` hook has none) | `ToolCensusHook` (D4) |
| `C#` cost row | `outcome`: `completed` / `paused` / `error` / `stopped` / `interrupted`, on every row of the turn; `emptyAnswer: true` on the **last** row of a `completed` turn whose final model call streamed no text (whitespace counts as none) | the coordinator: the arm the turn ended in (success, a failure arm, the cooperative Stop arm, the disconnect arm), the interrupt state for `paused`, and whether each call streamed text, recorded on its slot at `messageStop` |

Two corrections to the first draft of this row, found while building it. **`paused` is a fifth outcome.** A turn that stops on an interrupt for the user (OAuth consent, an approval, `ask_user_question`, a browser sign-in) reaches the success path and ends on the tool use the user is answering, so its final call has no text by design; without its own value every pause would read as an empty answer. `paused` is not counted in the interrupted share. **`emptyAnswer` is about the final call, not the turn's text.** The dev failure of 2026-10-30 *had* text — "Now let me verify:", a tool preamble — and no answer after it; a turn-level text length would have called it answered. The eval harness's own definition (`apis/shared/evals/score.py`: the final message is blank) is the one used. And `interrupted` means torn down by the connection closing, which includes a Stop whose cooperative signal lost the race with the disconnect, so `stopped + interrupted` is the reliable "user cut it off" figure, not `stopped` alone.
| `turn_prelude` log line and `TurnLatency` EMF | `appVersion`, `build` properties | `TurnTiming.emit` |
| `PromptCache` EMF | `appVersion`, `build` properties | `emit_prompt_cache_metrics` |
| `POST /telemetry/turn-timing` | `appVersion` (the SPA's `VERSION`), stored as an EMF property | `TurnTimingService` |
| `meta.json` of an eval run | `appVersion`, `build` read from the target's `/health` (`run --base-url`) or `/ping` (runtime-direct) | `eval_harness.py` |

Properties on EMF records are Logs Insights dimensions for free (the existing dashboards use the same mechanism for `modelId`), so "TTFT p50 by version" is one query with no metric-dimension cost.

### 5.2 Ledger rows (`system-cost-rollup` table, no TTL)

All counters are `ADD`ed; all are integers or `Decimal` dollars. `<v>` is `appVersion`, `<b>` is `build`, `<date>` is UTC `YYYY-MM-DD`, `<month>` is `YYYY-MM`.

| PK | SK | Counters |
|---|---|---|
| `LEDGER#VERSION` | `<date>#<v>#<b>` | `calls`, `turns` (calls with `call_index == 0`), `cost`, `inputTokens`, `outputTokens`, `cacheReadTokens`, `cacheWriteTokens`, `wastedUsd`, `partialMissCount`, `avoidableMissCount`, `agentSwitchMissCount`, `modelSwitchMissCount`, `uncachedCalls`, `estimatedCalls`, `unmeteredCalls`, `toolCalls`, `toolErrors`, `toolResultChars`, `documentTurns`, `documentReads`, `documentTokens`, `compactionEvents`, `forcedCompactions`, `firstTokenMsSum`, `firstTokenCount`, `turnDurationMsSum`, `emptyAnswerTurns`, `stoppedTurns`, `errorTurns`, `interruptedTurns`, `provenanceClaims`, `provenanceUnsupported`, `callKind.<kind>` (chat, resume, handoff_brief, api_converse, voice, scheduled) |
| `LEDGER#MODEL#<month>` | `<date>#<v>#<b>#<modelId>` | `calls`, `turns`, `cost`, the four token buckets, `wastedUsd`, `partialMissCount`, `firstTokenMsSum`, `firstTokenCount` |
| `LEDGER#TOOL#<month>` | `<date>#<v>#<b>#<toolId>` | `calls`, `errors`, `awaitingUser`, `resultChars`, `inputChars`, `durationMsSum`, `estInputUsd`, `callsWithUi` |
| `LEDGER#AGENT#<month>` | `<date>#<v>#<b>#<agentId>` | `calls`, `turns`, `cost`, `wastedUsd`, `toolCalls`, `toolErrors`, `agentSwitched` |
| `LEDGER#PROJECT#<month>` | `<date>#<v>#<b>#<projectId>` | `calls`, `turns`, `cost`, `wastedUsd`, `documentReads`, `kbQueries` |
| `LEDGER#DEPLOY` | `<service>#<build>` | `service`, `appVersion`, `build`, `firstSeenAt` — a conditional put, so the second task of a build and every restart after it write nothing. app-api writes it in a thread after startup; the Runtime's first `LEDGER#VERSION` row for a `(v, b)` pair is its marker, since it starts a microVM per session |
| `LEDGER#EVAL` | `<startedAt>` | `appVersion`, `build`, `gitCommit`, `modelId`, `k`, `target`, `families: {name: {n, base, run, gained, lost, p, gate}}`, `judgeMeans`, `costUsd`, `durationS` — the §8 table, nothing from any answer |
| `LEDGER#SMOKE` | `<startedAt>` | `appVersion`, `build`, `pass`, `fail`, `skip`, `firstTokenMsMedian`, `secondTurnCacheRead`, `secondTurnCacheWrite` |

Per call the writer issues one `VERSION` update, one `MODEL` update, one per distinct tool (the census cap is 64), one `AGENT` update when the call ran under an agent, one `PROJECT` update when under a project: typically three to five writes, in `asyncio.gather`, fire-and-forget beside the existing `ROLLUP#` bumps, swallowing and logging failures exactly as those do. At prod volume (about 2,200 calls a day) that is under 10k writes a day, or about $0.01.

`kbQueries` on the project row is the count of the project's KB-backed tool calls (`search_project_files`, `document_read` with a query) from the census; managed KB retrieval bills per query (`managed-kb-cost-attribution.md`), so the readout prices it at the published rate rather than reading Cost Explorer per project.

### 5.3 What is deliberately not stored

- **Distinct users per version.** `ADD` cannot count distinct; `ROLLUP#DAILY.activeUsers` is joined by date at read time, and a deploy day's users are attributed to both versions with the overlap noted.
- **Percentiles.** DynamoDB holds sums and counts (means); p50/p90 of TTFT come from CloudWatch at read time (§6.1).
- **Thumbs per version.** `F#` rows are joined to `C#` rows by `(sessionId, messageId)` at read time, the way `/admin/feedback/fleet` already does for its four dimensions; version becomes its fifth arm. Capped and reported as `coverage.truncated` like the others.
- **The history carry of a tool result** (how much a result costs every later re-write of the prefix). A real number, derivable from `resultChars` and the session's later `cacheWriteInputTokens`, and a follow-up: the first version of the tools table reports the payload at intake and says so.

## 6. The readers

### 6.1 Admin API (`admin.costs` scope, `require_costs_admin`)

| Route | Returns |
|---|---|
| `GET /admin/costs/ledger/versions?from&to` | The version timeline for the range: each `(appVersion, build)` with its first and last date, days observed, deploy markers, totals and the derived KPIs of §6.2 |
| `GET /admin/costs/ledger/series?from&to&by=version\|model\|tool\|agent\|project` | Daily rows for a chart, one series per key, with the version boundaries for the chart's bands |
| `GET /admin/costs/ledger/tools?from&to&version?` | The tool table: calls, error rate, awaiting-user share, mean and total `resultChars`, estimated input tokens and dollars, mean latency, and the spec tokens from the committed CountTokens measurement where one exists |
| `GET /admin/costs/ledger/agents?…`, `/projects?…` | The same shape over agents and projects |
| `GET /admin/costs/ledger/quality?from&to` | Per version: eval rows, smoke rows, thumbs arm (with `n`), retry count, empty-answer share, provenance share, forced compactions, outcome shares |
| `GET /admin/costs/ledger/readout?version&baseline?&days=7` | The comparison bundle of §6.3 |
| `GET /admin/costs/ledger/export?from&to` | NDJSON of the raw ledger rows for the range, for offline work |

TTFT percentiles come from one `cloudwatch:GetMetricData` call per request over `AgentCoreStack/TurnLatency` and the client and probe metrics, cached for an hour in the existing config-cache pattern; the IAM statement is added to app-api's role (no new resource). Everything else is DynamoDB queries: `LEDGER#VERSION` is one range query; the month-partitioned families are one query per month in range.

### 6.2 Derived KPIs (computed in one place, `apis/shared/costs/ledger_kpis.py`, pure)

| KPI | Definition | Reads |
|---|---|---|
| Cost per turn | `cost / turns` | the headline, always shown per model too |
| Cache efficiency | `cacheRead / (cacheRead + cacheWrite)` | the roadmap's write:read ratio |
| Unexplained-waste share | `(wastedUsd − agentSwitchUsd − modelSwitchUsd) / cost` | the roadmap's north star, now per version |
| Partial-miss share of calls | `partialMissCount / calls` | the §833 instrument |
| Tool error rate | `toolErrors / toolCalls` | per tool in the tools table |
| Tool payload per call | `toolResultChars / toolCalls` | the D4 measure |
| Document turn share | `documentTurns / turns` | the attachments cohort |
| Mean server TTFT | `firstTokenMsSum / firstTokenCount` | with p50/p90 beside it from CloudWatch |
| Empty-answer share | `emptyAnswerTurns / turns` | D6 |
| Interrupted share | `(stopped + error + interrupted) / turns` (`paused` is not interrupted) | D6 |
| Forced-compaction rate | `forcedCompactions / compactionEvents` | the `floor_unreachable` tell |

### 6.3 The readout bundle

`readout` picks the candidate version (default: the newest with at least one full day), the baseline (default: the previous version on the same environment), the two windows (D5), and returns:

```json
{
  "candidate": {"appVersion": "1.33.0", "build": "8f676bb2e1a4c0d2", "window": ["2026-11-02", "2026-11-08"], "days": 7},
  "baseline":  {"appVersion": "1.32.0", "build": "34833fff4b7c…",   "window": ["2026-10-25", "2026-10-31"], "days": 7},
  "mixShift": {"warn": true, "models": {"us.anthropic.claude-haiku-5-5…": [0.61, 0.44]}, "activeUsers": [412, 398]},
  "kpis": {"costPerTurn": {"baseline": 0.0312, "candidate": 0.0287, "delta": -0.08, "byModel": {…}}, "...": "…"},
  "tools": [{"toolId": "browse_web", "calls": [1203, 1288], "errorRate": [0.04, 0.09], "resultCharsPerCall": [6100, 6050], "estInputUsd": [3.21, 3.44]}],
  "agents": [...], "projects": [...],
  "latency": {"serverFirstTokenMs": {"p50": [1810, 1790], "p90": [4100, 4650]}, "clientFirstTokenMs": {…}, "probe": {…}},
  "quality": {"eval": {...the LEDGER#EVAL row for each...}, "smoke": {...}, "thumbs": {"downRate": [0.021, null], "n": [310, 14]}, "emptyAnswerShare": [0.0, 0.004], "...": "…"},
  "platform": {"month": "2026-11", "inferenceCost": …, "platformCost": …},
  "caveats": ["candidate window is 4 days, not 7", "thumbs arm under the floor (n=14)", "estimatedCalls: 31 rows carry attributed, not measured, usage"]
}
```

Every pair is `[baseline, candidate]`; every `delta` is relative; `null` is a value below its floor, never a zero. `caveats` is generated, not free text: each line is one of a fixed set of conditions the endpoint detects (short window, mix shift, floor, estimated rows, a deploy inside the window, a model with no catalog row), because the reader of this bundle may be a page, a script or a model, and all three need the same warnings in the same words.

### 6.4 The Releases tab

A sixth tab on `/admin/costs` (`CostTab` gains `'releases'`), gated by `features.releaseLedger`, rendered from `versions`, `series`, `tools` and `readout`:

1. **Version strip.** One chip per `(version, build)` in the period with its days and deploy markers; on dev, builds are the unit and the version is the label.
2. **Compare header.** Candidate vs baseline, the windows, the mix-shift warning in amber when set, and the caveats as a list, before any number.
3. **KPI cards.** The §6.2 KPIs as `baseline → candidate (Δ)`, cost per turn first and broken out per model under it, every rate with its `n`.
4. **Trend chart.** The daily series with shaded version bands, selectable by KPI; the same `CostTrendsChart` component with bands added.
5. **Tools table.** Sorted by estimated input dollars; error rate, payload per call and latency as deltas; the spec-token column from the committed measurement.
6. **Agents and projects tables.** Same shape, cost and waste per turn, top twenty.
7. **Quality panel.** The eval table for each version (the §8 table, verbatim), smoke pass/fail, the thumbs arm, empty answers, interrupted share, provenance share, forced compactions.
8. **Export.** The readout JSON and the NDJSON range, as downloads.

No fleet-wide quality score appears anywhere on the tab (D6). A delta whose `n` is under floor renders as "—" with the `n`, never as a number.

### 6.5 Claude's side

- `backend/scripts/release_readout.py --profile <p> --prefix <prefix> [--version --baseline --days]` reads the same DynamoDB rows and CloudWatch metrics the endpoint reads, through the same `ledger_kpis` module, and writes `readout.json` and `readout.md` to a directory. It exists so the readout works from a terminal with an AWS profile and no sign-in, the way the usage-patterns skill does; it never reads a `C#` row directly.
- `.claude/skills/release-readout/` runs the script and interprets the bundle with the traps written down: a mix shift is read before any delta; `agentSwitched` and `modelSwitched` misses are explained, not waste; an `estimated` row is attribution, not measurement; a thumbs arm under floor says nothing; a cost delta on a deploy day is a boundary, not a change; a TTFT delta under the probe's daily-median noise is "no evidence", not "unchanged". It ends with a recommendation in one of three words — ship, watch, investigate — and names the KPI that decided it.
- The `cutting-a-release` skill gains one step: run the readout for the *outgoing* release and paste its KPI table into the release PR, so each release PR carries the previous release's verdict and a reviewer can see a trend before the next one ships.

## 7. Backfill

`backend/scripts/release_ledger_backfill.py --prefix --from --to --version-map versions.json [--yes]` does the one scan this spec otherwise avoids: `C#` rows in the range through the content-free projection, attributed to a version by timestamp against a map of deploy times, then written as ledger rows with the same `ADD` semantics (so it is idempotent over a range already partly live, provided `--from` is before the first stamped row — the script refuses a range that overlaps stamped rows). The map is built from `gh run list --workflow backend.yml --branch main` completion times and the `VERSION` at each commit; on dev, from `--branch develop` and the content hash in each run's summary. The two census fields that did not exist (`resultChars`, `durationMs`) backfill as absent, and the tools table marks the pre-rollout range "payload not measured".

Prod has about 300 days of rows under the TTL, so the Releases tab has a trend across roughly ten releases on the day it ships, which is the point of doing the scan once.

## 8. Guards

- **TTFT:** no new work before the first token. The census change is a `len()` on strings already in memory at `AfterToolCallEvent`; the rollup writes are in `_update_system_rollups_async`, after the stream. `tests/agents/main_agent/test_turn_path_invariants` (or its equivalent) asserts no new `Before*` hook work.
- **Prompt cache:** nothing here reaches the prompt. The row stamps and census fields are metadata; the test that pins the prefix fingerprints across a turn is unchanged.
- **Content:** `test_content_policy` walks the new response models and the ledger projection; a tool argument, a title or a session id in any of them fails the suite.
- **Runtime budget:** `RELEASE_LEDGER_ENABLED` is written to the Runtime only while on; the version stamp is baked, so the `RuntimeEnvironmentPayloadGuard` sees at most 29 bytes more, and only on an environment that opted in.
- **Resources:** zero new CloudFormation resources; the CDK test that pins the count stays green without a bump.
- **Failure:** every ledger write is best-effort and logged; a failing rollup never fails a turn, which is the contract `ROLLUP#` has always had.

## 9. Build order

Each PR lands on its own and is useful alone; later ones read what earlier ones write.

| PR | Scope | Done when |
|---|---|---|
| **1 — Provenance** | `--build-arg APP_VERSION/APP_BUILD` in `build-one.sh`, `VERSION` in each image's hash manifest, `ARG APP_BUILD` in the Dockerfiles; `appVersion` + `build` on `C#` rows, `turn_prelude`, both EMF emitters, the turn-timing report (SPA sends `VERSION`), eval `meta.json`; the `LEDGER#DEPLOY` marker on app-api startup | `/health` on dev reports the version; a dev turn's `C#` row carries both fields; Logs Insights can group `FirstTokenMs` by `appVersion` |
| **2 — Weight** | `resultChars`, `inputChars`, `durationMs` in the census; `outcome` and `emptyAnswer` on the row | the session cost anatomy shows payload per tool; the usage-patterns one-pager's "tool-result tokens per tool" item is closed |
| **3 — Ledger writes** | the flag, `LEDGER#VERSION/MODEL/TOOL/AGENT/PROJECT` bumps in `_update_system_rollups_async`, the KPI module, the backfill script run on dev | dev rows accumulate; backfilled dev range matches `ROLLUP#DAILY` totals within rounding |
| **4 — Ledger API** | the seven routes, the CloudWatch join and cache, the thumbs version arm added to `/admin/feedback/fleet`, content-policy coverage | the readout for two dev builds returns with caveats; export round-trips the backfilled range |
| **5 — Publish** | `eval_harness.py score --publish`, `smoke_turns.py --publish`, both nightly tracks publish; the eval spec's §12 phase 3 marked as landing here | a nightly run on dev writes `LEDGER#EVAL` and `LEDGER#SMOKE` and the quality route returns them |
| **6 — Tab** | the Releases tab, bands on the trend chart, the quality panel, exports | browser-verified on dev against two builds; signed-out and `admin.costs`-less users see nothing |
| **7 — Claude** | `release_readout.py`, the `/release-readout` skill, the release-skill step, the docs-site flag row, a CLAUDE.md line under the cost tenet ("verify a release against the ledger, not a smoke run") | the outgoing release's readout appears in the next release PR; a prod backfill is run once the flag is on there |

PR-1 and PR-2 are safe for prod on merge (attributes only). PR-3 onward wait on the flag.

## 10. Open questions — answered 2026-11-06

1. **Prod backfill timing.** Evenings, about 22:00 local, which is prod's quiet hour. It cannot run before PR-3 exists and PR-1's stamps have been live for at least one deploy (the script refuses a range that overlaps stamped rows, so the map must end where the stamps begin). Scheduled when PR-3 lands, not before.
2. **Thumbs under the floor.** Decided: the quality panel shows the raw up and down *counts* under the floor, labelled "below floor (n=14)", and never a rate. Thumbs are kept as an arm because the join already exists and costs nothing, but they are the last row of the panel, not the first: at prod's rate of about one thumb a day the arm will sit under the floor for most of a version's life, and the signals above it (eval gate, empty answers, tool errors, interrupted share) are the ones expected to move. If a year of versions shows the arm never reaching the floor, it comes out.
3. **Spec tokens per tool.** Decided: do it. The CountTokens script's output is committed as `backend/evals/tool_spec_tokens.json` keyed by tool id, re-run when the catalog's schemas change (a test compares the committed ids with the catalog and fails on a tool with no row), and the tools table shows the spec column from it. Lands with PR-2, since that is the PR that makes the tools table worth reading. **Landed in PR-2.** There was no committed CountTokens script to reuse (the earlier measurement was a one-off recipe), so `backend/scripts/measure_tool_spec_tokens.py` is new: it builds each seeded id's family with the turn path's own factories, counts it on the base Haiku 4.5 id as the marginal cost beside an anchor tool (so Bedrock's ~560-token tool-use preamble is in the baseline, not charged to the first tool), and records each family member alone too, keyed by the tool name the census uses. Each row carries a `specHash`, and the test fails on a stale schema as well as a missing row, since a reworded description is tokens too. First measurement, dev us-west-2, 2026-11-06: 22 seeded ids, 118 (`get_my_quota`) to 2,852 (`create_powerpoint_presentation`) tokens, about 19.2k with every family on; chars/4 underestimates them by 5–30%.
4. **Alarms per version.** Deferred until two versions of ledger data exist on prod.
