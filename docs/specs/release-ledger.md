# Release ledger — cost and quality by version

**Status:** Written 2026-11-06. PR-1 (provenance) BUILT the same day: the build args, `VERSION` in every image's hash manifest, `apis/shared/build_info.py`, the stamps on both cost-row writers, both EMF emitters, the `turn_prelude` / `turn_first_token` lines, the turn-timing report (`spaVersion`) and the eval run's `meta.json`, and the `LEDGER#DEPLOY` marker (`apis/shared/costs/deploy_marker.py`). PR-2 (weight) BUILT the same day: `resultChars` / `inputChars` / `durationMs` (+ `timedCalls`) per tool in the census (`ToolCensusHook`), `outcome` and `emptyAnswer` on every turn's `C#` rows (`StreamCoordinator`), both in `CALL_ROW_PROJECTION` and the cost anatomy (a "Tools by payload" table and the call detail), and the committed per-tool spec tokens (`backend/evals/tool_spec_tokens.json`, generator `backend/scripts/measure_tool_spec_tokens.py`, staleness test `tests/test_tool_spec_tokens.py`). PR-3 (ledger writes) BUILT the same day, behind `RELEASE_LEDGER_ENABLED` (off everywhere): the `LEDGER#VERSION/MODEL/TOOL/AGENT/PROJECT` bumps (`apis/shared/sessions/ledger_rollups.py`, called from `_update_system_rollups_async`), the KPI module (`apis/shared/costs/ledger_kpis.py`), and the backfill (`backend/scripts/release_ledger_backfill.py`), run on dev on 2026-11-07 over every `C#` row before 2026-11-07 00:00 UTC (§7.1). Building it corrected §5.2, §6.2, §7 and §10 Q1; each correction says so where it is. PR-4 (ledger API) BUILT 2026-11-07, behind the same flag: the eight `/admin/costs/ledger/*` routes (`apis/app_api/admin/costs/ledger_routes.py`), the readers they share with the PR-7 script (`apis/shared/costs/ledger_reader.py`, `ledger_readout.py`, `ledger_latency.py`, `ledger_provenance.py`), the `version` arm on `/admin/feedback/fleet`, and one IAM statement; verified on dev the same day (§6.6). Its corrections are in §6.1 and §6.3. PR-6 (the Releases tab) BUILT 2026-11-07 behind `features.releaseLedger`, verified in the browser against dev's ledger the same day (§6.4). §9 is the build order; §10 holds the answers to the open questions.
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
| `LEDGER#VERSION` | `<date>#<v>#<b>` | `calls`, `turns` (a chat turn's **last** row, see below), `cost`, `inputTokens`, `outputTokens`, `cacheReadTokens`, `cacheWriteTokens`, `wastedUsd`, `partialMissCount`, `partialMissUsd`, `avoidableMissCount`, `agentSwitchMissCount`, `agentSwitchUsd`, `modelSwitchMissCount`, `modelSwitchUsd`, `uncachedCalls`, `estimatedCalls`, `unmeteredCalls`, `toolCalls`, `toolErrors`, `toolResultChars`, `toolInputChars`, `documentTurns`, `documentReads`, `documentTokens`, `compactionEvents` (`checkpoint` events), `forcedCompactions`, `floorUnreachableCompactions`, `firstTokenMsSum`, `firstTokenCount`, `turnDurationMsSum`, `outcomeTurns`, `completedTurns`, `pausedTurns`, `stoppedTurns`, `errorTurns`, `interruptedTurns`, `emptyAnswerTurns`, `callKind.<kind>` (chat, handoff_brief, voice, api_converse, document_extract, other) |
| `LEDGER#MODEL#<month>` | `<date>#<v>#<b>#<modelId>` | `calls`, `turns`, `cost`, the four token buckets, `wastedUsd`, `partialMissCount`, `firstTokenMsSum`, `firstTokenCount` |
| `LEDGER#TOOL#<month>` | `<date>#<v>#<b>#<toolId>` | `calls`, `errors`, `resultChars`, `inputChars`, `durationMsSum`, `timedCalls`, `estInputUsd` |
| `LEDGER#AGENT#<month>` | `<date>#<v>#<b>#<agentId>` | `calls`, `turns`, `cost`, `wastedUsd`, `toolCalls`, `toolErrors`, `agentSwitched` |
| `LEDGER#PROJECT#<month>` | `<date>#<v>#<b>#<projectId>` | `calls`, `turns`, `cost`, `wastedUsd`, `toolCalls`, `documentReads` |
| `LEDGER#DEPLOY` | `<service>#<build>` | `service`, `appVersion`, `build`, `firstSeenAt` — a conditional put, so the second task of a build and every restart after it write nothing. app-api writes it in a thread after startup; the Runtime's first `LEDGER#VERSION` row for a `(v, b)` pair is its marker, since it starts a microVM per session |
| `LEDGER#EVAL` | `<startedAt>` | `appVersion`, `build`, `buildSource`, `gitCommit`, `modelId`, `k`, `target`, `families: {name: {n, base, run, gained, lost, p, gate, gating}}`, `gateFailed`, `judgeMeans: {family: {evaluator: {n, base, run}}}`, `judgeModel`, `samples`, `calls`, `costUsd`, `durationS` — the §8 table, nothing from any answer (§5.4) |
| `LEDGER#SMOKE` | `<startedAt>` | `appVersion`, `build`, `buildSource`, `gitCommit`, `target`, `pass`, `fail`, `skip`, `observed`, `rows: {row: status}`, `firstTokenMsMedian`, `firstTokenTurns`, `secondTurnCacheRead`, `secondTurnCacheWrite`, `calls`, `costUsd`, `durationS` (§5.4) |
| `LEDGER#BACKFILL` | `<from>#<to>` | `from`, `to`, `rows`, `ledgerRows`, `startedAt`, `completedAt`, `status` — one per backfill run (§7), so a second run over the same range is refused |

Every row of the five per-call families also `SET`s its labels (`date`, `appVersion`, `build`, and `modelId` / `toolId` / `agentId` / `projectId`), so a reader never parses a sort key. The live writer adds `firstLiveAt = if_not_exists(firstLiveAt, :now)` on the same write, and the backfill sets `backfilledAt`: the earliest `firstLiveAt` is where live counting began, which is where the backfill must stop.

**Corrections made while building PR-3.**

- **A turn is counted on its last row, not on `call_index == 0`.** A `C#` row does not store its call index, so that rule cannot be applied by a reader of the row. The last row is marked: it alone carries `turnDurationMs`, which `StreamCoordinator._store_turn_call_metadata` writes on the final row of every turn it meters, on the success, failure and Stop/disconnect paths alike, and it is the row `emptyAnswer` goes on. So `turns`, the outcome counters, `emptyAnswerTurns`, `documentTurns` and `turnDurationMsSum` read that row alone, and a four-call turn adds 4 to `calls` and 1 to `turns` (pinned by `test_the_ledger_counts_each_turn_once_and_every_call`, which drives real turns through the coordinator on each arm). `turnDurationMs` exists from 2026-09-19, so a backfilled day before that has calls and no turns, and its per-turn KPIs read `None`.
- **`paused` is counted** (`pausedTurns`), beside a `completedTurns` the first draft left implicit, and **`outcomeTurns`** counts turns whose row carries any outcome. It is the denominator for the outcome and empty-answer shares, because rows written before PR-2 carry no outcome and dividing by every turn would read them as clean answers.
- **Call kinds are what the row can tell.** `callKind` is set only by the handoff brief. The other writers are recognised by the ids they already write: api-converse's `api-converse-<key>` pseudo-session, voice's `voice:<n>` message id, `document_extract`'s `extract:<toolUseId>`. `resume` and `scheduled` are not on the row (a resumed turn and a scheduled run are chat turns to the coordinator), so they are not kinds; `document_extract` is one the first draft missed; an unexpected `callKind` value is bucketed `other` rather than written as an attribute name. `callKind.<kind>` is a **flat** attribute whose name contains a dot, not a nested map: `ADD` on a path under a map that does not exist yet fails.
- **`agentSwitchUsd`, `modelSwitchUsd` and `partialMissUsd` are stored**, because the unexplained-waste KPI of §6.2 subtracts the first two and the counts alone cannot. They split exactly as the session anatomy does: an agent or model switch is counted only on a `miss_avoidable` call.
- **Not on the `C#` row, so not in PR-3:** `provenanceClaims` / `provenanceUnsupported` (verdicts are `SRCV#` rows, joined at read time in PR-4 the way thumbs are), the tool row's `awaitingUser` and `callsWithUi` (the census does not record either), and the project row's `kbQueries` (no tool or row field records a project KB query: project search-ahead runs in the prelude, not as a tool call). Each is a census or row field first, then a counter.
- **`firstTokenMs*` is the model's time to first byte per call** (`latency.timeToFirstToken`), not the turn's server TTFT, which includes the prelude and comes from CloudWatch at read time (§6.1).
- **`compactionEvents` counts `checkpoint` events**, the ones that cut a new summary; `forcedCompactions` and `floorUnreachableCompactions` count their own kinds.
- **The writer lives in `apis/shared/sessions/ledger_rollups.py`**, beside the metadata writer that calls it, because `sessions/` is already copied whole into the scheduled-runs image; a module under `costs/` would have pulled that package's `__init__` into the Lambda. `TURN_OUTCOMES` moved there from the coordinator, since app-api runs the writer too and may not import `agents`.

Per call the writer issues one `VERSION` update, one `MODEL` update, one per distinct tool (the census cap is 64), one `AGENT` update when the call ran under an agent, one `PROJECT` update when under a project: typically three to five writes, each on a worker thread and all in one `asyncio.gather`, gathered in turn with the existing `ROLLUP#` bumps inside the fire-and-forget `_update_system_rollups_async` task, so neither side can skip the other, and swallowing and logging failures exactly as those do. At prod volume (about 2,200 calls a day) that is under 10k writes a day, or about $0.01.

### 5.3 What is deliberately not stored

- **Distinct users per version.** `ADD` cannot count distinct; `ROLLUP#DAILY.activeUsers` is joined by date at read time, and a deploy day's users are attributed to both versions with the overlap noted.
- **Percentiles.** DynamoDB holds sums and counts (means); p50/p90 of TTFT come from CloudWatch at read time (§6.1).
- **Thumbs per version.** `F#` rows are joined to `C#` rows by `(sessionId, messageId)` at read time, the way `/admin/feedback/fleet` already does for its four dimensions; version becomes its fifth arm. Capped and reported as `coverage.truncated` like the others.
- **The history carry of a tool result** (how much a result costs every later re-write of the prefix). A real number, derivable from `resultChars` and the session's later `cacheWriteInputTokens`, and a follow-up: the first version of the tools table reports the payload at intake and says so.

### 5.4 Quality rows (PR-5)

`backend/scripts/eval_harness.py score --publish` and `backend/scripts/smoke_turns.py --publish --prefix <p>` each write one row through `apis/shared/costs/ledger_publish.py`, which holds the row builders, the content guard and the write. Found while building it:

- **The build is the one that served the turns, read from the turns themselves.** An eval run's `meta.json` has a top-level `build` from app-api's `/health`, and app-api does not serve a turn: on dev on 2026-11-07 it read `8b13e8ba31d32a80` while every call of the run ran on the Runtime's `078ac8998e8e88d2`. Keyed by it, the row would sit under a unit that `LEDGER#VERSION` never has, and the quality route would show it nowhere. So the key comes from the run's own `C#` rows, which the Runtime stamps (D1): the `(appVersion, build)` most of its calls carry, with `otherBuildCalls` counting any that ran on another (a deploy mid-run), and `buildSource: "cost_rows"`. The rows survive the run's cleanup (a deleted session keeps its cost rows), and they are already read for `score --explain`. Only when none can be read does it fall back to the Runtime's image tag (`imageTags["inference-api"]` for an eval, `/<prefix>/inference-api/image-tag` read before the first turn for a smoke run), labelled with app-api's version (`buildSource: "image_tag"`); the tag is `APP_BUILD`, so it names the same build. With neither, nothing is published. The same reading also gives `calls` and an exact `costUsd`.
- **A publish is a `PutItem`, so a re-run is the same row.** The key is the run's `startedAt` (microseconds), which a resumed run and a re-score keep; publishing one run twice leaves one item, and a re-score after a new baseline replaces it with the newer comparison. Nothing is `ADD`ed.
- **Content-free by an allowlist, checked before the write.** `assert_content_free` refuses an attribute outside `EVAL_ROW_FIELDS` / `SMOKE_ROW_FIELDS`, any string that is not id-shaped (the ledger reader's export pattern, so a published row reads back whole: the test round-trips both rows through PR-4's `LedgerStore` and `export_row`), and any string containing one of the run's own session or user ids. A family comparison's `reason` and its flipped case ids stay out (they are prose and the cases a failure names; `score --explain` on the run directory has them), as do a smoke row's detail and warnings: each row keeps its name and status only.
- **The smoke row reads the second turn's cache tokens from the table.** The matrix reads them from the admin cost anatomy, which a non-admin runner gets 403 from, so the row falls back to the shared session's last `C#` row, the same number. `firstTokenMsMedian` is the true median of the client-clock first token over the run's turns.
- **Best-effort.** A failed write is printed and logged; neither script's exit status moves.
- **The nightly smoke that publishes runs on dev.** The e2e track's smoke matrix runs on an ephemeral stack that is torn down with its ledger, so a row written there is read by nobody. `nightly.yml`'s eval job now runs the matrix against dev as the eval user (`smoke_turns.py --auth eval-user`, credentials from the SSM parameter `seed_eval_user.py` writes) right after the eval, and both publish when the environment has `CDK_RELEASE_LEDGER_ENABLED=true`. The e2e track's smoke run is unchanged and does not publish.

Verified on dev, 2026-11-07, from the branch: a `tool_selection` eval run (9/9, gate pass) and a smoke run as the eval user (11 pass, 0 fail, 6 skip, 1 observed) each wrote one row keyed `1.33.0#078ac8998e8e88d2` from their cost rows ($0.0075 over 17 calls, $0.0055 over 20); a second `score --publish` left one item; and PR-4's quality route, run against dev's tables, returned both under that build and under `1.33.0`.

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

**Corrections made while building PR-4.**

- **Eight routes, not seven:** `agents` and `projects` are two. Every route is `require_ledger_admin`, which chains through the `admin.costs` cookie-session dependency and only then 404s while the flag is off, so a signed-out caller gets 401 and a scope-less admin 403 either way (`test_auth_is_checked_before_the_flag`). The admin-scope coverage test now walks the whole dependency tree to see the scope beneath that guard.
- **A unit is a version or a build.** Every `version` / `baseline` parameter takes `<appVersion>` (all of its builds) or `<appVersion>#<build>`; `series` and `quality` take `unit=version|build`. On dev the version is constant between releases, so a build-against-build readout is the normal case there.
- **The latency join is by time, not by stamp.** `appVersion` and `build` are EMF *properties*, which `GetMetricData` cannot group by (only Logs Insights can), so a window's percentiles are over every sample in it: one call per window, one period spanning the window, `None` under 20 samples, cached an hour per window (five minutes for a failed read). A window that contains a deploy mixes builds, and the readout says so (`latency_mixed_window`).
- **Provenance is joined the way thumbs are, gated by the ledger.** `SRCV#` rows have no fleet index and no stamp. The join reads nothing unless `LEDGER#TOOL` shows `verify_sources` calls in range; then it walks the range's top-cost users (`PeriodCostIndex`, the top-sessions fan-out), queries their `SRCV#` rows projecting each claim's `state` by list index (`PROVENANCE_STATE_PROJECTION`: never a quote, value or filename), and attributes each verdict to the cost row of its session nearest before it. Capped at 200 users and 300 sessions, reported in `coverage`.
- **An unstamped item is attributed by the ledger, only where that is unambiguous.** A thumb or verdict whose cost row predates PR-1's stamps cannot be matched by stamp, while the ledger attributed that row by the deploy-time map, which is not stored. Such an item counts for a unit only on a date when that unit is the only one in `LEDGER#VERSION`; on a shared date it is left out.

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
| Empty-answer share | `emptyAnswerTurns / outcomeTurns` | D6 |
| Interrupted share | `(stopped + error + interrupted) / outcomeTurns` (`paused` is not interrupted) | D6 |
| Forced-compaction rate | `forcedCompactions / compactionEvents` | the `floor_unreachable` tell |

Built in PR-3 (`version_kpis`, `tool_kpis`, `group_kpis`, `relative_delta`). Every KPI is `{value, n}`, `n` being the denominator. A **share** needs `n ≥ 20` (`MIN_SHARE_N`, the feedback arm's floor) and a **mean** a non-zero `n`; either way a missing denominator is `None`, never `0`. Three were added beside the table: `costPerCall`, `meanTurnDurationMs` and `pausedShare`. The outcome and empty-answer shares divide by `outcomeTurns`, not `turns` (§5.2). The tool table's mean latency divides by `timedCalls`, never `calls`.

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

Every pair is `[baseline, candidate]`; every `delta` is relative; `null` is a value below its floor, never a zero. **Built in PR-4** (`apis/shared/costs/ledger_readout.py`) with three corrections: each caveat is `{code, message}`, the code from a fixed set (`CAVEAT_CODES`: `no_baseline`, `no_full_day`, `short_window`, `deploy_in_window`, `mix_shift`, `below_floor`, `thumbs_below_floor`, `estimated_rows`, `unmetered_rows`, `commit_attributed`, `payload_not_measured`, `latency_unavailable`, `latency_mixed_window`, `coverage_truncated`), so a page can style one and a model can match one; a **full day** is a UTC date before today on which the unit was the only one of its granularity to write a row, and a unit with none (a dev build that lived an hour) falls back to its observed days with `no_full_day`; and the mix-shift test is relative (a model's call share or the mean daily active users moving by more than 20% of the larger side), ignoring models under 5% of calls in both windows. `caveats` is generated, not free text: each line is one of a fixed set of conditions the endpoint detects (short window, mix shift, floor, estimated rows, a deploy inside the window, a model with no catalog row), because the reader of this bundle may be a page, a script or a model, and all three need the same warnings in the same words.

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

**Built in PR-6** (`frontend/ai.client/src/app/admin/costs/releases/`), with four decisions the list above left open:

- **The tab computes no delta.** The readout gives the tools table pairs, not deltas, so the table prints `baseline → candidate`; a relative delta computed in the SPA would be the second computation D10 forbids.
- **The trend chart sums totals and leaves rates blank on shared days.** The series is per unit, so an additive total (cost, calls) is summed across the units on a date, while a rate on a date two builds share is a gap: a rate across builds is neither build's, and computing one would re-derive a KPI outside `ledger_kpis`.
- **A tool window whose payload was not measured reads "—"** in the tools table (from the readout's per-side `payloadMeasured`), not the `0` its counters hold. The version-level `toolPayloadPerCall` card has no such flag and still reads `0 chars` for a pre-census window; the `payload_not_measured` caveat above it is what says so.
- **The mix-shift box lists models at 5% of calls or more in either window** (the readout's own `MIX_SHIFT_MIN_SHARE`) and counts the rest; on dev fourteen models were in the mix and twelve were under 1%.

Verified on dev's ledger 2026-11-07, from this branch's app-api against dev's tables: the default version readout (1.33.0 against 1.32.0, 13 caveats, mix shift on Haiku 5.5 and Haiku 4.5) and the §6.6 build pair (`1.33.0#d5ead33e05f3e229` against `1.33.0#f200a6a3514248d3`, chosen in the strip and the baseline picker: `no_full_day` ×2, `deploy_in_window` ×2, `latency_mixed_window`, the floors, `thumbs_below_floor` ×2) render in light and dark with every visible text node at WCAG AA; the quality panel shows PR-5's published eval and smoke rows under 1.33.0. Signed out, `/admin/costs` goes to sign-in; with no `admin.costs` the route sends the user home and every ledger route answers 403.

### 6.5 Claude's side

- `backend/scripts/release_readout.py --profile <p> --prefix <prefix> [--version --baseline --days]` reads the same DynamoDB rows and CloudWatch metrics the endpoint reads, through the same `ledger_kpis` module, and writes `readout.json` and `readout.md` to a directory. It exists so the readout works from a terminal with an AWS profile and no sign-in, the way the usage-patterns skill does; it never reads a `C#` row directly.
- `.claude/skills/release-readout/` runs the script and interprets the bundle with the traps written down: a mix shift is read before any delta; `agentSwitched` and `modelSwitched` misses are explained, not waste; an `estimated` row is attribution, not measurement; a thumbs arm under floor says nothing; a cost delta on a deploy day is a boundary, not a change; a TTFT delta under the probe's daily-median noise is "no evidence", not "unchanged". It ends with a recommendation in one of three words — ship, watch, investigate — and names the KPI that decided it.
- The `cutting-a-release` skill gains one step: run the readout for the *outgoing* release and paste its KPI table into the release PR, so each release PR carries the previous release's verdict and a reviewer can see a trend before the next one ships.

### 6.6 PR-4 on dev, 2026-11-07

Run from the PR branch against dev's own tables and CloudWatch (the route handlers called directly with dev's DynamoDB and metrics, the same code the deployed routes run), before merge deployed them.

- **Export round-trips the backfilled range.** `export?from=2026-06-23&to=2026-11-06` returned 424 `LEDGER#VERSION`, 585 `MODEL`, 671 `TOOL`, 190 `AGENT` and 48 `PROJECT` rows (1,918, the backfill's count), plus the deploy and backfill markers. Summed, the version lines and the model lines each give 10,095 calls and $83.174091, §7.1's figures, and the export's id-only filter dropped no attribute of any row.
- **Readout, versions.** Default candidate 1.33.0 (window 2026-11-06, its one full day) against 1.32.0 (2026-11-01 to 11-04, three full days): cost per turn $0.00865 → $0.01226 (n 956 / 117), server first-token p50 2,030 → 2,714 ms; caveats `short_window` ×2, `commit_attributed` ×2, `mix_shift` (Haiku 4.5 0.51 → 0.00 of calls, Haiku 5.5 0.45 → 0.92: the model change, not the code, is the first explanation), `below_floor`, `estimated_rows`, `payload_not_measured`, `thumbs_below_floor` ×2. The provenance join found 101 verdicts on 91 sessions; 55 baseline claims (6 `quote_unsupported`, 0.109) and 9 candidate claims, under the floor.
- **Readout, builds.** `version=1.33.0#d5ead33e05f3e229&baseline=1.33.0#f200a6a3514248d3`, the first live-ledger Runtime build and the pre-flag build before it: both windows are the one shared, still-open day, so `no_full_day` ×2 (including today), `deploy_in_window` ×2, `latency_mixed_window`, six `below_floor` and `thumbs_below_floor` ×2 come back with the numbers.

## 7. Backfill

`backend/scripts/release_ledger_backfill.py backfill --prefix --version-map versions.json [--from] [--to] [--report] [--yes]` does the one scan this spec otherwise avoids: every `C#` row in the range through `LEDGER_ROW_PROJECTION` (content-free, and the only attributes the mapping reads), mapped by the **same** `ledger_updates` function the live writer uses and `ADD`ed into the same rows. Default is a dry run that prints totals and reconciles every full day against `ROLLUP#DAILY`; `--yes` writes.

**Corrected while building PR-3.** The first draft had the script refuse any range overlapping stamped rows, so the map would end where the stamps began. That cannot work: PR-1's stamps went live on dev on 2026-11-06 at about 22:04 UTC, long before this flag turns on, so every call between the two would have been in neither the backfill nor the live ledger. The boundary that matters is the first **live ledger write**, not the first stamp:

- A row stamped with `appVersion` / `build` is attributed by its own stamp. An older row is attributed by a deploy-time map, `build-map --branch develop|main`: each successful `backend.yml` run on the branch, at its completion time, labelled with the `VERSION` at its commit and a build of `git-<sha12>`. The image's content hash was never recorded for those runs, so the label says it is a commit, not a hash. A row older than the map's first entry is `unknown`.
- The script refuses a `--to` past the earliest `firstLiveAt` on `LEDGER#VERSION` (those calls are already counted), and with no `--to` it stops exactly there, or at now if the flag has never been on.
- Each run writes a `LEDGER#BACKFILL` marker first, and a run that overlaps any marker is refused, because `ADD` is not idempotent. An interrupted run leaves its marker at `status: started`, which blocks a rerun until someone repairs it by hand.
- So the procedure on an environment is: backfill up to now, turn the flag on, then run once more with no `--to` to fill the gap up to the first live write.
- **Known gap at the cutover.** Turning the flag on redeploys the Runtime, but a microVM already serving a session keeps its old environment until the session ends, so a few minutes of calls after `firstLiveAt` can be written by a process with the flag still off. The per-day reconciliation shows it on the cutover day.

The census fields that did not exist before PR-2 (`resultChars`, `inputChars`, `durationMs`) backfill as absent, so a tool row from before it has calls and errors and no payload, and the tools table marks that range "payload not measured". `outcome` likewise, which is why the outcome shares divide by `outcomeTurns`.

### 7.1 Dev, 2026-11-07

Range: every `C#` row before 2026-11-07 00:00 UTC (the first row is 2026-06-23). 10,095 rows, 10,069 attributed by the map (697 successful `develop` runs) and 26 by their own stamp; none unknown. 1,918 ledger rows written (424 `LEDGER#VERSION` rows across 388 version/build pairs), read back to the same $83.174091 and 10,095 calls.

Against `ROLLUP#DAILY` over the 117 full days: **115 days agree exactly** on call count and cost. Two do not, and in both the ledger agrees with the `C#` rows, which are the source of truth:

| Day | Calls | Cost | Why |
|---|---|---|---|
| 2026-07-10 | ledger +5 | +$0.000583 | five `C#` rows the `ROLLUP#` bump never counted (its failures are swallowed by design) |
| 2026-09-03 | ledger −121 | −$0.331052 | `ROLLUP#DAILY` counted 746 calls; 625 `C#` rows exist for the day, so 121 were deleted after they were rolled up |

In total the ledger reads $83.174091 against `ROLLUP#DAILY`'s $83.504559, a difference of −$0.330468 (0.40%), all of it those two days.

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
| **3 — Ledger writes** (built) | the flag, `LEDGER#VERSION/MODEL/TOOL/AGENT/PROJECT` bumps in `_update_system_rollups_async`, the KPI module, the backfill script run on dev | dev rows accumulate; backfilled dev range matches `ROLLUP#DAILY` totals within rounding (§7.1: 115 of 117 days exact, the other two explained by the `C#` rows themselves) |
| **4 — Ledger API** (built) | the seven routes, the CloudWatch join and cache, the thumbs version arm added to `/admin/feedback/fleet`, content-policy coverage | the readout for two dev builds returns with caveats; export round-trips the backfilled range |
| **5 — Publish** (built) | `eval_harness.py score --publish`, `smoke_turns.py --publish`, both nightly tracks publish (the smoke one on dev, §5.4); the eval spec's §12 phase 3 marked as landing here | a nightly run on dev writes `LEDGER#EVAL` and `LEDGER#SMOKE` and the quality route returns them |
| **6 — Tab** (built) | the Releases tab, bands on the trend chart, the quality panel, exports | browser-verified on dev against two builds; signed-out and `admin.costs`-less users see nothing |
| **7 — Claude** | `release_readout.py`, the `/release-readout` skill, the release-skill step, the docs-site flag row, a CLAUDE.md line under the cost tenet ("verify a release against the ledger, not a smoke run") | the outgoing release's readout appears in the next release PR; a prod backfill is run once the flag is on there |

PR-1 and PR-2 are safe for prod on merge (attributes only). PR-3 onward wait on the flag.

## 10. Open questions — answered 2026-11-06

1. **Prod backfill timing.** Evenings, about 22:00 local, which is prod's quiet hour. It cannot run before PR-3 exists and PR-1's stamps have been live for at least one deploy. Scheduled when PR-3 lands, not before. *Corrected in PR-3:* the script does not refuse stamped rows — it attributes them by their own stamp, and refuses only a range that reaches past the first live ledger write (§7), so on prod it runs once before the flag goes on and once after, to fill the gap up to the first live write.
2. **Thumbs under the floor.** Decided: the quality panel shows the raw up and down *counts* under the floor, labelled "below floor (n=14)", and never a rate. Thumbs are kept as an arm because the join already exists and costs nothing, but they are the last row of the panel, not the first: at prod's rate of about one thumb a day the arm will sit under the floor for most of a version's life, and the signals above it (eval gate, empty answers, tool errors, interrupted share) are the ones expected to move. If a year of versions shows the arm never reaching the floor, it comes out.
3. **Spec tokens per tool.** Decided: do it. The CountTokens script's output is committed as `backend/evals/tool_spec_tokens.json` keyed by tool id, re-run when the catalog's schemas change (a test compares the committed ids with the catalog and fails on a tool with no row), and the tools table shows the spec column from it. Lands with PR-2, since that is the PR that makes the tools table worth reading. **Landed in PR-2.** There was no committed CountTokens script to reuse (the earlier measurement was a one-off recipe), so `backend/scripts/measure_tool_spec_tokens.py` is new: it builds each seeded id's family with the turn path's own factories, counts it on the base Haiku 4.5 id as the marginal cost beside an anchor tool (so Bedrock's ~560-token tool-use preamble is in the baseline, not charged to the first tool), and records each family member alone too, keyed by the tool name the census uses. Each row carries a `specHash`, and the test fails on a stale schema as well as a missing row, since a reworded description is tokens too. First measurement, dev us-west-2, 2026-11-06: 22 seeded ids, 118 (`get_my_quota`) to 2,852 (`create_powerpoint_presentation`) tokens, about 19.2k with every family on; chars/4 underestimates them by 5–30%.
4. **Alarms per version.** Deferred until two versions of ledger data exist on prod.
