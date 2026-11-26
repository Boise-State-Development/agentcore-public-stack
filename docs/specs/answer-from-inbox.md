# Answer From the Inbox — Slice 2 of the Proactive Agent Gaps

**Status:** Build plan. Nothing here is built. The developer settled every open question on 2026-11-25 (§8); §2 records the decisions, and Q1 moved the resume's drain into the scheduled-runs worker (§3.5).
**Date:** 2026-11-25
**Targets branch:** `develop`
**Parent:** `docs/specs/proactive-agent-gaps.md` (slice 2: G5, approvals answered later, from anywhere)
**Builds on:** `docs/specs/quiet-watcher.md` (slice 1: `agent_item`, `SCHEDRUN#` history, `SCHEDSTATE#`), `docs/specs/agent-handoffs-and-consults.md` §4.3 (relayed prompts, the closest existing "answer a pause somewhere else")
**Flag:** `PROACTIVE_RUNS_ENABLED` / `CDK_PROACTIVE_RUNS_ENABLED`, `features.proactiveRuns`. In development: off by default and in prod, only `"true"` enables. No new flag (§5).

---

## 1. What a user gets

A scheduled run that stops to ask the user something no longer looks finished. Examples: a watcher that drafted a reply and needs approval to send it, or a digest that asks which of two calendars to use. The run leaves one bell item: "*Inbox watcher* is waiting for you: approve *send_message*. Expires in 7 days."

**Review** opens the prompt beside the bell, the same approval card, question picker or form the conversation would show, with the full draft to inspect. **Approve**, **Decline** or an answer continues the run in the background. The item then reads "Approved · finished" and links to the conversation. The user never has to open the conversation. If nobody answers, the item says when it will expire. After that it reads "Expired", with no buttons left.

The schedule's run history shows the run as **Waiting for you**, then as what happened.

## 2. Decisions (settled 2026-11-25)

