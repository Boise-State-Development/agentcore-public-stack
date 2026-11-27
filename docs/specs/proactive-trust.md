# Proactive Trust — Slice 4 of the Proactive Agent Gaps

**Status:** Build plan. Nothing here is built. The developer settled §12 on 2026-11-27, Q9 included (recover, keep alive, re-pin). The grant fix of §5.2 is built in #2518's PR. The PR plan (§11) can start; each build PR is the developer's call. PR 4.0 is merged and dev-checked (#2495).
**Date:** 2026-11-26
**Targets branch:** `develop`
**Parent:** `docs/specs/proactive-agent-gaps.md` (slice 4: G7 standing instructions and dismissals, G8's pre-flight, G9 budgets and governance)
**Builds on:** `docs/specs/quiet-watcher.md` (slice 1), `docs/specs/answer-from-inbox.md` (slice 2), `docs/specs/quiet-watcher-triage.md` (T.1 and T.2, in shadow)
**Flag:** `PROACTIVE_RUNS_ENABLED` / `CDK_PROACTIVE_RUNS_ENABLED`, `features.proactiveRuns`, for everything in development (§10). Two pieces are fixes and ship unflagged (§10.2).

---

## 1. What a user gets

Slices 1 and 2 made a watcher that is quiet until something is new, and that can ask before it acts. This slice makes it one the user can leave running:

- **It knows what matters to them.** A watcher has standing instructions ("always tell me when the provost writes; never about newsletters"). When the user marks items **Not useful**, the watcher sees the counts and may suggest a change to its instructions. The user accepts or ignores it. Nothing changes on its own.
- **Its spend has a daily ceiling.** Any schedule can have a daily budget, and a short-interval one must. The schedule page shows today's spend. A schedule that reaches its budget runs no more checks that day and says so in the bell. The bound is per schedule and checked between runs: one run in progress finishes (within the run's 14-minute cap), and the user's monthly quota stays the cap across all their schedules (§4.2).
- **It says before it loses access.** "Scheduled runs stop on Dec 4 unless you sign in" arrives days ahead. Reconnecting a tool resumes the schedules it had stopped.
- **It leaves a record of what it did.** Every unattended tool call that isn't known to be read-only has a row: which tool, when, whether it was approved and by whom, and whether it succeeded. A watch run that made a known write always keeps its conversation, so it never vanishes with a quiet run.
- **Notification rules exist for Push to read.** Before Push (slice 3) there is no channel to silence, so the bell orders and badges by urgency and the rest waits for Push (§6).

## 2. What reality changed since the gap spec

The gap spec (2026-11-25) proposed shapes before slices 1, 2 and triage were built. Reading the code moves several of them:

| Gap spec says | What the code does | Consequence for this slice |
|---|---|---|
| G7: the attention page is a Memory Space page the job binds; "agent edits reuse the existing rule that a scheduled run's shared-memory saves become proposals" | Memory Spaces reach a run **only through a Shared Project** (`agents/builtin_tools/memory_spaces/project_tools.py`, scopes `project` and `mine`). A personal run has no memory-space tool and no binding. The proposal rule is project-scope only: `ProjectMemoryProposals`, reviewed by the project's editors | A personal watcher can't bind a page today. §3.1 puts the instructions on the schedule, and the agent's edits become suggestions in the bell. A Memory Space binding stays the path once personal spaces reach runs (§12 Q4) |
| G4: `dismiss`, `not_useful`, `snooze` on an item, "recorded as feedback for G7" | None is built. The bell has **read** only (`/notifications/{id}/read`, `/read-all`). Reading is the normal case, so it is no signal | §3.2 adds **Not useful** (and **Useful**) as the signal. Dismiss and Snooze are not feedback and stay out of this slice |
| G8: "warns at 'expires in 3 days'" for the grant **and** vaulted OAuth tokens | The grant's expiry is exact: `HeadlessGrant.ttl = token_issued_at + 30 days`. A connector's is **unknowable**: `resolve_token_or_consent_url` answers "a token" or "a consent URL", with no expiry, and AgentCore refreshes behind it | The grant gets a real "expires in N days" warning (§5.1). A connector can only be found **already lapsed**, ahead of the run that needs it (§5.3) |
| G8: the grant lapses "30 days without a login" | The grant re-pins only on Run now, **Enable scheduled runs** and an inbox answer (`_resolve_grant` in `apis/app_api/runs/routes.py`). Signing in does not re-pin it. A re-pin anchors on the **pinned session's own login** (`token_issued_at = session.created_at`), not on now. Signing out of that session revokes its Cognito refresh token (`_revoke_refresh_token`), which is the token the grant holds | A user who signs in every day still loses scheduled runs 30 days after the login of the session they enabled from, which can be much sooner than 30 days after the click. Signing out of that session stops them at once, with nothing telling the user how to recover (found in code and config, §5.2). Both are fixed in #2518, which also re-pins at sign-in (§12 Q9) |
| G9: "summed from its runs' `C#` rows by `runId`" | `C#` rows carry **no `runId`** (`store_message_metadata`); they are keyed by session. They are also written **after `done`**, so a worker that reads them at the end of a run races them | The run's cost comes from the turn's final `metadata` frame, which already carries `cost.total`, priced per call by the same calculator the `C#` rows use (§4.1) |
| G9: "an interval under 1 hour requires a triage model to be set" | Tier 1 needs **no** model. Tier 2 has **no default** model and may never be built (`quiet-watcher-triage.md` §2.2, §7). Slice 1 decided on a form note, not a refusal (`quiet-watcher.md` §3.1) | The guard is a **budget**, not a model: §4.4 |
| G9: admin spend "under its own `callKind`" | A watch run's calls are **turns**: `call_kind()` buckets them `chat`. Only side calls carry a `callKind` (the tier 2 decider will be `watch_triage`) | Unattended spend is split by **run trigger**, not `callKind`, so a turn stays a turn (§8) |
| (not in the gap spec) | **The runaway guard pauses every 15-minute schedule after 24 runs.** `max_runs_per_day` defaults to 24, nothing sets it, and every completed run (quiet ones too) counts. A 15-minute watcher runs 96 times a day | Fixed first, unflagged (§4.5, PR 4.0). Until then, the triage shadow's 7-day readout (`quiet-watcher-triage.md` §6.5) pauses every watcher about 6 hours into each UTC day |
| (not in the gap spec) | An MCP tool asks for approval only if an admin flagged it `needs_approval`. **An unflagged write tool runs unattended** on a headless run, and a watch run that wrote and then reported `nothing_new` is soft-deleted, leaving no conversation | §7 records every unattended call not known to be read-only before it runs, keeps any run that made a known write, and asks the developer whether a watcher may write without asking at all (§12 Q7) |
| (not in the gap spec) | Strands runs **every** `BeforeToolCallEvent` callback even after one raises an interrupt (`strands/hooks/registry.py`, `invoke_callbacks_async`). A hook "after the approval gate" still runs on the pass that pauses | The audit is an `ExecuteToolStage` middleware, which runs only when the call really executes (§7.2) |

## 3. Standing instructions and learning from dismissals (G7)

### 3.1 Standing instructions

**Data.** `ScheduledPrompt` gains one field:

```
standingInstructions: Optional[str]   ≤ 2,000 characters, watch mode only
```

It is set in the schedule form and edited only through the existing `PATCH /schedules/{id}`. It is separate from `promptText` on purpose: the prompt says **what to check** ("my GitHub notifications"); the instructions say **what matters** ("always tell me about review requests; never about Dependabot"). Users then edit one without breaking the other, and the agent can suggest edits to the second without touching the first.

**The run.** `build_watch_prompt` renders it in the user message, after `<watch_state>` and before the prompt:

```
<watch_state> … </watch_state>

<standing_instructions>
{text, "<" escaped as in watch_state}
</standing_instructions>

{promptText}
```

