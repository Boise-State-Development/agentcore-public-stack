# Answer From the Inbox — Slice 2 of the Proactive Agent Gaps

**Status:** Draft build plan. Nothing here is built. The decisions in §2 are proposed; §8 lists what the developer still has to settle.
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

## 2. Decisions (proposed 2026-11-25)

| Question | Proposal | Why |
|---|---|---|
| What does the harness call a run that paused on the user? | **`needs_user`**, a new `RunStatus`, carrying the paused interrupt ids. It is classified for every caller, not behind the flag | Today a tool-approval, question, elicitation or browser sign-in pause reads as `completed`, which is wrong for every caller (Run now's toast says "Run finished"). Each caller handles the new status in the same PR, and with the flag off the worker maps it back to exactly today's `completed` path |
| Where does the prompt's detail live? | **In the session's `PendingInterrupt` breadcrumbs, read on demand.** The inbox row carries ids and one bounded display line per prompt, never the arguments | The breadcrumbs already hold what the conversation's own card shows, and they expire with the pause. Copying a drafted email into an inbox row with a 90-day TTL would keep it 83 days longer than the pause can use it |
| What is the respond path? | **`POST /notifications/{id}/respond`** on app-api. It checks the item and the live pause under `(session, user)`, claims the item, and resumes **headlessly** through `run_agent_headless(interrupt_responses=…)`, so the Runtime's `/invocations` resume route stays the only resume path | The phone's notification actions (slice 3) cannot hold an SSE stream, so the answer has to drain server-side. That is the same shape as Run now. The gaps spec called it `/inbox/…`; the inbox router is `/notifications`, so the route lives there |
| Whose token runs the resume? | **The caller's own session access token**, the same one `/chat/stream` forwards, not the headless grant | The user is present and answering. An answer must still work after the 30-day headless grant lapses, which is when a stale pause is most likely |
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
4. **Claim.** One conditional `UpdateItem` sets `resolution = {state: "applying", at, resumeRunId}` and `readAt`. Its condition is no resolution, or `retry`, or `applying` older than the harness's 14-minute cap, which is a resume lost to a task restart. This is the race guard between the bell, a second tab and, in slice 3, the phone: the loser gets 409.
5. **Audit.** The start audit row is written with `trigger: "inbox_respond"` and `resumesRunId`. It records the decision kinds, never the answers. That is the first per-action audit G9 asks for: who approved, and when. Fail-closed, as with Run now.

**After the 202**, `spawn_run` runs `run_agent_headless` in the background:

- `session_id` is the paused one and `prompt` is `""`, as the SPA's own resume sends.
- `interrupt_responses` carries the answers.
- `auth` is `StaticBearerAuth` with the caller's access token.
- `trigger` is `"inbox_respond"`, with `mark_unread=False`.

The Runtime does everything else as it does for the conversation's card: snapshot, expiry check, `_validate_resume_interrupts`, lease, and breadcrumb cleanup. No second resume path exists.

### 3.6 After the resume

One shared function, `apis/shared/scheduled_prompts/resume_run.py` (`finish_resumed_run`), reads the `RunResult`:

| Result | Inbox row | History row | Else |
|---|---|---|---|
| `completed` | `answered`, `outcome: finished` | `resolution`, `resumeOutcome: finished`, tokens added | Unread dot. For a watcher: the resumed `report`'s cursors and tracking saved, its keys added to `seen`, the hold cleared, and **no** `agent_item` rows (open question 3) |
| `needs_user` again | `answered`, `outcome: waiting_again` | `resumeOutcome: waiting_again`, new `expiresAt` | A new needs-user row for the new pause (§3.2 steps 1, 4, 5), pointing at the same run |
| `error`, `error_kind: resume_unavailable` | `answered_elsewhere` (reason `unknown_interrupt`) or `expired` | Unchanged | The Runtime's backstop for a race the pre-check missed |
| `error` with HTTP 409 (the lease: the user is mid-turn in that conversation), or a transport error | `retry` | Unchanged | The buttons come back. Nothing was resumed |
| Any other `error`, `timeout` | `answered`, `outcome: failed` | `resumeOutcome: failed` | Unread dot. The schedule's failure streak is **not** touched: the next scheduled run is independent of this one |
| `oauth_required` | `answered`, `outcome: waiting_again` | `resumeOutcome: waiting_again` | A needs-input row whose prompt is the consent. The schedule is not paused: its next run is a fresh session |

A resume never pauses or resumes the schedule, and never moves `lastRunSessionId`.

**Answered in the conversation instead.** That resume goes through `/chat/stream`, and nothing reads its outcome. Two things follow:

1. **The inbox row.** `GET /messages` of a headless session, with the flag on, resolves that session's open needs-user rows as `answered_elsewhere` once the pause is gone. This is one filtered `Query` on the caller's inbox, the way handoffs mark their entries read on open. It runs only for headless sessions and never on the turn path. The worker's hold check (§3.2) independently sees the pause is gone.
2. **The watcher's state.** A watch run resumed there still has `report` (§3.7), but no worker reads it, so its cursors don't advance. The next run may raise that finding once. This is accepted for this slice (open question 7).

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
- **No new resources and no IAM change.** app-api already reads and writes the projects table (inbox) and sessions-metadata (`META`, `SCHEDRUN#`, `SCHEDSTATE#`), and Run now already calls the Runtime. The Runtime gains no environment variable.

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
| **2.1: outcome and inbox rows** | `needs_user`, `pending_interrupt_ids` and `error_kind` in the harness; Run now's toast and the eval mapping; the worker's §3.2 branch, the hold, and the `SCHEDSTATE#` fields; the two kinds, `Notification.resolution`, the list's lazy expiry and the `unread_count` filter; the bell's descriptions (Open conversation only); this spec | A paused run lands in the bell and in the history as Waiting, and a watcher holds. The answer is still given in the conversation |
| **2.2: respond** | `GET /notifications/{id}/prompts`, `POST /notifications/{id}/respond` (claim, audit, background resume), `finish_resumed_run`, the history row update, the `GET /messages` resolution; the Runtime's snapshot `run_mode` and `report` rebuild, with a digest test showing the resumed `toolConfig` equals the paused one | Answerable through the API. Tested with moto and a fake Runtime stream for every row of §3.6 |
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
6. **Cost.** The resume's `C#` row has `cacheStatus`, with `toolConfigHash` and `systemPromptHash` equal to the paused call's. State the resume's tokens and dollars, and the held watcher's zero model calls.

## 8. Open questions for the developer

1. **Drain in app-api or in the worker Lambda?** The proposal is app-api's background task, as Run now does: no new IAM, and the user is present. The alternative is async-invoking the scheduled-runs worker with a resume payload, so all run bookkeeping lives in one process. That survives an app-api deploy mid-resume, at the cost of an `InvokeFunction` grant and a function-name variable on app-api. A resume lost to a restart today ends `applying` and becomes retryable after 14 minutes (§3.5).
2. **The watcher's hold.** Is holding (no checks for up to 7 days if the user ignores it) right? The alternatives:
   - keep running with an `awaitingYou` block in `<watch_state>` and trust the model not to redo the work. The quiet watcher already declined to trust the model with dedupe;
   - give watch runs a shorter pause window. The TTL today is per session kind, not per run.
3. **A resumed watch run raises no `agent_item` rows.** The user just acted on that run, and anything else it found is in its conversation, marked unread. Is that enough, or should keys new since the pause still notify?
4. **Run now.** Should a Run now that pauses also raise a bell row? The proposal is no for this slice: the user started it and gets the toast. It is cheap to add later, because the row needs no schedule.
5. **Project schedules** stay as today until gaps open question 4 settles who receives a project run's inbox items.
6. **One resume per pause.** Every prompt in a multi-prompt pause must be answered together. Is that acceptable? Partial answers would need a queue for answers that arrive while another resume holds the lease.
7. **Answers given in the conversation itself.** A resumed watch run's `report` there reaches no worker, so the next run may raise that finding once. Accept for this slice and measure on dev, or have the chat proxy hand a headless session's resume outcome to `finish_resumed_run`?
8. **Model-written text on the row.** A question's text or a sign-in reason, at most 120 characters, is display text in the user's own inbox, under the `agent_item` title's standard. Confirm. Whether it may go into a push payload is gaps open question 6.