| Question | Proposal | Why |
|---|---|---|
| What does the harness call a run that paused on the user? | **`needs_user`**, a new `RunStatus`, carrying the paused interrupt ids. It is classified for every caller, not behind the flag | Today a tool-approval, question, elicitation or browser sign-in pause reads as `completed`, which is wrong for every caller (Run now's toast says "Run finished"). Each caller handles the new status in the same PR, and with the flag off the worker maps it back to exactly today's `completed` path |
| Where does the prompt's detail live? | **In the session's `PendingInterrupt` breadcrumbs, read on demand.** The inbox row carries ids and one bounded display line per prompt, never the arguments | The breadcrumbs already hold what the conversation's own card shows, and they expire with the pause. Copying a drafted email into an inbox row with a 90-day TTL would keep it 83 days longer than the pause can use it |
| What is the respond path? | **`POST /notifications/{id}/respond`** on app-api. It checks the item and the live pause under `(session, user)`, claims the item, writes the start audit row, and **async-invokes the scheduled-runs worker** with a resume payload. The worker resumes **headlessly** through `run_agent_headless(interrupt_responses=…)`, so the Runtime's `/invocations` resume route stays the only resume path (settled question 1) | The phone's notification actions (slice 3) cannot hold an SSE stream, so the answer has to drain server-side. In the worker, a resume survives an app-api deploy or crash, which would otherwise leave the item "applying" for up to 14 minutes after the user tapped Approve, with nobody watching a tab in slice 3. It also keeps all run bookkeeping (the run's record, the cursors, the history row) in one process. The gaps spec called it `/inbox/…`; the inbox router is `/notifications`, so the route lives there |
| Whose token runs the resume? | **The headless grant, renewed by the respond call.** The route pins the caller's live session into their grant before the invoke, exactly as Run now does (`_resolve_grant`), and the worker mints from it with `CognitoRefreshBearerAuth` as it does for every run | Follows from settled question 1: the worker has no caller's session, and an access token put in an async-invoke event would sit in Lambda's queue and could outlive its hour on a throttle retry. Renewing on respond keeps the original reason: an answer still works after the 30-day grant lapses, because answering renews it |
| Where is it answered in the web app? | **A review dialog opened from the bell row.** The bell stays a menu | A `cdkMenu` item can't hold form controls, and approving a send without seeing the draft is the wrong default. The dialog reuses the conversation's prompt components, so "answer in the bell" means "without opening the conversation" |
| One row per interrupt or per pause? | **One row per pause.** It lists up to 5 prompts, which are answered together in one resume | One resume per pause keeps the session's single-flight lease and the row's claim simple. Two parallel approvals in one tool batch (concurrent tool execution is on in prod) would otherwise race each other for the lease |
| What does a watcher do while its run waits? | **It holds.** It skips its runs until the pause is answered or expires | Its cursors only advance through `report` at the end of the run, so the next run would find the same item and draft the same reply again: a second approval, or worse, a second send |
| Does a resumed watch run keep `report`? | **Yes.** The snapshot records `run_mode`, and the resume rebuilds `report` as it rebuilds `client_tools` | Without it the resumed run can't save its cursors, and its `toolConfig` differs from the paused turn's, so the cached prefix is lost. Quiet-watcher §3.3 named this follow-up; this slice makes it common |

## 3. Design

### 3.1 The `needs_user` outcome (harness)

`apis/shared/harness/models.py`:

- `RunStatus` gains `"needs_user"`.
- `RunResult` gains `pending_interrupt_ids: List[str]`, from the pause frames. It also gains `error_kind: Optional[str]`, the `stream_error`'s `metadata.error_kind`. The accumulator keeps the `code` today, which for a refused resume is only `bad_request`.

`InvocationStreamAccumulator` records the `interruptId` of every `tool_approval_required`, `user_question_required`, `mcp_elicitation_required` and `browser_login_required` frame. It keeps the id only: the display text comes from the breadcrumbs (§3.3), so the harness holds no tool input.

Status precedence in `run_agent_headless`, first match wins:

```
timeout > error > oauth_required > client_tool_call > needs_user > completed
```

`oauth_required` stays first, so a schedule that needs a connector still pauses with slice 0's `schedule_paused`, unchanged.

The runner's own unread dot is set for `completed` **or** `needs_user`. That is today's behaviour exactly, since a paused run read as `completed`, and the dot is right: the conversation holds the card.

Every caller handles the status in PR 2.1:

| Caller | `needs_user` handling |
|---|---|
| Scheduled-runs worker | §3.2 |
| Run now (`apis/app_api/runs/routes.py`, `run-now.service.ts`) | The audit row says `needs_user`. The SPA's toast reads "Waiting for your answer" with Open, instead of falling through to "Run needs_user." |
| Eval harness (`apis/shared/evals/execute.py`) | Its own outcome, "paused for the user". PR 2.1 checks that no committed case relied on a pause scoring as `completed` |

### 3.2 What the worker does

| Schedule | Flag | On `needs_user` |
|---|---|---|
| Any | off | Exactly as `completed` today: `record_run_result(completed)`, no history row, no inbox row |
| Project | on | As `completed` today. A project schedule's inbox recipient is gaps open question 4, so it is out of scope here |
| Personal, standard | on | Below, no hold |
| Personal, watch | on | Below, plus the hold |

For a personal schedule with the flag on, the worker:

1. Reads the session's `META` with `load_session_meta(session_id, user_id)`: one read. It takes the breadcrumbs whose ids are in `pending_interrupt_ids`, and the `pausedTurn.expiresAt`. If there are no live breadcrumbs, or the snapshot is missing or expired, the run is recorded as `completed`. Something answered or cleared the pause first.
2. Calls `record_run_result(completed, session_id)`. The run did its work, so the failure streak resets.
3. Writes the `SCHEDRUN#` row with `status: "needs_user"`, `expiresAt` and `promptCount` (§3.7).
4. Writes one inbox row (§3.3).
5. For a watcher only, writes `pausedSessionId` and `pausedUntil` onto its `SCHEDSTATE#` item. The write is conditional on the version the run read, like every state write, and the cursors and `seen` are left alone. The session is **never soft-deleted**: a run that asked the user is not quiet.

**The hold.** Before a watch run, the worker already reads the state. When `pausedSessionId` is set and `pausedUntil` is in the future, it reads that session's `META`:

- If `derive_awaiting_input` says the pause is still live, the run is **skipped**. Nothing is invoked, and no history row is written: 96 "skipped" rows a day would bury the run that matters. The schedule page shows the wait from the state instead (§3.8).
- Otherwise, the worker clears the two fields and runs.

So a watcher costs nothing while it waits. The extra read happens only while a pause is open.

### 3.3 Inbox kinds and payload

Two kinds, as named in G4:

- `agent_needs_approval`: every prompt in the pause is a tool approval.
- `agent_needs_input`: anything else (a question, a form, a browser sign-in, or a mix).

The payload holds ids plus one display line per prompt:

```
{scheduleId, runId, runFinishedAt, sessionId, label, expiresAt,
 answerable: bool,
 prompts: [{interruptId, kind, toolName?, serverName?, headline?}]}   ≤ 5 prompts
```

| `kind` | Display fields | Answerable in the dialog |
|---|---|---|
| `tool_approval` | `toolName` (≤ 64) | Yes |
| `user_question` | `headline`: the first question's text (≤ 120), suffixed "(+2 more)" when it has more | Yes |
| `mcp_elicitation` | `serverName` (the catalog's display name, ≤ 64), `headline`: the server's message (≤ 120) | Yes |
| `browser_login` | `headline`: the agent's reason (≤ 120) | No: Open conversation (the live view is that conversation's screen) |

- **Never on the row:** tool input, question options, form fields, defaults, or any answer.
- **Text the model wrote** (a question's text, a sign-in reason) is held to the `agent_item` title's standard: one line, clipped. For whether it may go into a push payload, see gaps open question 6.
- **`answerable`** is false when any prompt can't be answered in the dialog. Such a row offers Open conversation only.
- **`runFinishedAt`** names the history row a resume updates.

The row's resolution is a new top-level `resolution` attribute, not part of the payload, so the payload stays as written:

```
resolution: {state: "applying" | "answered" | "answered_elsewhere" | "expired" | "retry",
             decision?: "approved" | "declined" | "answered" | "skipped",
             outcome?: "finished" | "waiting_again" | "failed",
             at, resumeRunId?}
```

`Notification` gains `resolution: Optional[…]`. Older clients ignore it. The iOS app doesn't read the inbox yet, and when it does it must skip unknown kinds, as it already skips unknown SSE events.

**Read state follows the answer.** Opening a needs-user row to review it does not mark it read, so the badge counts the waits until they are answered or expire. Resolving the row marks it read.

### 3.4 Reading the prompt

`GET /notifications/{id}/prompts` returns the live prompts, in the wire shape `RelayedPrompt` already uses, extended with `mcp_elicitation`'s `elicitation`. It returns them only if the row is the caller's (`INBOX#{email}`), its kind is `agent_needs_*`, and the session's `META` under `(payload.sessionId, caller.user_id)` still holds those breadcrumbs with an unexpired `pausedTurn`. That is the relay's rule (`relayed_prompts_from_row`, `derive_awaiting_input`), and the same function serves both.

Another user's session has no `META` row for this caller, so a forged id reads nothing. The full tool input is shown here, behind Inspect, as the conversation's card shows it.

| Case | Answer |
|---|---|
| Flag off | 404, after authentication |
| Not the caller's row, or not a needs-user kind | 404 |
| Pause gone | 409 `already_answered`, and the row resolves `answered_elsewhere` |
| Pause expired | 410 `expired`, and the row resolves `expired` |

### 3.5 `POST /notifications/{id}/respond`

```
POST /notifications/{notificationId}/respond
{responses: [{interruptId, response}]}
→ 202 {resumeRunId, resolution}
```

**Authentication** is `get_current_user_from_session`, so the cookie and the `Authorization: BFF` header (the phone) both work with nothing extra.

**Before the 202**, in this order:

1. **Flag and row.** Flag off → 404. Not the caller's row, or not a needs-user kind → 404. Resolved and not `retry` → 409 `already_answered`.
2. **Live pause.** The §3.4 read: gone → 409 and the row resolves `answered_elsewhere`; expired → 410 and the row resolves `expired`. The payload's `sessionId` must also be headless (`is_headless_session`). An attended conversation's pause is answered in that conversation.
3. **The answers.** Refused with 422 if any of these fail:
   - Every response names one of the row's interrupts.
   - Every prompt is covered, and the row is `answerable`.
   - No response is null.
   - Each response has its kind's shape, the one the conversation's own card sends:
     - tool approval: `"approved"` / `"declined"`;
     - question: `{answers}` / `{skipped: true}`;
     - elicitation: `{action: "accept", content}` / `{action: "decline"}` / `{action: "cancel"}`. The Runtime still checks the content against the server's schema and sends `cancel` on a mismatch.

   The shape checks reuse the Runtime's models from `apis.shared`, so the two can't drift.
4. **Claim.** One conditional `UpdateItem` sets `resolution = {state: "applying", at, resumeRunId}` and `readAt`. Its condition is no resolution, or `retry`, or a **stale** `applying`: one claimed more than 16 minutes ago that no worker has started (an invoke that never ran), or one whose worker started more than 16 minutes ago (a worker that died; its timeout is 15). This is the race guard between the bell, a second tab and, in slice 3, the phone: the loser gets 409.
5. **Grant.** `_resolve_grant` renews the caller's headless grant from the live session, as `POST /runs/now` does. With no session record and no active grant, the claim is released to `retry` and the route answers 409, as Run now does.
6. **Audit.** The start audit row is written with `trigger: "inbox_respond"` and `resumesRunId`, under the `resumeRunId`. It records the decision kinds, never the answers. That is the first per-action audit G9 asks for: who approved, and when. Fail-closed, as with Run now: a failed write releases the claim to `retry`.
7. **Invoke.** One `lambda:InvokeFunction` with `InvocationType=Event` on the scheduled-runs worker, in `asyncio.to_thread` so the boto3 call never blocks the event loop. The payload:

   ```
   {kind: "inbox_resume", userId, notificationId, resumeRunId, sessionId, scheduleId, responses}
   ```

   It carries no bearer. The answers travel in it as they would in the `/invocations` body, and the worker never logs them. If the invoke is refused, the claim is released to `retry`, the audit row ends `error`, and the route answers 503, so the buttons come back at once.

**In the worker.** `lambda_handler` branches on `kind`; a payload without one is a scheduled run, as today. For `inbox_resume`:

1. **Start mark.** One conditional `UpdateItem` on the inbox row sets `resolution.workerStartedAt`. Its condition is `resolution.state = applying`, `resolution.resumeRunId` equals the payload's, and no `workerStartedAt`. This is what makes Lambda's async retries harmless:
   - **A retry after the first attempt started** (a crash or a timeout) fails the condition. It spends no second model call: it ends the audit row `error` (`error_kind: resume_lost`) if that row is still `started`, and releases the row to `retry`, so the buttons come back without waiting 16 minutes.
   - **A retry after a throttle** (the first attempt never ran) passes, and runs normally.
   - **A late event** for a claim that was since retaken names an old `resumeRunId`, fails the condition, and is dropped.

   So the function's retry settings stay as they are, and scheduled runs keep theirs. Turning retries off would also change every scheduled run.
2. **Resume.** `run_agent_headless` with:
   - `run_id` = `resumeRunId` and `audit_started=True`, so the runner writes the end audit row onto the row app-api started;
   - `session_id` the paused one and `prompt` `""`, as the SPA's own resume sends;
   - `interrupt_responses` the answers;
   - `auth` = `CognitoRefreshBearerAuth()`, minting from the grant the route just renewed;
   - `trigger` = `"inbox_respond"`, with `mark_unread=False`.
3. **Finish.** `finish_resumed_run` (§3.6).

The Runtime does everything else as it does for the conversation's card: snapshot, expiry check, `_validate_resume_interrupts`, lease, and breadcrumb cleanup. No second resume path exists.

**The tracker works unchanged.** `GET /runs/{runId}` reads the `RUN#` audit row in sessions-metadata. app-api writes its start before the 202, and the worker's `run_agent_headless` writes its end through `GovernanceFloor.on_run_end` on the same key; the worker's role already holds read and write on that table. A dropped retry ends the row itself (step 1), so a tracker never waits on a `started` row that nothing will finish.

**Infrastructure.** app-api gains one `lambda:InvokeFunction` statement on the worker's ARN and `SCHEDULED_RUNS_WORKER_FUNCTION_NAME` in its environment, threaded from `ScheduledRunsConstruct.workerLambda` through `PlatformComputeRefs` (the construct already sets the same name on the dispatcher). It is written only while `PROACTIVE_RUNS_ENABLED` is on, like the flag's other wiring. A policy statement and a variable, not a new resource; app-api's environment has no size budget.

### 3.6 After the resume

One shared function, `apis/shared/scheduled_prompts/resume_run.py` (`finish_resumed_run`), reads the `RunResult`. Only the worker calls it, so the run's bookkeeping has one home:

| Result | Inbox row | History row | Else |
|---|---|---|---|
| `completed` | `answered`, `outcome: finished` | `resolution`, `resumeOutcome: finished`, tokens added | Unread dot. For a watcher: the resumed `report`'s cursors and tracking saved, its keys added to `seen`, the hold cleared, and **no** `agent_item` rows (settled question 3) |
| `needs_user` again | `answered`, `outcome: waiting_again` | `resumeOutcome: waiting_again`, new `expiresAt` | A new needs-user row for the new pause (§3.2 steps 1, 4, 5), pointing at the same run |
| `error`, `error_kind: resume_unavailable` | `answered_elsewhere` (reason `unknown_interrupt`) or `expired` | Unchanged | The Runtime's backstop for a race the pre-check missed |
| `error` with HTTP 409 (the lease: the user is mid-turn in that conversation), or a transport error | `retry` | Unchanged | The buttons come back. Nothing was resumed |
| Any other `error`, `timeout` | `answered`, `outcome: failed` | `resumeOutcome: failed` | Unread dot. The schedule's failure streak is **not** touched: the next scheduled run is independent of this one |
| `oauth_required` | `answered`, `outcome: waiting_again` | `resumeOutcome: waiting_again` | A needs-input row whose prompt is the consent. The schedule is not paused: its next run is a fresh session |

A resume never pauses or resumes the schedule, and never moves `lastRunSessionId`.

**Answered in the conversation instead.** That resume goes through `/chat/stream`, and nothing reads its outcome. Two things follow:

1. **The inbox row.** `GET /messages` of a headless session, with the flag on, resolves that session's open needs-user rows as `answered_elsewhere` once the pause is gone. This is one filtered `Query` on the caller's inbox, the way handoffs mark their entries read on open. It runs only for headless sessions and never on the turn path. The worker's hold check (§3.2) independently sees the pause is gone.
2. **The watcher's state.** A watch run resumed there still has `report` (§3.7), but no worker reads it, so its cursors don't advance. The next run may raise that finding once. This is accepted for this slice, and measured on dev (settled question 7, §7).

### 3.7 `report` on a resumed watch run (Runtime)

- `PausedTurnSnapshot` gains `run_mode`, written when a watch run pauses.
- The resume path rebuilds `report` when `snapshot.run_mode == "watch"`, the session is headless, and the Runtime's copy of the flag is on. It is rebuilt in the same slot `_build_turn_tools` appends it to, so `toolConfig` is byte-identical to the paused turn's.
- Nothing is read to do it: the snapshot is already loaded on resume. A watch run's agent is still never cached.

### 3.8 Run history and the 24-hour summary

The `SCHEDRUN#` row gains these fields:

```
status: "needs_user"   (new value)
expiresAt              the pause's, while it waits
promptCount
resolution             approved | declined | answered | answered_elsewhere
resumeOutcome          finished | waiting_again | failed
resumeRunId, resumedAt
```

The resumed turn's tokens are **added** to the row, so a run's cost stays one row.

`expired` is derived on read (`needs_user`, no resolution, `expiresAt` passed). No process runs at expiry, and none needs to.

**Labels** (`runStatusLabel` gains a resolution argument):

| Row | Label |
|---|---|
| Waiting | Waiting for you · expires Tue 3:40 PM |
| Expired | Expired |
| Resolved | Approved · finished, Declined · finished, Answered · failed, Approved · waiting for you again, Answered in the conversation |

**The 24-hour summary** (`lastDayRunSummary`):

- A `needs_user` run is a finished run, or a finished check for a watcher. It did its work, so it is never counted with the failures. Today it would fall into "· 1 failed".
- For a watcher, it also counts as "found something". It found something it needed the user for.
- Waiting runs add a tail: "Checked 22 times · found something 2 times · 1 waiting for you".

While a watcher holds, its schedule page shows a banner from `SCHEDSTATE#`: "Waiting for you since 9:15 AM. Checks resume once you answer, or on Dec 2 when it expires." It has a Review button that opens the same dialog. `GET /schedules/{id}/state` returns the two fields.

**Expiry, end to end.** No surface ever offers a dead button:

| Surface | Before expiry | After expiry |
|---|---|---|
| Bell row | "Expires in 6 days", then "in 3 hours" | "Expired", no buttons, Open conversation only. Derived from `payload.expiresAt` on the client, so it is right before any server write |
| Server | — | `GET /notifications` resolves the page's unresolved needs-user rows past `expiresAt` as `expired`, which is at most 20 conditional writes and usually none. `unread_count` filters out rows past `payload.expiresAt`, so an expired wait never holds the badge up |
| Respond and prompts | — | 410 |
| History | Waiting | Expired |

### 3.9 Web app

- **Bell rows.** Two new descriptions:
  - `describeAgentNeeds`: "*{label}* is waiting for you: approve *{toolName}*", "…: *{headline}*" or "…: 2 things to answer";
  - a resolution suffix: "Approved · finished", "Answered in the conversation", "Expired".

  Clicking an answerable, unresolved row opens the **review dialog**. Any other row opens the conversation. The descriptions are not flag-gated, like slice 1's; the Review action is, behind `features.proactiveRuns`.
- **The review dialog** (CDK Dialog, focus-trapped).
  - It fetches `GET /notifications/{id}/prompts` and renders each prompt with the conversation's own components: `ToolApprovalPromptComponent` (with Inspect for the full arguments), `UserQuestionPromptComponent` and `McpElicitationPromptComponent`. A form renders only where `features.mcpElicitation` is on, and otherwise offers Open conversation and Decline.
  - It collects one answer per prompt and posts them together. Send is enabled once every prompt is answered.
  - It names the run, its schedule and the expiry, and always offers Open conversation.
  - 409 and 410 close it onto the resolved row: "Already answered" or "Expired".
- **Progress.** The 202's `resumeRunId` goes to the existing background-task tracker that Run now uses (`GET /runs/{runId}`), which toasts the outcome. The bell refreshes when the tracker settles.
- **The schedule page.** The banner (§3.8), the new labels, and the summary tail.
- **Run now's toast** for `needs_user` (§3.1), unflagged.

The bell never shows tool input or answers outside the dialog. The dialog shows them only from the live breadcrumb, over the session cookie.

## 4. Latency, cost and cache

- **The interactive turn path is unchanged.** No new awaited call happens before the first token. A web or terminal turn's snapshot never carries `run_mode`, so the resume path's only addition is a constant spec appended with no IO, on watch-run resumes only. `test_turn_path_io_inventory` stays as is. The respond and prompts routes, the list's lazy expiry and the `GET /messages` resolution are all off the turn path.
- **A resume continues the same session, so its prefix is unchanged.** The system prompt, `toolConfig` (with `report` rebuilt, §3.7) and history up to the paused `toolUse` are byte-identical to the paused turn's, and it reads from cache if the cache is still warm.
  - An inbox answer usually comes after the cache TTL, so the resume pays **one prefix re-write** at the write premium: 1.25× the model's base input rate at the 5-minute TTL. That is the same as any idle return, and the release ledger already counts it.
  - For the slice 1 watcher on Haiku 4.5, that prefix was about 4.3k tokens, about $0.007 (quiet-watcher §6). It scales with the run's history and its model, and is priced from `curated-models.ts` for the model in play.
  - Without §3.7 the resume would always miss, because `report` would be gone from `toolConfig`. It would also lose the cursors.
- **A held watcher costs nothing.** No model call happens while it waits, and only one extra `META` read per tick while a pause is open.
- **The inbox stays small.** It holds ids plus at most five 120-character lines per row. Prompt detail is read from the breadcrumb and goes when the pause goes.
- **No new resources; one IAM statement.** app-api gains `lambda:InvokeFunction` on the scheduled-runs worker and that function's name as a variable, written only while the flag is on (§3.5). app-api's environment has no size budget. Everything else is already granted: app-api reads and writes the projects table (inbox) and sessions-metadata (`META`, `RUN#`), and the worker already reads and writes both, mints from the grant and calls the Runtime. The Runtime gains no environment variable.
- **The respond call adds one async invoke**, off every turn path. A Lambda cold start sits between Approve and the run's first token; nobody watches that stream, and the tracker shows Applying meanwhile.

## 5. Flag

The flag is `PROACTIVE_RUNS_ENABLED`, which slice 1 already wires to app-api, both scheduled-runs Lambdas and the Runtime (the Runtime copy is written only while on). This slice adds no flag. It is one more piece of the in-development proactive-runs feature, and CLAUDE.md flags in-development work, not each piece of it.

While off:

- the worker treats `needs_user` as `completed` (§3.2);
- the two kinds are never written;
- `respond` and `prompts` 404 after authentication;
- the Runtime never writes `run_mode` onto a snapshot or rebuilds `report`.

The SPA's Review action is behind `features.proactiveRuns`.

**Not behind the flag:** the harness's `needs_user` classification, the runner's unread dot (unchanged in effect), Run now's toast and the eval mapping. They correct a misclassification that every caller sees today. Gating a fix would leave Run now saying "Run finished" for a run that is waiting.

## 6. PRs

| PR | Scope | Behaviour while the flag is on |
|---|---|---|
| **2.1: outcome and inbox rows** | `needs_user`, `pending_interrupt_ids` and `error_kind` in the harness; Run now's toast and the eval mapping; the worker's §3.2 branch, the hold, and the `SCHEDSTATE#` fields; the two kinds, `Notification.resolution`, the list's lazy expiry and the `unread_count` filter; the bell's descriptions (Open conversation only); this spec | A paused run lands in the bell and in the history as Waiting, and a watcher holds. The answer is still given in the conversation, so opening the row marks it read as any row does: nothing resolves it on an answer until 2.2's `GET /messages` resolution, and without that a wait answered in the conversation would hold the badge up for 7 days. 2.2 moves read state to follow the answer (§3.3) |
| **2.2: respond** | `GET /notifications/{id}/prompts`; `POST /notifications/{id}/respond` (claim, grant renewal, audit, async invoke); the worker's `inbox_resume` branch (start mark, retry and late-event no-ops, resume) and `finish_resumed_run`; the history row update; the `GET /messages` resolution; app-api's `InvokeFunction` statement and worker-name variable, with a CDK test that both exist only while the flag is on; the Runtime's snapshot `run_mode` and `report` rebuild, with a digest test showing the resumed `toolConfig` equals the paused one | Answerable through the API. Tested with moto and a fake Runtime stream for every row of §3.6, plus a retried event that spends no second call and a dropped one that ends its audit row |
| **2.3: the web app** | The review dialog, resolution text, expiry countdown, the history labels and summary tail, the schedule banner, the tracker hand-off | Approve, Decline and answer in the bell |
| **Dev readout** | See §7 | Decides whether slice 3 (push) can build on it unchanged |

The SSE contract is unchanged, so the TUI and iOS need nothing. `GET /notifications` gains two kinds and an optional field, which older clients ignore.

## 7. Dev readout

On dev with the flag on, using a personal watcher whose prompt drafts a reply through a tool its catalog row marks `needs_approval` (a dev connector's write tool):

1. **Approve from the bell.** The run finishes, the history reads "Approved · finished", the watcher's cursors advance, and the next run is quiet.
2. **Decline from the bell.** The model sees the cancellation and replans.
3. **A question pause** from a schedule that uses `ask_user_question`, answered and then skipped.
4. **The race.** Answer in the conversation first, then in the bell (409 "Already answered"). Answer in the bell while that conversation is mid-turn (`retry`).
5. **Expiry**, forced by editing the snapshot's `expiresAt` on dev. The row reads "Expired", respond answers 410, and the watcher resumes checking.
6. **Answered in the conversation.** Count, over the dev run, how often a watch run's pause was answered in its conversation and the next run raised the same finding again (settled question 7).
7. **Cost.** The resume's `C#` row has `cacheStatus`, with `toolConfigHash` and `systemPromptHash` equal to the paused call's. State the resume's tokens and dollars, and the held watcher's zero model calls.

## 8. Settled questions

The developer answered all eight on 2026-11-25. Only the first changed the design.

1. **Drain in the worker Lambda, not app-api.** It is the better experience: a resume lost to an app-api deploy or crash would leave the item "applying" for up to 14 minutes after the user tapped Approve, and in slice 3 a phone action has nobody watching a tab. It also keeps all run bookkeeping (recording the run, advancing cursors) in the worker, so `finish_resumed_run` has one home. The respond route claims, audits and async-invokes the worker (§3.5); app-api gains one `InvokeFunction` statement and the worker's name (§4); a retried event never spends a second model call (§3.5, the start mark); and `GET /runs/{runId}` works unchanged, because the worker ends the audit row app-api started. **Follow-on:** the resume runs on the headless grant, renewed by the respond call from the caller's live session, rather than on the caller's access token, which an async event should not carry (§2).
2. **Hold, as proposed.** A watcher runs no checks while its run waits, up to the pause TTL.
3. **After a resume, no `agent_item` rows.** The unread conversation is enough.
4. **Run now: toast only, no bell row.**
5. **Project schedules stay out of slice 2.**
6. **One resume per pause.** Every prompt in a pause is answered together.
7. **Answers given in the conversation:** accept the one-time duplicate for this slice, and measure on dev how often it happens (§7).
8. **Model-written text on the bell row,** at most 120 characters: confirmed.
