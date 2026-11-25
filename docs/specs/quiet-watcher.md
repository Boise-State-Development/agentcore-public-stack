# Quiet Watcher — Slice 1 of the Proactive Agent Gaps

**Status:** PR 1.1 and PR 1.2 built (see §5); PR 1.3 (the web app) and the dev readout to come.
**Date:** 2026-11-25
**Targets branch:** `develop`
**Parent:** `docs/specs/proactive-agent-gaps.md` (slice 1: G1 job state, G2 silent outcome, G3 step 1 polling, G4 `agent_item`)
**Flag:** `PROACTIVE_RUNS_ENABLED` / `CDK_PROACTIVE_RUNS_ENABLED`, `features.proactiveRuns`. In development: off by default, only `"true"` enables.
**Companion:** `docs/specs/quiet-watcher-triage.md`, the design for the second triage stage (a decider that settles whether a watch run has anything new before the schedule's own model runs).

---

## 1. What a user gets

A schedule can be a **watcher**. "Every 15 minutes, check my GitHub notifications and tell me when something needs me." Every run checks. A run that finds nothing new leaves **nothing behind**: no conversation, no unread dot, no notification. A run that finds something new leaves one conversation and one bell item per finding. The schedule's page shows every run, quiet ones included, and what the watcher is tracking.

## 2. Decisions (2026-11-25)

| Question | Decision | Why |
|---|---|---|
| Is a watcher a new entity? | **No: a schedule with extra fields** (`mode`, `modelId`) and a state item beside it | Reuses the form, dispatcher, worker, pauses and slice 0's notices. A new entity can come with push triggers (slice 5) |
| How is a quiet run kept cheap? | **One run on the schedule's own model**; the form steers intervals under 1 hour to a small model | A run must call connector tools to see what's new, so triage can't be a toolless side call. Measure quiet-run cost on dev before adding a second stage |
| Where does a finding show up? | **A new conversation per notifying run**, as scheduled runs do today, plus one bell item per finding | No change to history or caching. A per-watcher feed can come later |

## 3. Design

### 3.1 Schedule fields

`ScheduledPrompt` gains:

- `mode: "standard" | "watch"`, default `"standard"`. A standard schedule behaves exactly as today.
- `modelId: Optional[str]`: the model the run uses, passed to `run_agent_headless(model_id=…)`. It is checked with `grants_model_access` at create and edit, like an Agent's model override, and is unset by default (the Agent's or the user's default, as today).

Both can be set only while the flag is on. A `watch` schedule whose interval is under 60 minutes gets a form note, not a refusal: it states the runs per day and recommends a small model. The catalog has no "small" marker, so the note doesn't guess which models are small.

### 3.2 Watcher state (G1)

One item per watcher, beside the schedule in sessions-metadata:

```
PK = USER#{userId}   SK = SCHEDSTATE#{scheduleId}
cursors:  {name: value}   ≤ 20 entries, value ≤ 200 chars   written by the model, through report
tracking: [text]          ≤ 20 entries, ≤ 200 chars each    written by the model, through report
seen:     [key]           ≤ 500, newest last                 maintained by the worker
version:  int                                                 conditional write, so two runs never both advance it
lastRunAt, updatedAt
```

The model never writes state directly. It returns the new state through `report` (§3.3), and the **worker** writes it after the run, with one conditional `UpdateItem`. So the Runtime needs no new table access and no new tools beyond `report`, and a run that crashes writes nothing.

Deleting the schedule deletes its state. "Start over" (`DELETE /schedules/{id}/state`) clears it, so the next run reads everything as new.

### 3.3 The run (G2)

**Request.** `InvocationRequest` gains `run_mode: Optional[Literal["watch"]]`. The worker sends it for a `watch` schedule, along with `incognito: true`, so quiet runs never feed long-term memory.

**The `report` tool.** A constant injected tool, built only when `run_mode == "watch"`, the session is headless, and the Runtime's copy of the flag is on:

```
report(outcome: "nothing_new" | "notify",
       items?: [{key, title, summary?, urgency?: "now" | "today" | "fyi"}],   ≤ 10 items
       state?: {cursors?, tracking?})
```

- `key` is the finding's stable identity (a message id, an issue URL). `title` is at most 120 characters; `summary` at most 300.
- It returns `Recorded.` and does nothing else. The arguments reach the worker on the SSE stream (`tool_use` input), captured by the harness accumulator. The last call wins.
- Its spec is constant per deployment. It is pinned by a digest test, and its tokens are measured with `scripts/measure_tool_spec_tokens.py`. It is appended after every existing family in `_build_turn_tools`, so no other spec moves.
- It is **not** in `KEY_DESCRIBED_INJECTED_TOOL_IDS`. Every headless run is a fresh session, so the agent cache never hits anyway.

**The prompt.** The worker puts a bounded `<watch_state>` block in the user message:

- the last run's time;
- the cursors and tracking list;
- the 50 most recent seen keys;
- one paragraph of instruction: check from the cursors, report only what's new, and call `report` once at the end.

It never goes in the system prompt, so the system prompt and `toolConfig` stay constant across a watcher's runs.

**Outcome rules, applied by the worker:**

| Run ended with | Worker does |
|---|---|
| `report(nothing_new)` | Quiet |
| `report(notify, items)`, every key already in `seen` | Quiet. The worker dedupes; the model's word isn't trusted |
| `report(notify, items)` with new keys | Notify, for the new items only |
| No `report`, but final assistant text | Notify, with one item titled with the schedule's label. A finding is never swallowed |
| No `report`, no text | Quiet |
| `error`, `timeout`, `oauth_required` | As today: failure streak, pause, slice 0 notice |
| Completed, but over quota (`quota_exceeded`) | A failure, never a finding: the text is the quota message. Failure streak, then the slice 0 notice |

- **Quiet** means the run's session is **soft-deleted**: moved to `S#DELETED#`, its `C#` cost rows kept for quota, the ledger and audit. No unread dot, no inbox row, and `lastRunSessionId` is left unchanged. The run's messages stay in AgentCore Memory until its retention expires (app-api's memory cascade is not run for a quiet run); the run is incognito, so nothing reaches long-term memory. The soft delete moves from `SessionService.delete_session` (app-api) into `apis.shared.sessions` so the worker can call it, and app-api keeps calling the same function.
- **Notify** keeps the session, sets the unread dot, and writes one `agent_item` inbox row per new item: `{scheduleId, runId, sessionId, label, title, urgency, key}`. The title is the model's short display line for the user's own inbox, held to the same standard as a conversation title. No summary text goes in the inbox.
- **Either way**, the state is saved: the new cursors and tracking from `report`, plus the items' keys appended to `seen`.

**Resume.** A watch run that pauses (OAuth consent) resumes without `report`, because a resume never rebuilds injected tools. It then ends by the "no `report`" rows above: notify if it wrote anything. Rebuilding `report` from the snapshot, as `client_tools` are, is a follow-up if this proves common.

### 3.4 Run history

The worker writes one row per run for **every personal schedule**, standard or watch:

```
PK = USER#{userId}   SK = SCHEDRUN#{scheduleId}#{finishedAt}#{runId}   ttl = 30 days
runId, status (completed | quiet | notify | error | timeout | paused), itemCount, sessionId?, finishedAt,
inputTokens, outputTokens, cacheReadTokens
```

`GET /schedules/{id}/runs` returns the newest 50, behind the flag. Dollar cost per run is left for later: the run's `C#` rows land asynchronously from the Runtime, and tokens are enough to tell a 2k-token quiet run from a 40k-token one.

### 3.5 Bell

`agent_item` reads "*{label}*: *{title}*" and opens the run's conversation. Urgency only orders and badges it in slice 1. Push comes in slice 3.

## 4. Latency, cost and cache

- **The interactive turn path is unchanged.** `run_mode` is absent on every non-headless request, `report` is never built there, and nothing new is awaited before the first token. The `test_turn_path_io_inventory` stays as is.
- **A quiet run costs one small-model turn plus its tool calls.** The dev readout states the measured tokens and dollars per quiet run, and the per-day figure for a 15-minute watcher, before the flag goes on anywhere else.
- **Prefix stability across a watcher's runs.** The system prompt and `toolConfig` are constant; the `<watch_state>` block lives in the user message. The dev readout checks `systemPromptHash` and `toolConfigHash` on consecutive runs' `C#` rows. A 15-minute interval is past the default 5-minute cache TTL, so cross-run cache reads aren't expected and aren't the goal.

## 5. PRs

| PR | Scope | Behaviour while the flag is on |
|---|---|---|
| **1.1: data, flag, API** | Flag plumbing (`feature_flags.py`, `config.ts`, `platform.yml`; app-api and the scheduled-runs Lambdas now, the Runtime in 1.2); `mode` and `modelId` on schedules (validated, accepted only while on); the worker passes `model_id`; the `SCHEDSTATE#` store and the `SCHEDRUN#` rows; `GET /schedules/{id}/runs`, `GET`/`DELETE /schedules/{id}/state`; this spec | A watch schedule runs like a standard one; every personal run leaves a history row |
| **1.2: the watch run** | `run_mode` on `InvocationRequest`, the `report` tool (digest pin, token measurement), harness capture of `report` input and `incognito`, the worker's state block, dedupe and outcomes, soft delete moved to `apis.shared`, the `agent_item` inbox kind; the Runtime's copy of the flag (written only while on) | Watchers are quiet until something is new |
| **1.3: the web app** | `features.proactiveRuns`; mode and model in the form, with the short-interval guidance; a schedule detail view with run history (quiet runs counted) and the state with Start over; `agent_item` in the bell | Users can make and inspect watchers |
| **Dev readout** | A 15-minute watcher on a dev connector for a day: quiet-run tokens and cost, prefix hashes, false-positive and missed findings | Decides whether a second triage stage is needed before slice 4 |

**Built:** PR 1.2, 2026-11-25. Two choices the table above leaves open: a watch run's agent is never cached (`report` is not a cache-key element, and a later turn in a notifying run's conversation must not be served it), and a run with no `report` keeps the previous cursors and tracking and adds no `run:` key to `seen`.
