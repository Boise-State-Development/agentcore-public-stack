# Answer From the Inbox — Slice 2 of the Proactive Agent Gaps

**Status:** Built and dev-validated: slice 2 is done. PR 2.1 (#2460), PR 2.2 (#2469), PR 2.3, the web app's review dialog (#2471), the history fix (#2475) and the resume-tools fix (#2478) are merged, and the dev readout is in §7. Two findings from it are fixed: two answers to one pause both resuming it (#2486, the pause's claim) and a pause answered in its conversation re-raising its finding (#2490). The developer settled every open question on 2026-11-25 (§8); §2 records the decisions, and Q1 moved the resume's drain into the scheduled-runs worker (§3.5).
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
| What about a pause answered in its conversation instead? | **Its `report` is saved too (#2490).** The Runtime records it on the session's `META` row as the pause ends; the worker's next hold check saves it and ends the hold in one conditional write (§3.6 point 2) | Settled question 7 first accepted the next run raising the finding again. The readout saw that every time, and for an approval it asks to repeat a write already made |

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
   {kind: "inbox_resume", userId, email, notificationId, resumeRunId, sessionId, scheduleId, responses}
   ```

   `email` names the inbox the worker settles (`INBOX#{email}`): the worker has no user lookup, and the schedule may be deleted meanwhile. It carries no bearer. The answers travel in it as they would in the `/invocations` body, and the worker never logs them. If the invoke is refused, the claim is released to `retry`, the audit row ends `error`, and the route answers 503, so the buttons come back at once.

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
| `error`, `error_kind: resume_unavailable` | `answered_elsewhere` (reason `unknown_interrupt` or `already_resumed`) or `expired` | `resolution: answered_elsewhere`, `expiresAt` cleared (the bell's list never reaches a row settled here, found on dev 2026-11-27); unchanged when `expired` | The Runtime's backstop for a race the pre-check missed. `already_resumed` is the pause's claim: another answer, from the conversation or a second tab, reached the pause first and is running it |
| `error` with HTTP 409 (the lease: the user is mid-turn in that conversation), a transport error, or no bearer could be minted | `retry` | Unchanged | The buttons come back. Nothing was resumed. As built, a resume takes the lease with `force`, so it never gets this 409. Two answers to one pause are settled by the pause's claim instead (below), which refuses the second as `already_resumed` |
| Any other `error`, `timeout` | `answered`, `outcome: failed` | `resumeOutcome: failed` | Unread dot. The schedule's failure streak is **not** touched: the next scheduled run is independent of this one |
| `oauth_required` | `answered`, `outcome: waiting_again` | `resumeOutcome: waiting_again` | A needs-input row whose prompt is the consent (`kind: "oauth"`, `serverName` the provider, headline "Connect {provider} to continue"), not answerable in the dialog. The schedule is not paused: its next run is a fresh session |

A resume never pauses or resumes the schedule, and never moves `lastRunSessionId`.

**One resume per pause (#2486).** Every resume, from any path, claims its pause before it takes the lease: one conditional `UpdateItem` on the `META` row the preamble already read stamps an owner on `pausedTurn` (`claim_paused_turn`) and returns the snapshot, replacing the snapshot read a resume made before, so it adds no round trip. A second resume of the same pause loses the condition and is refused as `resume_unavailable`, reason `already_resumed`, before it touches the lease, the agent or the conversation. The conversation shows "This was already answered", and the worker settles the bell's row `answered_elsewhere`. Nothing is retired on that refusal: the snapshot and breadcrumbs belong to the resume that is running. The claim ends with the pause (a finished resume clears `pausedTurn`, a re-paused one replaces it), is released by a resume that ends any other way, and lapses after 15 minutes, so a resume that died never strands the pause.

**Answered in the conversation instead.** That resume goes through `/chat/stream`, and nothing reads its outcome. Two things follow:

1. **The inbox row and the run's history row.** `GET /notifications`, with the flag on, settles the page's waiting needs-user rows whose pause is gone as `answered_elsewhere`, and lapsed ones as `expired`, each marked read (`settle_unanswered_rows`). It reads one `META` per row still waiting, under `(session, caller)`, all at once, usually none; the write is conditional on no resolution, so an inbox answer that landed meanwhile keeps its own. As built, this replaced the planned `GET /messages` hook: that read happens when the conversation opens, *before* the user answers there, so it could never see the answer, while the bell re-reads its list on open and on refocus, after the answer, and the badge comes from the same call. Nothing runs on the turn path. The worker's hold check (§3.2) independently sees the pause is gone. A row settled `answered_elsewhere` (here, by the 409 on prompts or respond, or by the worker when the Runtime refuses its resume) also marks its run's `SCHEDRUN#` row `resolution: answered_elsewhere` and clears its `expiresAt` (`record_schedule_run_answered_elsewhere`), one conditional write on no resolution, so the history reads "Answered in the conversation" instead of "Waiting for you" until it expired (found on dev, 2026-11-26). An expired row writes nothing there: the history row reads as expired from its own `expiresAt`.
2. **The watcher's state (#2490).** A watch run resumed there still has `report` (§3.7), and its cursors now advance as a bell resume's do. The Runtime can't tell the two resumes apart (the worker's request is the browser's) and doesn't know the schedule, so for **every** finished watch-run resume it writes that leg's last `report` input as `watchResumeReport` onto the session's `META` row, in the same update that clears `pausedTurn` (`clear_paused_turn(watch_resume_report=…)`), after the stream (`scheduled_prompts/conversation_resume.py`). **The hold is the token.** A bell resume ends the hold in its own save (`save_watch_state(clear_pause=True)`), so nothing ever reads the record it left. A conversation resume leaves the hold, so the watcher's next tick finds the pause gone and the record there, and its hold check (`_watcher_holds`) saves that report and ends the hold in **one** conditional write on the state version it read (`apply_conversation_resume`). Then the run starts from the saved state, past the finding. So the report is saved once whichever path answered, a state that moved on (another tick, "Start over") is never written over, and, as with a bell resume (settled question 3), no `agent_item` is raised for what the user saw in the conversation. The record is read only while the hold is unexpired, like the pause itself. A watcher ticks every few minutes against a hold of days, so an answer is seen long before its hold lapses. Measured on dev before the fix, 2 of 2 such pauses re-raised (§7 row 6).

### 3.7 The resumed run's tools (Runtime)

- `PausedTurnSnapshot` gains `run_mode`, written when a watch run pauses, and `project_id`, written when a project harness pauses (the memory binding names the project only while memory spaces are on).
- The resume builds **every** injected tool family through the turn's own `_build_turn_tools` (`_build_resume_tools`), from the snapshot's effective tool ids, `assistant_id`, memory binding, `project_id` and `run_mode`, then appends the client tools, as the turn does. `report` comes from `run_mode` in its slot, last of the families. `toolConfig` is byte-identical to the paused turn's (`tests/apis/inference_api/test_resume_tool_parity.py`).
- `_resolve_effective_tools` is not run again: the snapshot holds its answer, and re-running it would read the catalog and the session's files and could move `toolConfig` if an admin pin changed during the pause.
- Nothing new is read: the one read is the `document_read` gate, which the resume already made for its cache key. A watch run's agent is still never cached, and a resumed watch run makes no tool-summary side call, as its first leg made none.
- The rebuilt agent re-records `run_mode`, `project_id` and the client tools, so a second pause snapshots the same shape.
- **Found on dev, 2026-11-26.** 2.2 first rebuilt only the client tools and `report`. A watch run whose agent was never cached therefore resumed without `my_skill_create`, and the approved call came back "Unknown tool". The changed tool list also re-wrote the prefix: a 7,951-token cache write on the pause, then 4,873 tokens written and 0 read on its resume. Any resume whose paused agent had left the Runtime's cache took the same path.

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
- For a watcher, it does **not** count as "found something". While it waits it is in the tail, and counting it as a finding too would count it twice (dev, 2026-11-26: two paused checks that found nothing read "found something 2 times · 1 waiting for you"). Once answered, `resumeOutcome` can't say whether the resume found anything new.
- Waiting runs add a tail: "Checked 22 times · found something 1 time · 1 waiting for you".

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

- **The interactive turn path is unchanged.** No new awaited call happens before the first token. A web or terminal turn's snapshot never carries `run_mode`, so the resume path's only addition is a constant spec appended with no IO, on watch-run resumes only. `test_turn_path_io_inventory` stays as is. The respond and prompts routes and the list's lazy expiry and settling are all off the turn path.
- **A resume continues the same session, so its prefix is unchanged.** The system prompt, `toolConfig` (with `report` rebuilt, §3.7) and history up to the paused `toolUse` are byte-identical to the paused turn's, and it reads from cache if the cache is still warm.
  - An inbox answer usually comes after the cache TTL, so the resume pays **one prefix re-write** at the write premium: 1.25× the model's base input rate at the 5-minute TTL. That is the same as any idle return, and the release ledger already counts it.
  - For the slice 1 watcher on Haiku 4.5, that prefix was about 4.3k tokens, about $0.007 (quiet-watcher §6). It scales with the run's history and its model, and is priced from `curated-models.ts` for the model in play.
  - Without §3.7 the resume would always miss, because `report` would be gone from `toolConfig`. It would also lose the cursors.
- **A held watcher costs nothing.** No model call happens while it waits, and only one extra `META` read per tick while a pause is open.
- **The inbox stays small.** It holds ids plus at most five 120-character lines per row. Prompt detail is read from the breadcrumb and goes when the pause goes.
- **No new resources; one IAM statement.** app-api gains `lambda:InvokeFunction` on the scheduled-runs worker and that function's name as a variable, written only while the flag is on (§3.5). The statement goes in app-api's existing worker-invoke managed policy (`AppApiMemoryMaintenanceInvokePolicy`), never on the task role: a role statement lands in CDK's overflow policies, which it packs by an estimate, and moving statements between them is how an earlier deploy saw AccessDenied. app-api's environment has no size budget. Everything else is already granted: app-api reads and writes the projects table (inbox) and sessions-metadata (`META`, `RUN#`), and the worker already reads and writes both, mints from the grant and calls the Runtime. The Runtime gains no environment variable.
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
| **2.1: outcome and inbox rows** | `needs_user`, `pending_interrupt_ids` and `error_kind` in the harness; Run now's toast and the eval mapping; the worker's §3.2 branch, the hold, and the `SCHEDSTATE#` fields; the two kinds, `Notification.resolution`, the list's lazy expiry and the `unread_count` filter; the bell's descriptions (Open conversation only); this spec | A paused run lands in the bell and in the history as Waiting, and a watcher holds. The answer is still given in the conversation, so opening the row marks it read as any row does: nothing resolves it on an answer until 2.2's list settling, and without that a wait answered in the conversation would hold the badge up for 7 days. 2.2 moves read state to follow the answer (§3.3) |
| **2.2: respond** | `GET /notifications/{id}/prompts`; `POST /notifications/{id}/respond` (claim, grant renewal, audit, async invoke); the worker's `inbox_resume` branch (start mark, retry and late-event no-ops, resume) and `finish_resumed_run`; the history row update; read state that follows the answer: the list settles waits answered in the conversation (§3.6, in place of the planned `GET /messages` hook), and the bell no longer marks a waiting row read when it is opened (§3.3); app-api's `InvokeFunction` statement and worker-name variable, with a CDK test that both exist only while the flag is on; the Runtime's snapshot `run_mode` and `report` rebuild, with a digest test showing the resumed `toolConfig` equals the paused one | Answerable through the API. A wait answered in its conversation leaves the badge on the bell's next read. Tested with moto and a fake Runtime stream for every row of §3.6, plus a retried event that spends no second call and a dropped one that ends its audit row |
| **2.3: the web app** | The review dialog, resolution text, expiry countdown, the history labels and summary tail, the schedule banner, the tracker hand-off. As built: each prompt renders in a slot that provides its own approval, question and form services, so the conversation's cards are reused unchanged and their resume handlers only hand the answer up. One prompt sends on its own answer; several wait for Send, each answered one collapsing to a line with Change. A 409 or 410 closes the dialog with a toast and shows the row settled. The tracker is Run now's poll (`pollRunUntilDone`), and when it settles the bell re-reads, so a run the worker gave back reads "nothing changed, answer again". The banner's Review opens the dialog only when the bell's page holds the row, and otherwise links to the conversation. It hides once the inbox shows the wait settled, ahead of the worker clearing the hold. With `features.mcpElicitation` off, a form can still be declined from the dialog | Approve, Decline and answer in the bell |
| **Dev readout** | See §7 | Decides whether slice 3 (push) can build on it unchanged |

The SSE contract is unchanged, so the TUI and iOS need nothing. `GET /notifications` gains two kinds and an optional field, which older clients ignore.

## 7. Dev readout

Run on dev on 2026-11-26 and 2026-11-27, with the flag on, after #2478 (Runtime build `09df16c1e25cb088`). The watchers ran on Claude Haiku 5.5. No connector write tool registers on a headless run (LMS Middleware forwards a token, and the rest need OAuth), so the approval pause came from `my_skill_create`: its catalog row asks for approval, and the schedule names it in `enabledTools`. The question pause came from `ask_user_question`. Each prompt keyed its one finding on a cursor (`skill`, `calendar`), so the next run is quiet only if the resume's `report` was saved. Runs were forced by backdating `nextRunAt` and invoking the dispatcher. Items 1 to 5 also draw on the checks recorded for PRs 2.2, 2.3 and #2478.

| # | Check | Result |
|---|---|---|
| 1 | **Approve from the bell** | Passes. #2478's check approved a watch run and a standard run whose agent had gone cold (resumed after the 900 s idle timeout). Both created the skill with the same `toolConfigHash` across the pause. In this readout, watcher B's approval resumed within 5 s. Its history reads `approved`, `finished`, the resumed `report` saved the cursor, and the **next run was `quiet`**, with no inbox row |
| 2 | **Decline from the bell** | Passes (2.2's check). The resume ends cleanly, the model is told the call was declined, and the resume read the whole prefix from cache with the same `toolConfigHash`. The dialog's toast reads "Declined · finished" (2.3's check) |
| 3 | **A question, answered and skipped** | Passes. Answered: 2.2's check, and the dialog's toast reads "Answered · finished" (2.3's). Skipped (watcher A): `{skipped: true}` resumed and finished, the history reads `skipped`, `finished`, the model saved the cursor as `skipped`, and the next run was `quiet` |
| 4 | **The race** | Mostly passes, with one bug, **#2486**. Answering in the conversation first and then in the bell gives 409 `already_answered` (2.2's check), as do two simultaneous bell answers (one 202, one 409) and an answer after the run. **"Mid-turn" can't arise as §3.6 describes it**, because every resume takes the session lease with `force=True`. A lease row written to stand in for a live turn (watcher C) was simply taken over, and the bell's answer finished. The real mid-turn case is a fresh message sent in the paused conversation, and that message ends the pause at the start of its turn. A bell answer sent while that turn streamed (watcher F) got 409 `already_answered`, and the inbox and history rows settled as `answered_elsewhere`. **The bug:** a bell answer followed 50 ms later by an answer in the conversation (watcher G) resumed the turn **twice at once**. Both runs started from the same history, giving two pairs of model calls and two final answers ("Home", then "Work"). The stored history now has two `toolResult`s for one `toolUse`. The next turn still ran, but the model had to correct itself, and the watcher kept the bell's answer while the conversation said the other. Two tabs answering one card already had this race. The bell widens it to the 5–8 s before the worker starts, and slice 3 adds a phone. The `retry` row of §3.6 can't occur. **Fixed by the pause's claim (§3.6, "One resume per pause")**: the second answer is refused as `already_resumed` before it runs |
| 5 | **Expiry** | Passes for the row (2.2's check: `payload.expiresAt` backdated, the list settles it `expired` and read, prompts and respond answer 410; 2.3's check: the bell reads "Expired"). **Not checked: the watcher resuming.** The hold follows the snapshot's own `expiresAt`, which no check moved |
| 6 | **Answered in the conversation, then re-raised** | **n = 2, both re-raised.** D (an approval) and E (a question) were answered in their conversations. Their next runs paused again on the same step: D asked to create the same skill again, and E asked the same question. That follows from settled question 7: the conversation's resume still calls `report`, but no worker saves it, so the cursor never advances. Every watch-run pause answered in its conversation therefore re-raises once, and the re-raise is a **second pause**, not a duplicate `agent_item`. For an approval, that means asking to repeat a write the user already approved: approving it again would repeat the write. The bell showed both re-raises, and the history row of each earlier run read "Answered in the conversation" (#2475). **Fixed in #2490:** the worker now saves the conversation's `report` at its next hold check (§3.6 point 2) |
| 7 | **Cost** | Every resume's `C#` rows keep the paused call's `toolConfigHash` and `systemPromptHash`. The bell resumes here came 35–44 s after their pauses, inside the 5-minute TTL, so they read as `hit`: two calls each, 13.5k–14.8k tokens read, 0.4k–0.9k written, 225–368 output, **$0.00032–$0.00048 a resume** at Haiku 5.5's rates. #2478's watch resume read 7,067 tokens from cache and wrote 558. Its standard resume came 18 minutes later and read as `miss_ttl_expired`, the normal case for an inbox answer: a resume the size of B's then re-writes about 7.7k tokens at $0.1375/MTok, **about $0.0013**. No resume ran a title or tool-summary side call. **A held watcher costs nothing:** D, forced while its second pause waited, logged "holding while an earlier run waits on the user", took 111 ms, and wrote no history row and no `C#` row |

**What slice 3 can build on.** The respond contract, the inbox kinds and the read state hold as built, so push can call them unchanged. The race that #2486 named is fixed by the pause's claim (§3.6), which holds whichever path answers first, a phone included.

This readout's seven watchers cost about $0.018, more than half of it the 600-word turn sent to watcher F ($0.0088). The schedules and the two test skills are deleted; their conversations are kept.

## 8. Settled questions

The developer answered all eight on 2026-11-25. Only the first changed the design then; the dev readout later reversed the seventh (#2490).

1. **Drain in the worker Lambda, not app-api.** It is the better experience: a resume lost to an app-api deploy or crash would leave the item "applying" for up to 14 minutes after the user tapped Approve, and in slice 3 a phone action has nobody watching a tab. It also keeps all run bookkeeping (recording the run, advancing cursors) in the worker, so `finish_resumed_run` has one home. The respond route claims, audits and async-invokes the worker (§3.5); app-api gains one `InvokeFunction` statement and the worker's name (§4); a retried event never spends a second model call (§3.5, the start mark); and `GET /runs/{runId}` works unchanged, because the worker ends the audit row app-api started. **Follow-on:** the resume runs on the headless grant, renewed by the respond call from the caller's live session, rather than on the caller's access token, which an async event should not carry (§2).
2. **Hold, as proposed.** A watcher runs no checks while its run waits, up to the pause TTL.
3. **After a resume, no `agent_item` rows.** The unread conversation is enough.
4. **Run now: toast only, no bell row.**
5. **Project schedules stay out of slice 2.**
6. **One resume per pause.** Every prompt in a pause is answered together.
7. **Answers given in the conversation:** accept the one-time duplicate for this slice, and measure on dev how often it happens (§7). **Superseded:** the readout found it every time (§7 row 6), and for an approval it asks to repeat a write already made, so it is fixed (#2490, §3.6 point 2): the worker saves the conversation's `report` at its next hold check.
8. **Model-written text on the bell row,** at most 120 characters: confirmed.