- **Never the system prompt.** A watch run's system prompt and `toolConfig` stay constant per deployment, so Haiku 5.5's tools-and-system segment (5.2–5.6k tokens, `quiet-watcher.md` §6.1) can read from cache across runs, and across watchers of the same user when the 1-hour TTL is on.
- **Escaped like `watch_state`.** Text the user typed can't close a block.
- **The instruction paragraph** (`_INSTRUCTION` in `watch_run.py`) gains one sentence: follow the standing instructions; when they conflict with the prompt, the instructions win.

**Editing invalidates the probe.** A changed instruction can make an unchanged source newly relevant ("also tell me about Dependabot"), and tier 1 would call that run quiet because no fingerprint moved.

- **The mechanism.** The worker stores `instructionsHash`, a SHA-256 of `promptText` and `standingInstructions`, with the probe it saves (triage §3.4's write). `skip_reason` gains one row: a stored hash that differs from the schedule's current one forces a full run (`instructions_changed`).
- **Why a hash, not a `REMOVE` on edit.** A `PATCH` can't bump the state's `version`, so a `REMOVE` races an in-flight run: the run's save would write a fresh probe built under the old instructions. The hash is compared at the next run, so it needs no cross-item write and can't lose that race.
- **The decider.** Tier 2 (if T.3 is built) reads the instructions too, beside the prompt text.

### 3.2 Feedback: Not useful and Useful

**The signal.** An `agent_item` bell row gains two actions, **Not useful** and **Useful**. Dismissing or reading a row says nothing, because those are the normal case.

**The route.** `POST /notifications/{notificationId}/feedback {value: "not_useful" | "useful" | null}` on app-api, behind the flag. It checks the row is the caller's (`INBOX#{email}`) and an `agent_item`. It sets `feedback` and `feedbackAt` on the row, and `null` clears it. Then it updates the counts. Changing an answer moves one count, never adds a second.

**Where the counts live.** A new item beside the watcher's state:

```
PK = USER#{userId}   SK = SCHEDFEEDBACK#{scheduleId}
sources: {source: {useful: int, notUseful: int, lastAt}}   ≤ 20 sources, least recently updated dropped
updatedAt
```

- **Its own item, not `SCHEDSTATE#`.** "Start over" (`DELETE /schedules/{id}/state`) resets what the watcher has seen. It should not erase what the user taught it. Deleting the schedule deletes both.
- **Never touches `version`.** The run's state save is conditional on `version`. A feedback write that bumped it would fail an in-flight run's save and lose its cursors. On its own item, the two can't collide.
- **One transaction.** The row's `feedback` and the item's counts are written in one `TransactWriteItems` across the two tables' items. The inbox row lives on the projects table, so that is a cross-table transaction, which DynamoDB supports and app-api already holds both grants for. The transaction's condition is the row's previous `feedback`, so two clicks can't both count.
- **Counts only.** No title, summary or key goes on this item.

**What a "source" is.** `report`'s items gain an optional `source`: the model's short name for where the item came from, such as `github:org/repo` or a sender's domain. The worker copies it onto the `agent_item` payload, and the feedback route reads it from there. An item with no `source` counts under `(none)`.

- **It is untrusted text.** The model writes it from content it read, and it is replayed into every later run's `<watch_state>`. The worker normalises it before storing: lowercased, trimmed, limited to `[a-z0-9._:/@-]`, at most 64 characters. Anything else becomes `(none)`. That also keeps the counts from fragmenting over spelling.

**What the model sees.** The worker reads `SCHEDFEEDBACK#` with the state, in one `BatchGetItem` (the state read today is one `GetItem`, so this adds no round trip). `<watch_state>` gains:

```
"feedback": [{"source": "github:org/repo", "notUseful": 9, "useful": 1}, …]   ≤ 10 entries, most marked first
```

That is about 15 tokens an entry, at most ~150. The raw history never reaches the model.

### 3.3 The agent suggests; the user decides

`report`'s `state` gains one optional field:

```
state.instructionSuggestion?: {text: ≤ 2,000 chars, reason: ≤ 200 chars}
```

The new instructions in full, and one line on why.

- **What the instruction paragraph says:** when feedback shows the user doesn't want a kind of item, suggest new instructions rather than quietly stop reporting it. Never suggest more than once in seven days.
- **The worker** writes one `agent_suggestion` bell row: "*Inbox watcher* suggests a change to what it tells you". The text itself stays off the row (the inbox's ids-and-display-lines rule).
- **Where the text lives.** On the `SCHEDFEEDBACK#` item as `pendingSuggestion {suggestionId, text, reason, at, runId}`, beside `lastSuggestionAt`. A newer suggestion replaces it.
  - Not on `SCHEDSTATE#`, so "Start over" can't orphan a bell row or reset the seven-day rule.
  - Never rendered into any run's prompt, so injected text can't re-enter later runs through it.
- **The seven-day rule** is enforced in the worker from `lastSuggestionAt`, not only by the model.
- **Review** opens a dialog with the current and suggested instructions side by side.
  - **Accept** goes through `POST /schedules/{id}/instruction-suggestion/accept {suggestionId}`. The id must match the stored one, or it answers 409 (a newer suggestion arrived while the dialog was open). It writes `standingInstructions` through the same validation as `PATCH`, removes `pendingSuggestion`, and resolves the bell row. The hash rule (§3.1) makes the next run a full run.
  - **Dismiss** removes `pendingSuggestion` only.
- **Nothing is applied without Accept.** A watcher reads untrusted content (an email, a web page). Injected text could steer it to suggest "never tell me about security alerts". The suggestion is shown in full, as a diff, before it can change anything.

**Why not let the counts suppress items directly.** A watcher that silently learns to stop reporting a source is the failure the triage spec calls the worst one: cheap and silent while something needs the user (`quiet-watcher-triage.md` §1.1). A suggestion keeps the change visible and reversible. The counts only let the model suggest; only the user changes the instructions (§12 Q5, decided).

### 3.4 Cost and cache

| Addition | Where | Tokens per run |
|---|---|---|
| `<standing_instructions>` | user message | ≤ ~500 (2,000 characters) |
| `feedback` in `<watch_state>` | user message | ≤ ~150 |
| One more instruction sentence, twice | user message | ~40 |
| `items[].source`, `state.instructionSuggestion` on `report` | `toolConfig`, constant per deployment | to be measured; T.1's `wakeAt` added 52 |

- **Per run, at worst:** about 700 tokens, all written to cache at 1.25× the base input rate. On Haiku 5.5 ($0.11/MTok) that is about **$0.0001 a run, $0.01 a day** for a 15-minute watcher. On Haiku 4.5 it is about ten times that.
- **Most runs pay less.** Instructions are optional, and feedback is empty until the user gives some.
- **The `report` change is one deliberate `toolConfig` rewrite at deploy.** It is an additive optional argument on a tool a deployment already chose, gated by `report`'s gate, so it gets no flag of its own (CLAUDE.md, *Feature Flags*). The proof: a digest test pins the new spec, and `scripts/measure_tool_spec_tokens.py` is re-run on dev, with the growth stated in the PR. A watch run's agent is never cached and every run is a fresh session, so no live prefix moves. The one exception: a run paused across the deploy resumes with the new spec and pays one prefix write.
- **Haiku 4.5's 4,096-token minimum.** A watch run's tools and system segment is about 3.9k tokens there (`quiet-watcher.md` §6). The `report` growth could carry it past the minimum, which would make that segment cache for the first time. The PR measures it and says which happened. Either way it's a saving, never a cost.

## 4. Budgets and the scale guard (G9)

### 4.1 What a run cost

The turn's final `metadata` frame already carries `cost: {total, inputCost, outputCost, cacheReadCost, cacheWriteCost}`. The stream coordinator computes it per model call through `_calculate_streaming_cost`, with the same rates and long-context rule the `C#` rows use. So:

- **The harness keeps it.** `InvocationStreamAccumulator` keeps `cost.total` from the last `metadata` frame, and `RunResult` gains `cost_usd: Optional[float]`.
- **The history row stores it.** `record_schedule_run` writes `costUsd` on `SCHEDRUN#`. A resume adds its own cost to the row, as it adds tokens today (`finish_resumed_run`).
- **No extra read, no race.** The cost arrives on the stream the worker is already reading. The `C#` rows that hold the same figures are written after `done`, so reading them at the end of a run would race them.
- **What the frame doesn't cover: side calls.** It sums the main agent's calls only. A watch run makes no title or tool-summary call (`quiet-watcher.md` §6), but a standard schedule's run makes both, and either can compact. Those write their own `C#` rows with a `callKind`. For a standard run they add a few Nova Micro calls, which the budget under-counts by design. It is a guard, not a bill: quota and the ledger still read every `C#` row.
- **When the frame is missing** (a timeout, a turn cut off mid-call, a provider error), `cost_usd` is `None`, and the history row is marked `costPending`. Those are often the costliest runs, so no stand-in figure is used. Instead, the worker reconciles at the schedule's next run: for any `costPending` row, it sums that run's session `C#` rows (one Query on `SessionLookupIndex`; a quiet run's rows are kept). It writes `costUsd`, and adds the amount to the day it ran if that is still today. Only runs that failed pay this read.
- **The tier 2 decider**, if T.3 builds it, returns its cost on `watch_triage_result`, and the worker adds it the same way. Tier 1 makes no model call and costs nothing here. Runtime compute is not in the budget, as it is in no model figure in this stack.

### 4.2 The budget

`ScheduledPrompt` gains:

```
maxCostPerDay:    Optional[Decimal]   USD; null = no budget (today's behaviour)
spendToday:       Decimal              maintained by the worker
spendTodayDate:   "YYYY-MM-DD"         in the schedule's own timezone
budgetHoldUntil:  Optional[ISO]        set while a day's budget is spent (§12 Q2)
budgetDaysAtLimit: int                 consecutive days the budget was reached
```

**Who has one.** Any personal schedule, standard or watch, may set a budget. One whose interval is under 60 minutes must have one (§4.4). Run now is not counted: the user pressed it and watches it, and its cost is metered and on quota like any turn.

**The day is the schedule's day.** "20 cents a day" means the user's day, so the date is computed in `ScheduledPrompt.timezone`. `runsToday` stays UTC: it is a runaway guard, not a user number (§12 Q3, decided).

**The worker keeps the count.** After every run and every inbox resume, it adds the run's cost with one conditional `UpdateItem`:

- `ADD spendToday :c`, conditional on `spendTodayDate = :today`;
- if that fails, `SET spendToday = :c, spendTodayDate = :today`, conditional on `spendTodayDate <> :today` (or absent);
- if that fails too, another writer just rolled the day over, so retry the first.

So two concurrent writers at midnight never lose one's cost. It runs beside `record_run_result`'s write. Today that function reads the schedule and then writes it; this change doesn't make it atomic, and doesn't need to.

**Reaching the budget.** When the new `spendToday ≥ maxCostPerDay`, the worker holds the schedule until its next local midnight (below) and writes one `fyi` `schedule_paused` bell row with `reason: "budget"`, the day's spend, the budget and when it resumes.

**The dispatcher's guard 2b.** The dispatcher already reads the whole schedule from `DueScheduleIndex`, so a guard beside the runaway guard costs nothing:

- It compares `spendToday` (for today's date) with `maxCostPerDay`, and honours `budgetHoldUntil`. It is the backstop for a budget the user lowered below today's spend.
- **It always re-arms.** A guard that skipped without re-arming would leave the schedule most-overdue, and enough of those would fill every tick's `Limit=20` sweep and starve every other schedule. Over budget, the dispatcher re-arms `nextRunAt` to the hold's end, with the same conditional `rearm_schedule`, and invokes nothing.

**Edits respect the hold.** A `PATCH` that recomputes `nextRunAt` (cadence, hour, timezone) takes the later of the new time and `budgetHoldUntil`. Raising `maxCostPerDay` above today's spend clears `budgetHoldUntil` and recomputes `nextRunAt`, so the schedule runs again today.

**An inbox answer still runs.** The user chose to answer, so a resume is never refused for the budget. Its cost counts toward the day, and can be the run that reaches it.

**A held watcher costs nothing**, so it never moves the count.

**What the budget does not bound:**

- one run in progress, which runs to its end within the harness's 14-minute cap;
- the sum across a user's schedules, for which the monthly quota is the cap: there is no per-user daily cap (§12 Q1, decided);
- side calls on standard runs (§4.1).

**How a run past the budget is stopped (§12 Q2, decided: hold until tomorrow).**

- **The hold.** `budgetHoldUntil` is set to the schedule's next local midnight, and `nextRunAt` is re-armed to the later of that and its next cadence time, with the existing conditional `rearm_schedule`. The schedule stays `active`, so the due index wakes it with no new machinery.
- **The user does nothing.** The schedule resumes tomorrow, and its one `fyi` bell row says when.
- **The bound.** A schedule that reaches its budget every day would spend it every day. `budgetDaysAtLimit` counts consecutive days at the limit (reset by a day that ends under it), and at 3 the schedule pauses with `pause_schedule(schedule, "budget")`: `paused_error`, off the due index, until the user presses Resume.

### 4.3 Defaults and the form

- **The default** for a new schedule under 60 minutes is a deployment setting, `SCHEDULE_DEFAULT_MAX_COST_PER_DAY` on app-api, shipped at **$0.50** a day (§12 Q1, decided). It is configuration, not a flag.
- **The form shows the projection.** The budget field shows "about $X a day at this interval", computed in the SPA from the schedule's recent runs (`costUsd` on its `SCHEDRUN#` rows, median of the last 20) once it has some. Before it has any, it uses a stated estimate of ~6k input tokens a run at the model's catalog rate, labelled as an estimate. No new endpoint: `GET /schedules/{id}/runs` already returns the rows.
- **Today's spend** shows on the schedule page beside the 24-hour summary: "$0.04 of $0.25 today".

### 4.4 The scale guard: a budget, not a model

The gap spec's rule, "an interval under 1 hour requires a triage model", no longer fits what was built:

- **Tier 1** needs no model. When T.5 turns gating on, a stable source's quiet run costs nothing, on any schedule model.
- **Tier 2** has no default model, waits on a readout, and may never ship (`quiet-watcher-triage.md` §7, §10 Q1).
- **The schedule's own model** is the cost driver. A frontier model every 15 minutes costs about $1.90 a day (triage §7, scenario D) whatever triage does on its quiet runs, because every escalated run is a full run on that model.

So the guard bounds **spend**, which is what it was for:

- **Any schedule under 60 minutes must have a budget,** watch or standard. `maxCostPerDay` may not be null there. Create and edit refuse a null with 422, naming the field. The default fills it in, so the user sees a number rather than an error. Existing short-interval schedules get the default once, from a one-time script in PR 4.1, so none is left uncapped.
- **The short-interval note stays** (`quiet-watcher.md` §3.1), and now quotes the projection: "Every 15 minutes is 96 checks a day, about $0.11 a day on this model".
- **The budget then covers whatever triage does.** A cheaper quiet run spends less of it; a volatile source spends more. Neither needs a rule of its own.
- **No per-model interval floor** (§12 Q1, decided). "No frontier model under an hour" isn't needed: the budget bounds the spend whatever the model.

### 4.5 The runaway guard (fix first, PR 4.0)

`max_runs_per_day` was meant to catch a schedule firing faster than its cadence (a stuck re-arm), not to cap a cadence the user chose. With a fixed 24 it does the second:

- **The fix.** The dispatcher's guard 2 uses `max(schedule.max_runs_per_day, cadence_ceiling(schedule))`, where `cadence_ceiling` is `ceil(1440 / interval_minutes) + 4` for `interval` and 24 for `daily`, `weekday` and `weekly`. It is derived where it is checked, from the cadence the dispatcher already holds, so nothing is stored, nothing needs a backfill, and an edit to the cadence can't leave it stale.
- **Not a flag.** It is a fix: a 15-minute schedule pausing itself at midday is a bug in what shipped, for standard and watch schedules alike.
- **Inbox resumes** don't pass through the dispatcher, and don't count toward `runsToday` today. That stays.
- **In the same PR: a paused schedule doesn't run.** The worker reads the schedule but never re-checks its state, so a schedule the user (or the budget) paused between the dispatcher's re-arm and the worker's start still runs once. The worker skips anything not `active`, with a log line, as it already drops a deleted one.

## 5. Connection health (G8's pre-flight)

### 5.1 The headless grant: warned before it lapses

The grant's expiry is exact. Every run already mints from the grant (`CognitoRefreshBearerAuth` reads it with `get_active_grant`), so the warning costs no extra read:

- **The auth reports what it used.** `CognitoRefreshBearerAuth` keeps the `HeadlessGrant` it minted from, and the harness copies `ttl` and `token_issued_at` onto `RunResult` as `grant_expires_at` and `grant_issued_at`.
- **The rule.** After any personal run, the worker warns when the grant lapses before the schedule's **next run** (`nextRunAt`, which the dispatcher re-armed before this run started) or within 3 days, whichever is later. That covers every cadence with one rule:
  - a 15-minute watcher warns 3 days ahead;
  - a weekly digest warns at its last run before the lapse, up to 7 days ahead, rather than after a run that already failed.
- **One warning per grant epoch.** The worker writes `expiryWarnedFor = token_issued_at` on the grant item with a conditional `UpdateItem`, and writes the bell row only if it won that write. A re-pin rewrites the grant, so a renewed grant can warn again 30 days later. The worker already has read and write on the BFF sessions table.
- **The bell row** is `connection_expiring` with `{scope: "grant", expiresAt, scheduleCount}`: "Scheduled runs stop Thu, Dec 4 unless you renew".
- **Renewing must actually renew.** A re-pin anchors on the live session's login (`token_issued_at = session.created_at`), so `POST /runs/grant` from a session signed in 28 days ago gives a grant that still lapses in 2 days. So:
  - **Renew** calls `POST /runs/grant`, and the row resolves **only if** the returned `expiresAt` clears the warning window.
  - Otherwise the row changes to "Sign in again to keep scheduled runs going", with **Sign in**, which signs out and back in, and then re-pins on the way back (§5.2, or the same `POST` from the new session if §12 Q9 says no).
  - A new login gives a new `token_issued_at`, so the new grant's own warning can fire 30 days later.
- **Every warning needs the owner's email.** The inbox is keyed by email, and a schedule created before its owner's email was recorded has none until they next list their schedules (`remember_owner_email`, `pausing.py`). Such a warning is skipped and logged today. So the warning also shows as a banner on `/schedules` from `GET /runs/grant`'s `expiresAt`, which needs no email, and the budget notice shows on the schedule page from `spendToday`.
- **No run, no warning.** A user whose schedules are all paused gets none, because nothing would fail.

### 5.2 Re-pin at sign-in, and a grant that survives a sign-out (§12 Q9, built)

The grant's own docstring states the policy as "the platform may act headlessly as a user only within 30 days **of the login**". Before #2518 it was 30 days from the login of the session the user last enabled from, because only Run now, Enable and an inbox answer re-pinned it, and a re-pin keeps that session's login time. The re-pin at sign-in, as built:

- **The change.** `/auth/callback`, after the session is created, re-pins the user's grant from the new session **only if an active grant exists**. That is one Query on the grant index and, if there is one, one `UpdateItem`, after the redirect is decided and off any turn path.
- **What it keeps.** A user who never enabled scheduled runs gets no grant, and a revoked grant stays revoked.
- **The effect.** The warning in §5.1 then reaches only users who stopped signing in, which is the case it should reach.

- **Which sign-ins.** Only the web app's. The callback also serves the terminal's device flow and the phone's native exchange, whose sessions never slide on idle (`client = "native"`). Pinning a CLI or phone token would tie scheduled runs to a device the user may revoke from the devices list.

**Signing out stops scheduled runs (found in code and config, then confirmed live on dev, 2026-11-27).**

- **The same token.** The BFF app client (`cognito-construct.ts`, the same construct on every deployment) leaves refresh-token rotation off and token revocation on. Read from dev's user pool on 2026-11-27: `RefreshTokenRotation` unset, `EnableTokenRevocation: true`. So a grant stores exactly the refresh token of the session it was pinned from. `persist_rotated_refresh_token` never fires.
- **Logout revokes it.** `POST /auth/logout` calls Cognito `RevokeToken` on that session's token (`_revoke_refresh_token` in `apis/app_api/auth/bff/routes.py`).
- **The run fails.** The schedule's next run can't refresh, raises `HeadlessAuthError`, and pauses with `reauth_required`. That is true in prod today, for standard schedules too, for any user who signs out of the session they last enabled from.

**What the user sees is worse than a silent stop.**

1. The bell says "Your schedule stopped until you sign in again". Signing in doesn't fix it, because a sign-in never re-pins the grant.
2. The grant row still reads `active` with its `ttl` weeks away, so `GET /runs/grant` answers `enabled: true` and `/schedules` hides its **Enable scheduled runs** banner. That banner also promises runs continue "even when you're logged out".
3. **Resume** re-arms the schedule, and its next run fails the same way.

Only Run now, or revoking and re-enabling the grant, pins a live token again, and nothing tells the user to do either.

**Built (#2518), unflagged, as a fix to shipped behaviour:**

1. **A refused grant is recoverable.** `CognitoRefreshBearerAuth` (`apis/shared/harness/auth.py`), where every unattended mint happens, marks the grant `needs_reauth` when Cognito answers `NotAuthorizedException`, and only then (`CognitoRefreshError.refused`). A throttle or network error leaves it alone. The write is conditional on the grant still holding the refused token, so a re-pin that won the race is never undone, and it removes the dead token. A `needs_reauth` grant isn't active, so `GET /runs/grant` reads `enabled: false` and `/schedules` shows the Enable banner again. Enabling re-pins the same grant and sets it active. A revoked grant is never reused. The `reauth_required` text reads "until you turn scheduled runs back on in Schedules", and the row's button reads **Turn back on**. Because this lives in the harness, Run now and inbox resumes get it too, and the worker is untouched.
2. **A sign-out keeps the grant's token alive.** The grant now records `pinned_session_id`, the session it took the token from.
   - **Logout.** `bff_logout`, for cookie and header-presented sessions alike, skips Cognito's `RevokeToken` when the user's active grant holds this session's token (`grant_tokens.grant_keeps_token`, one Query on `HeadlessGrantUserIndex`). It still deletes and publishes the session row, so the client is signed out. It answers `scheduled_runs_continue: true`, and the sign-in page then says "Your scheduled runs keep running", and how to stop them.
   - **Grant revoke and re-pin.** A token the grant lets go of is revoked once no live session holds it (`grant_tokens.release_refresh_token`). That covers revoking the grant on `/schedules` and re-pinning it to a newer session.
   - **What a sign-out leaves alive.** The refresh token, server-side only, in the grant. It is bounded by the grant's own window (the token's 30-day validity, from the same login) and ends when the user revokes the grant.
3. **A web sign-in re-pins an active grant** (`_repin_grant_after_web_sign_in` in `bff_callback`). It runs after the session row is written and before the browser redirect: one Query, plus one `UpdateItem` when there is an active grant, never failing the sign-in. The terminal's device flow and the phone's native exit return before it. It never creates a grant and never revives a revoked or `needs_reauth` one.

**Not built: the grant's own refresh token.** A sign-in made for the grant, so no browser session shares its token, would separate the two lifetimes fully. It is a design of its own.

**The live check, on dev (2026-11-27).**

1. Enable scheduled runs from the developer's in-app browser session pinned the grant to it.
2. A no-tools Haiku 5.5 schedule, forced through the dispatcher, completed.
3. The browser then switched to a second dev user: sign out, then sign in as the other user.
4. A second forced run of the same schedule failed at the mint with Cognito's `NotAuthorizedException`, "Refresh Token has been revoked". The worker paused it with `reauth_required`.

What else it showed:

- **Switching accounts is enough to trigger it.** No one has to mean "stop my schedules"; signing out to use another account, or signing out on a shared machine, does it.
- **Every schedule on the account stops, not only the one that ran.** Five triage-readout watchers on the same account each paused with `reauth_required` at their next run, 15:56–16:11 UTC.
- **Recovery took exactly the steps §5.2 describes:**
  - signing back in fixed nothing, because a sign-in doesn't re-pin;
  - `/schedules` offered no Enable banner, because the revoked grant still read `enabled: true`;
  - only an explicit `POST /runs/grant` from the new session, then **Resume** on each paused schedule, brought them back.
- **Cost:** two Haiku 5.5 runs, about $0.002.

This changes what consent means: from "act as me for 30 days after I click Enable" to "act as me while I keep signing in". That is the developer's call.

### 5.3 Connectors: lapsed, found before the run that needs it

A vaulted token's expiry can't be read, so a connector can only be found already lapsed. Today a run finds it, pauses with `oauth_required`, and slice 0 tells the user. Two additions, one now and one held:

**Now: reconnecting resumes what it stopped.** Today a schedule paused for `oauth_required` stays paused after the user reconnects, until they also press Resume on `/schedules`. With this change, `complete_consent` (`apis/app_api/connectors/routes.py`), after the vault confirms the token:

1. finds the caller's personal schedules whose state is `paused_error` with `stateReason: "oauth_required"` and whose `lastError` names that provider **exactly**: the token `provider={id}` parsed as a whole field, so `github` never matches `github-enterprise`;
2. resumes each as the Resume button does (`resume_schedule` in `apis/app_api/schedules/routes.py`: `compute_next_run_at`, then `set_schedule_state("active")`), moved into a shared function both call. A schedule over today's budget stays held (§4.2), and a project schedule is left to the project's own resume, which re-checks its standing;
3. resolves their `schedule_paused` bell rows.

The read is one Query on the caller's `SCHEDPROMPT#` items, which `GET /schedules` already makes. `complete_consent` already resumes the caller's KB sync policies the same way, so this follows an existing pattern. This is a fix to slice 0's behaviour, unflagged (§10.2).

**Held: a pre-flight before low-frequency runs (§12 Q10).** For a schedule that runs daily or less often, a check some hours before its run would let the user reconnect the evening before, rather than at 8 a.m. when the run fails. **Decided: held** (§12 Q10). It is built only if data shows `oauth_required` pauses are common; the grant-expiry warning (§5.1) ships regardless. The shape, kept for then:

- **A second entry in `DueScheduleIndex`** under its own partition value (`GSI3_PK = PREFLIGHTDUE`, `GSI3_SK = {preflightAt}#{scheduleId}`), on a small `SCHEDPREFLIGHT#{scheduleId}` item. It is written when the dispatcher re-arms a schedule whose interval is 6 hours or more, at `nextRunAt − 12 h`. No new index, and no new resource.
- **The dispatcher** queries that partition on each tick beside the due one, and invokes the worker with `{kind: "connection_preflight", userId, scheduleId}`.
- **The worker** sends a `connection_check` invocation to the Runtime. It resolves the schedule's tools through the turn's own phases, as `watch_triage` does (`resolve_turn_agent`, `_resolve_effective_tools`), takes the `requires_oauth_provider` of each, and asks `resolve_token_or_consent_url` for each provider concurrently. No agent, no MCP connection, no model. It answers `{providers: [{providerId, connected}], agentAvailable}`. The Runtime, not the worker, because only the Runtime can resolve "the tools this schedule would carry" without copying the tool path across the import boundary (triage §4.1, §9.2).
- **The result** is a `connection_expiring` row with `{scope: "connector", providerId, scheduleIds}` when something is lapsed, deduped per provider per day.

**Why it's held.** It adds a Runtime invocation a day per low-frequency schedule, and its value is a few hours' lead time on a pause the user is already told about. Reconnect-resumes removes most of that pause's cost. Measure how often schedules pause on `oauth_required` first: the `SCHEDRUN#` rows already record `paused`.

## 6. Quiet hours and channel rules

### 6.1 Data

Notification preferences are one item on the **user-settings table**, beside `SETTINGS`, not fields on it:

```
PK = USER#{userId}   SK = NOTIFY_PREFS
timezone:   IANA
quietHours: {start: "21:00", end: "07:00", days?: [0..6]} | null
nowBreaksThrough: bool                                  default true: `now` items ignore quiet hours
channels:   {now: [...], today: [...], fyi: [...]}   each a subset of ["bell", "push"]
version, updatedAt
```

- **Not on `SETTINGS`.** The turn path reads that item every turn, and nothing here is turn data. That is the same reason Organize lists keeps `ARRANGEMENT#` items apart.
- **Routes.** `GET` and `PUT /users/me/notification-preferences` on app-api, behind the flag. Validated: times are `HH:MM`, the timezone is IANA, and `now` always includes `bell`, so nothing raised as urgent can be silenced everywhere.
- **The default** (no item): no quiet hours; `now` and `today` go to the bell and Push, and `fyi` to the bell only.

### 6.2 What slice 4 does with them before Push

The bell is pull-only. It refreshes when it opens and when the tab regains focus, and nothing polls while a tab is hidden. So before Push there is no channel for quiet hours to hold back. Slice 4 does this much:

- **Urgency orders and badges the bell** (it only prefixes "Urgent —" today). `now` rows sort first and always count toward the badge. `fyi` rows count toward the badge only when the user's `fyi` channels include `bell`.
- **Quiet hours are stored, with no form yet.** Nothing acts on them until slice 3, whose sender reads this item, and the Settings form ships with Push in slice 3, so no setting ships that visibly does nothing (§12 Q6, decided). A `now` item breaks through quiet hours unless the user turns that off (`nowBreaksThrough`, §6.1).
- **Bell rows this slice writes carry an urgency:**

  | Row | Urgency |
  |---|---|
  | `connection_expiring` | `today`, then `now` in its last 24 hours |
  | `schedule_paused` for `budget` | `fyi` |
  | `agent_suggestion` | `fyi` |

**Considered and not done:** firing browser notifications for `agent_*` rows while a tab is hidden, as handoffs do. Handoff notifications fire from the live stream in the tab that started the handoff. An agent item has no stream in any tab, so it would need a poll while hidden, which is what Web Push (slice 3) replaces.

**Per user, not per job (§12 Q6, decided).** A person has one do-not-disturb window: a person has one do-not-disturb window. A job's control is the urgency its items carry. A per-job override can come later as a field on the schedule, read by the same sender.

## 7. The action audit (G9)

### 7.1 What is recorded

**Three classes of call.** Every tool call an unattended run makes (§7.2 defines one) falls into one of three classes:

| Class | Test | Audited | Keeps a watch run from being quiet (§7.3) | A watch run asks first (§7.5) |
|---|---|---|---|---|
| `read_only` | `runs_in_parallel` (`agents/main_agent/core/tool_concurrency.py`): `PARALLEL_SAFE_TOOLS`, `___WebSearch`, or MCP `readOnlyHint` without `destructiveHint`. The same definition triage's probe uses | No | No | No |
| `write` | **Positive evidence**: MCP `destructiveHint`; the catalog's `needs_approval`; the skill-authoring writes; or a new optional catalog marker `writes: true` that an admin sets on a tool | Yes | Yes | Yes |
| `unclassified` | Everything else: an un-annotated MCP tool, `browse_web`, a document reader not in `PARALLEL_SAFE_TOOLS` | Yes, marked `unclassified` | No | No |

**Why three, not two.** "Not known to be read-only" is the right test for the audit, which should fail closed. It is the wrong test for changing what a run does. Most un-annotated MCP tools are reads, and treating them as writes would make every watcher that uses one never quiet, undoing slice 1. Under §12 Q7, it would also pause the watcher for approval on every run. So the audit records `unclassified` calls, and only positive evidence changes an outcome.

**Excluded:** three injected tools that act on nothing outside the run, `report`, `ask_user_question` and `request_user_login`.

**The residual risk:** an un-annotated tool that writes runs like a read. It is still recorded before it runs, shows as "unclassified" in the schedule's Actions, and the dev readout lists every unclassified tool seen on headless runs so an admin can mark it `writes` or `readOnlyHint`. The `writes` marker is an additive catalog field read where the tool's metadata is already read, so it needs no flag.

**The row.** One per call, written by the Runtime through `RunAuditRecorder.record_action` (new) in sessions-metadata:

```
PK = USER#{userId}   SK = RUNACT#{runId}#{toolUseId}
GSI_PK = RUNACTS#{userId}#{scheduleId | "run_now"}   GSI_SK = {startedAt}     (SessionLookupIndex, existing)
runId, sessionId, scheduleId?, trigger, toolName, toolId (catalog id), serverName?,
class: "write" | "unclassified",  why: "destructive_hint" | "needs_approval" | "catalog_writes" | "skill_write" | "unannotated",
approval: "not_required" | "approved",  approvedVia?: "inbox" | "conversation",  approvingRunId?,
inputSha256, inputChars,
outcome: "started" | "ok" | "error",
startedAt, finishedAt?, rounds, ttl (365 days, §12 Q8)
```

- **Keyed by the call, so it is written once.** The sort key is the run and the tool use, and the put is conditional on the row not existing. An MCP form's replay of the same call (`mcp_elicitation`) or a resume of it finds the row and updates `rounds` and `outcome` rather than adding a second.
- **The index partition includes the user**, so one user's listing can never read another's rows through a missing filter, and Run now's rows don't share one partition across all users.
- **No input and no result on the row**, only the hash and length, as the `RUN#` row does for the prompt. The owner reads what was sent in the run's conversation (§7.3).
- **A declined call has no row.** It never executes. The bell's own audit row already records the decision, who made it and when (`answer-from-inbox.md` §3.5 step 6), and a declined call in the conversation is in its history.
- **The row can outlive its evidence.** A user can delete the run's conversation, and the row (hash-only) stays for its retention. That is intended: the record that something was sent should survive the conversation that sent it.

### 7.2 Recorded before it acts, or it doesn't act

**Where.** An `ExecuteToolStage` middleware, `HeadlessActionAudit`, placed inside `ToolConcurrencyPlugin` (the outermost one). Not a `BeforeToolCallEvent` hook: Strands runs every `Before` callback even after one raises an interrupt, so a hook "after the approval gate" would write a row on the pass that pauses, and again on the resume. A middleware runs only when the call really executes, after every gate has passed. It takes the place of the call:

- **Fail-closed.** It writes the row, off the event loop (`asyncio.to_thread`; the store's boto3 is synchronous). If the write fails, it returns an error result without calling the tool: "This action wasn't run because it couldn't be recorded." The model sees an error it can report, and nothing acted unrecorded. That is F6a's rule for runs ("no audit record, no run"), applied to actions.
- **The outcome** is written after the call, best-effort. A lost update leaves `outcome: started`, which reads as "may have run", never as "didn't".
- **The approval** is read from the invocation: a call whose interrupt this invocation answers is `approved`, and `approvedVia` is `inbox` when the resume's `run_trigger` is `inbox_respond`, otherwise `conversation`.

**What makes a run unattended.** Not the `headless-` session-id prefix: a user who opens a delivered run and sends a follow-up keeps that id, and they are present. Instead:

- `InvocationRequest` gains two optional fields that only the harness sends: `run_id` and `run_trigger` (`schedule`, `run_now` or `inbox_respond`). Both are absent from the SPA, the TUI and the iOS app, and a request without them behaves exactly as today.
- `PausedTurnSnapshot` records both when an unattended run pauses. A resume takes them from the snapshot, whoever answers. So an approval answered **in the conversation** still writes its row, with `approvedVia: conversation`.
- A user's own follow-up turn carries neither, and is not audited.

**Not on the agent.** The middleware is on every agent and is inert unless the invocation's state carries a run id. It reads the run id per invocation and never stores it on the agent, so a cached agent can't carry one run's id into another (CLAUDE.md: never cache session state on an agent instance). It is not an agent-cache key element.

**Turn-path impact.**

- **Interactive turns: none.** The inert check is one dictionary lookup per tool call, with no I/O. `test_turn_path_io_inventory` stays as is, and the PR adds a test that an interactive turn writes nothing.
- **Unattended runs: one `PutItem` before each audited call** (about 5–15 ms, measured in the PR). Nobody waits on a headless run's first token. Read-only calls, including every probe replay, pay nothing.
- **One attended case pays it:** a user who answers a scheduled run's approval **in its conversation**. That resume's first token comes after the approved call, so the `PutItem` is on its path. It applies only to resumes of unattended pauses, never to an ordinary turn. CLAUDE.md requires the developer to accept a measured addition there explicitly. Accepted on 2026-11-27 (§12 Q14); the PR that builds it states the measured number.
- **Nothing reaches the model** beyond the error on a failed write.

### 7.3 A run that wrote is never quiet

The Runtime marks each audited call's `tool_result` frame with an optional `audited: "write" | "unclassified"`, which the SPA, the TUI and the iOS app ignore. The harness counts them onto `RunResult`, and `decide_watch_outcome` gains the `write` count as an input. A watch run with any `write`:

- **keeps its conversation:** no soft delete, the unread dot set, `lastRunSessionId` moved;
- **records the writes:** its history row says `status: "notify"` (or the item outcome it reported) and gains `writeCount`;
- **is never quiet,** even if it called `report(nothing_new)`.

So a known write never leaves with a soft-deleted session. It already leaves no probe (triage §2.1), so its next run is a full run.

An `unclassified` call changes no outcome. A quiet run that made one is soft-deleted as today, and its `RUNACT#` rows stay. The run's messages stay in AgentCore Memory until their retention expires (`quiet-watcher.md` §3.3).

### 7.4 Who sees it

- **The owner,** on the schedule page: **Actions**, newest first, from `GET /schedules/{id}/actions` (Query on `SessionLookupIndex`, `GSI_PK = RUNACTS#{userId}#{scheduleId}`, at most 50). Each row reads "Ran *send_message* · approved in the bell · Tue 9:15 AM" and links to the run's conversation. Unclassified calls read "Ran *list_files* (not marked read-only)".
- **Admins** see ids and counts only, under the existing audit console's scope, and never the conversation (§12 Q8, decided). Rows are kept 365 days, matching `apis/shared/audit`.

### 7.5 Writes without asking (§12 Q7, decided)

The audit records unattended writes. It doesn't decide whether they may happen. Today a headless run asks first only when an admin flagged the tool `needs_approval`, or for the skill-authoring writes. **Decided:** a **watch** run asks before every `write`-class call unless the schedule lists the tool in `actWithoutAsking`. `unclassified` calls are audited but not gated, and standard schedules are audit only, with no new pauses, since a digest that files a document each morning would otherwise stop daily.

- **The gate.** An approval gate on watch runs turns any `write`-class call that no gate already pauses into an approval pause: slice 2's path, unchanged downstream. The gate is a `BeforeToolCallEvent` hook beside `MCPExternalApprovalHook`, built only when the request or snapshot is an unattended watch run. It raises no I/O, and it skips any call another gate already pauses, because Strands refuses two interrupts with one name and runs every `Before` callback either way.
- **The exemption.** The schedule may list tools in a new `actWithoutAsking` (≤ 20 entries), set in the form. Each entry names one tool on one server (`{catalogId}/{toolName}`), never a whole server, so allowing a mail connector's `create_draft` never allows its `send` or `delete`. Entries are validated against the tools the user may use (RBAC), not `enabledTools`, which may be null.
- **`unclassified` calls are not gated**, for the reason in §7.1.
- **A nuisance lever, not a silent one.** Injected content that provokes a `write` call pauses the watcher, and its hold (`answer-from-inbox.md` §3.2) stops its checks until the user answers or the pause expires in 7 days. That is visible: it is a bell row naming the tool. Declining it from the bell releases the hold at once.

## 8. Unattended spend in `/admin/costs`

A watch run's calls are turns, and must stay turns, or the release ledger would lose them from every per-turn figure. So unattended spend is split by **trigger**, not by `callKind`:

- **Cost rows gain two attributes.** On a headless session, the Runtime's `store_message_metadata` adds `runTrigger` (from the request's `run_trigger`) and `runMode` (`watch` when set). Ids only, no new GSI: the existing admin queries already read the rows, and project these two.
- **`/admin/costs` gains an *Unattended* breakdown:** spend and calls by `runTrigger` × `runMode`, with the tier 2 decider (`callKind: watch_triage`, triage §4.2) beside them. The ledger rollup (`ledger_rollups.py`) adds `unattended.<trigger>` counters to the version row, next to `callKind.<kind>`, so a release readout can show and subtract them.
- **The per-turn KPIs are unchanged for now.** At scale, watch turns could outnumber interactive ones and pull per-turn cost and TTFT toward a headless profile. They stay in the turn KPIs for now, beside the new `unattended.<trigger>` counters, and the question is revisited at the first readout with watchers at scale (§12 Q12, decided).

## 9. Where each check runs, and the cost contract

### 9.1 Where it runs

| Check | Runs in | Why there |
|---|---|---|
| Budget before a run | The **dispatcher** (guard 2b) | It already holds the whole schedule from the due index, so the guard is free, and an over-budget run is never invoked |
| Spend count, budget trip | The **worker**, after the run | It reads the run's stream, which carries the cost, and it already writes the schedule after each run |
| Standing instructions, feedback summary | The **worker**, building the prompt | It already builds `<watch_state>` from the state it reads |
| Feedback, suggestion accept, preferences | **app-api** | They are the user's clicks |
| Grant warning | The **worker**, after the run | The run's own mint already read the grant |
| Reconnect resumes | **app-api**, in `complete_consent` | That is where the vault confirms the token |
| Action audit | The **Runtime**, in an `ExecuteToolStage` middleware, active only on unattended runs | Only the Runtime sees the tool object (its MCP annotations) and the approval decision at call time, and only there can a write be stopped before it runs |
| Connector pre-flight (held) | The **Runtime**, as a `connection_check` invocation | Resolving a schedule's tools anywhere else would copy the tool path (triage §4.1) |

### 9.2 Latency, cost and cache

- **The interactive turn path is unchanged.** No piece adds an awaited call before an ordinary turn's first token. The audit middleware is inert without a run id, and the new request fields are sent only by the harness. `test_turn_path_io_inventory` stays as is. The one attended case that pays, an approval of a scheduled run's pause answered in its conversation, is §7.2's and §12 Q14's.
- **A watch run's system prompt and `toolConfig` stay constant per deployment.** Everything per-watcher or per-run (instructions, feedback, state) is in the user message, bounded: ≤ 2,000 characters of instructions, ≤ 10 feedback entries (§3.4). The one `toolConfig` change is `report`'s two optional arguments, a deliberate rewrite at deploy, measured in its PR.
- **No piece adds a model call.** The budget reads a cost already on the stream. The grant warning reads a grant the run already read. The audit is one `PutItem` before each unattended write, and nothing before a read.
- **Per-run cost added:** at most ~700 input tokens, about $0.0001 a run on Haiku 5.5 (§3.4). Budgets, the audit and the warnings add none.

### 9.3 How each piece meets the hold, triage and Push

| Piece | The watcher hold (slice 2) | Triage (T.1, T.2, later T.5) | Push (slice 3) |
|---|---|---|---|
| Standing instructions | No effect | An edit changes `instructionsHash`, so the next run is full (§3.1). Tier 2 reads them | — |
| Feedback and suggestions | Feedback can arrive while a run waits; it touches no `version` | No effect on fingerprints | `agent_suggestion` is `fyi`, so not pushed by default |
| Budget | A held watcher spends nothing. An inbox answer runs even over budget and counts | A tier 1 quiet run costs $0. The decider's cost counts once built | The budget row is `fyi`, so not pushed by default |
| Grant warning | A held watcher makes no runs, but its answer renews the grant anyway | No effect | `today`, then `now` in its last 24 hours: the one row this slice expects Push to send |
| Reconnect resumes | No effect | A resumed schedule's next run has no change in its probe | — |
| Action audit | An approval answered from the bell records `approvedVia: inbox` | A run with a write leaves no probe (already), and is never quiet (§7.3). Probe replays are read-only and write no rows | Push's notification actions answer through the same respond route, so they are recorded the same way |
| Notification preferences | — | — | Push's sender reads them; nothing else does |

## 10. Flags

### 10.1 Behind `PROACTIVE_RUNS_ENABLED`

Everything in this slice that is new behaviour, while in development. This follows slices 1 and 2: one in-development feature, built over several PRs, with no flag per piece. While off:

- the new schedule fields are refused (as `mode` and `modelId` are);
- the dispatcher's guard 2b and the worker's spend count don't run;
- the feedback, suggestion, actions and notification-preference routes answer 404 after authentication;
- the Runtime's `HeadlessActionAudit` stays inert and writes no `RUNACT#` rows, and no watch-run write gate is built (its copy of the flag is already written only while on);
- the grant warning isn't written.

The SPA's halves are behind `features.proactiveRuns`.

**Kinds once finished:**

| Piece | Kind | Why |
|---|---|---|
| Budgets, standing instructions, feedback | Part of the proactive-runs **feature switch** | Each extends a watcher. A deployment that wants watchers wants these |
| `SCHEDULE_DEFAULT_MAX_COST_PER_DAY` | **Configuration** | A number, not an on/off |
| Notification preferences | Read by `PUSH_NOTIFICATIONS_ENABLED`'s sender (slice 3) | Inert without a channel |
| Action audit | A **rollout switch** of its own, after the dev readout: `HEADLESS_ACTION_AUDIT_ENABLED`, default on, `=false` opts out (§12 Q11, decided) | It ships ahead of the rest of slice 4, so prod's existing scheduled runs and Run now get it before proactive runs is promoted. It retires on the default condition, and its PR adds a line to #1422 |

### 10.2 Unflagged fixes

- **The runaway ceiling** (§4.5, PR 4.0). A 15-minute schedule pausing itself every day is a bug in what shipped.
- **Reconnect resumes** (§5.3). The user just connected the tool the schedule was waiting on. Today's extra Resume click is a wart in slice 0, which shipped unflagged.

## 11. PRs

| PR | Scope | What it proves on dev |
|---|---|---|
| **4.0: runaway ceiling** (unflagged) | Guard 2 uses `max(max_runs_per_day, cadence_ceiling)` in the dispatcher; the worker skips a schedule that isn't `active`; a unit test per cadence | A 15-minute dev watcher runs past 24 in a UTC day without pausing, a forced re-arm loop still trips the guard, and a schedule paused after dispatch doesn't run |
| **4.1: budget** | Harness `cost_usd` from the final `metadata` frame; `costUsd` and `costPending` on `SCHEDRUN#` (resumes add theirs) and the next run's reconcile; `maxCostPerDay`, `spendToday`, `spendTodayDate`, `budgetHoldUntil`, `budgetDaysAtLimit`; the worker's conditional count; the hold until local midnight and the pause after 3 days; dispatcher guard 2b, which always re-arms; edits that respect the hold; the short-interval rule for any schedule, and the one-time default for existing ones; `SCHEDULE_DEFAULT_MAX_COST_PER_DAY`; the bell text for `reason: budget` | A watcher with a budget of a few runs' cost stops after that many runs, with one bell row, and resumes after local midnight, and on a third day at the limit it pauses. Each watch run's `costUsd` equals the sum of its session's chat-call `C#` rows to the cent, and a timed-out run's is filled in at the next run. A null budget under 60 minutes is refused. Twenty over-budget schedules don't starve a twenty-first |
| **4.2: action audit** | `run_id` and `run_trigger` on `InvocationRequest` and `PausedTurnSnapshot`; the three classes and the catalog `writes` marker; `HeadlessActionAudit` (middleware, fail-closed, off the loop, idempotent per call); `RunAuditRecorder.record_action`; `audited` on `tool_result`; never-quiet after a `write`; `writeCount`; `GET /schedules/{id}/actions`; the interactive no-write test; the measured `PutItem` (§12 Q14); §7.5's watch-run gate and `actWithoutAsking` | A watch run approved from the bell writes one row (`approved`, `inbox`) before the call and its outcome after, and the pass that paused wrote none. One approved in the conversation writes `approvedVia: conversation`. An MCP form's replay keeps one row. A run that made a `write` and reported `nothing_new` keeps its conversation; one that made only `unclassified` calls stays quiet. In tests, a failed audit write returns the error without calling the tool, and an interactive turn writes nothing |
| **4.3: connection health** | `grant_expires_at` and `grant_issued_at` on `RunResult`; the warning rule and `expiryWarnedFor`; the `connection_expiring` kind, which resolves only when `expiresAt` clears the window, and its Sign in fallback; the `/schedules` banner; reconnect-resumes in `complete_consent` (exact provider match, budget and project rules); the logout check (§5.2), run first; re-pin at sign-in (built separately in #2518) | A grant backdated to expire in 2 days produces one bell row, and a second run produces none. Renew from an old session leaves the row asking for a sign-in; renew after a fresh sign-in resolves it. Reconnecting `github` resumes the schedule it paused and not one paused for `github-enterprise`. The logout check's answer is recorded here |
| **4.4: instructions and feedback** | `standingInstructions` and `instructionsHash` with its `skip_reason` row; `<standing_instructions>`; `report.items[].source` and `state.instructionSuggestion` (digest pin, spec tokens measured and stated); the `source` normaliser; `SCHEDFEEDBACK#` and the transactional `feedback` route; the `<watch_state>` summary; `agent_suggestion`, `pendingSuggestion` and accept (with `suggestionId`) or dismiss | A planted run of 9 Not useful marks on one source produces one suggestion within the seven-day rule. Accept changes the field, and the next run is full. An accept for a replaced suggestion gets 409. Feedback during a run doesn't fail that run's state save, and Start over keeps the counts and the pending suggestion |
| **4.5: notification preferences** | `NOTIFY_PREFS` and its routes; the bell's urgency order and badge rule | The bell orders `now` first, and `fyi` rows stop raising the badge when the user says so |
| **4.6: the web app** | The budget field with its projection and today's spend; standing instructions in the form; Not useful and Useful on `agent_item` rows; the suggestion review dialog; the Actions list; the Connections warning banner with Renew; notification preferences in Settings; `/admin/costs` *Unattended* | Each surface in the in-app browser, light and dark, at phone width |
| **4.7: unattended spend** | `runTrigger` and `runMode` on `C#` rows; the admin breakdown; `unattended.<trigger>` ledger counters | A day of dev watchers shows under *Unattended*, and the ledger's turn KPIs are unchanged |
| **Dev readout** | Seven days of the triage shadow watchers with budgets and the audit on: spend per watcher against `C#` sums, budget trips, suggestions raised and accepted, actions recorded, warnings sent | Decides whether the connector pre-flight (§5.3) is worth building (§12 Q10), and starts the audit's own rollout switch (§12 Q11) |

4.0 can merge before anyone reads the rest of this spec. 4.1 to 4.5 are independent of each other, with two exceptions: 4.2's `run_trigger` is what 4.7 tags rows with, and 4.6 follows the backend PR of each surface.

**The contracts.** No SSE event is added. The `tool_result` frame gains an optional `audited`, which the SPA, the TUI and the iOS app ignore. `InvocationRequest` gains two optional fields only the harness sends, and `PausedTurnSnapshot` records them. `GET /notifications` gains kinds (`connection_expiring`, `agent_suggestion`) and an optional `feedback` on rows; older clients ignore both, and the iOS app must skip unknown kinds, as it already skips unknown SSE events.

**Infrastructure: no new AWS resources and no new IAM statements.**

- Every row goes on an existing table, under an existing GSI.
- The Runtime already writes sessions-metadata. The worker already writes sessions-metadata and the BFF sessions table (the grant). app-api already writes all three tables it touches here.
- No Runtime environment variable is added; the flag's copy exists.
- `SCHEDULE_DEFAULT_MAX_COST_PER_DAY` is one app-api variable, which has no size budget.

The PlatformStack resource count (`docs/specs/platform-stack-resource-budget.md`) is unchanged. The held connector pre-flight (§5.3) would also add no resource.

## 12. Decisions

The developer settled these on 2026-11-27, taking the recommendation on each. Q9 stays open: the live logout check has reported (§5.2), and its recommendation waits on the developer.

1. **Default budget, the interval floor, and a per-user cap.** **Decided (Phil, 2026-11-27).** `SCHEDULE_DEFAULT_MAX_COST_PER_DAY` ships at **$0.50** a day. That is about 4.5× a Haiku 5.5 15-minute watcher ($0.11) and below a Haiku 4.5 one ($0.73). A budget is required on any schedule under 60 minutes, watch or standard. There is no per-model interval floor and no per-user daily cap: the budget bounds each schedule, and the monthly quota bounds the user (§4.2–§4.4).
2. **A run past the budget.** **Decided (Phil, 2026-11-27).** Hold until the schedule's next local midnight, with one `fyi` bell row, and pause after 3 consecutive days at the budget (§4.2).
3. **The budget's day.** **Decided (Phil, 2026-11-27).** The schedule's timezone. `runsToday` stays UTC, as it is a runaway guard, not a user number (§4.2).
4. **Where standing instructions live.** **Decided (Phil, 2026-11-27).** A field on the schedule now (§3.1). They move to a Memory Space binding when personal Memory Spaces reach runs, which is its own work in `user-markdown-memory.md`.
5. **Feedback counts.** **Decided (Phil, 2026-11-27).** They only let the model suggest an instruction change. Only the user changes instructions (§3.3).
6. **Quiet hours.** **Decided (Phil, 2026-11-27).** Per user, and `now` breaks through, which the user can turn off (`nowBreaksThrough`). Slice 4 ships the data and the bell rule; the Settings form ships with Push in slice 3 (§6).
7. **Writes without asking.** **Decided (Phil, 2026-11-27).** A **watch** run asks before every `write`-class call unless the schedule lists the tool in `actWithoutAsking`. `unclassified` calls are audited, not gated. Standard schedules are audit only (§7.5).
8. **Who sees the action audit, and for how long.** **Decided (Phil, 2026-11-27).** The owner, in full, on the schedule page. Admins see ids and counts only, through the existing audit console's scope, and never the conversation. Rows are kept **365 days**, matching `apis/shared/audit`'s retention (§7.4).
9. **Re-pin the headless grant at sign-in, and the logout finding** (§5.2). **Decided (Phil, 2026-11-27): recover, keep alive, re-pin.** The live check had confirmed that signing out of the pinned session stopped every schedule on the account, in prod as well, and that the user was told to do something that didn't fix it. Built in #2518's PR: a refused grant becomes `needs_reauth` and shows Enable again; logout keeps a token the active grant holds, and a token the grant lets go of is revoked once no session holds it; a web sign-in re-pins an active grant. The grant's own refresh token stays a later design.
10. **The connector pre-flight.** **Decided (Phil, 2026-11-27).** Held. It is built only if data shows `oauth_required` pauses are common. The grant-expiry warning (§5.1) ships regardless (§5.3).
11. **Ship the action audit ahead of the rest.** **Decided (Phil, 2026-11-27).** After the dev readout, the audit gets its own default-on **rollout switch** (`HEADLESS_ACTION_AUDIT_ENABLED=false` opts out), retired on the default condition. The PR that adds it adds a line to tracking issue #1422 (§10.1).
12. **Watch turns in the release ledger's per-turn KPIs.** **Decided (Phil, 2026-11-27).** Keep them, add the `unattended.<trigger>` counters (§8), and revisit at the first readout with watchers at scale.
13. **The runaway ceiling (PR 4.0).** **Decided (Phil, 2026-11-27).** Done: #2495 merged, and its dev check passed on 2026-11-27 (#2494).
14. **One measured `PutItem` on an attended resume.** **Decided (Phil, 2026-11-27).** Accepted. It is one `PutItem` before the first token, only on a resume of an unattended pause answered in its conversation (§7.2). The PR that builds it must state the measured number.
