# Proactive Agents — Gap Spec

**Status:** Draft / gap analysis. Nothing here is built or approved.
**Date:** 2026-11-25
**Targets branch:** `develop`
**Builds on:** `docs/specs/agentic-platform-primitives.md` (F1–F6), `docs/specs/scheduled-agent-runs.md` (F1–F3 design)
**Related:** `docs/specs/user-markdown-memory.md` (Memory Spaces), `docs/specs/agent-handoffs-and-consults.md` (handoff inbox entries), `docs/specs/native-app-auth.md` (the iOS app, the push channel)

---

## 0. What this is

`agentic-platform-primitives.md` set out to make a proactive "chief of staff" agent a matter of **configuration over primitives**, not a bespoke feature. Since July, its keystone has shipped: a headless run entrypoint (F1), a run record and session delivery (minimal F2), a scheduler (F3), a governance floor (F6a), and Memory Spaces (F5).

That gets us an agent that **runs on a clock while nobody watches**. Experiences like Anthropic's Claude Duet (research preview, October 2026) go further. The agent watches what the user has connected, decides **by itself** that something deserves attention, reaches the user wherever they are, and asks before it acts. This spec lists what stands between the primitives we have and that experience. Each gap gets a proposed shape, a cost and latency note, and how it is gated.

The test from the parent plan still applies to every item: **is this a reusable primitive any Agent, skill, schedule or project could use, or is it welded to one feature?** Nothing below is a "proactive assistant" feature. Each gap is a building block that a scheduled digest, a project monitor or a workflow run would also use.

### The loop we are trying to close

```
 trigger ──► run (headless, as the user) ──► triage: anything new? ──no──► end silently, cheaply
   ▲                │                               │yes
   │           job state (cursor,                   ▼
   │           what was already said)        inbox item ──► push (phone, browser)
   │                ▲                               │
   │                └──── feedback (dismissed, ◄────┤
   │                      "not urgent", rules)      ▼
   └──────────── next run ◄──────────── approval answered later, from anywhere ──► resume ──► act (audited)
```

Today's path covers the left column and the run itself. Everything right of the run is the gap.

---

## 1. What exists (as of `develop`, 2026-11-25)

| Primitive | Where | What it does today | Limit that matters here |
|---|---|---|---|
| Headless run (F1) | `apis/shared/harness/runner.py` (`run_agent_headless`) | Runs a turn as user U off the live socket. Outcomes: `completed`, `error`, `timeout`, `oauth_required`, `client_tool_call` | No "nothing to report" outcome. An approval or question pause is not a distinct outcome |
| Delivery (minimal F2) | same, after the run | Every run materializes a session; `schedule` and `run_now` runs set the unread dot | A run that found nothing still lands a conversation in the sidebar |
| Scheduler (F3) | `apis/shared/scheduled_prompts/`, `lambdas/scheduled_runs_{dispatcher,worker}/` | Cadences `daily` / `weekday` / `weekly` / `interval` (floor `MIN_INTERVAL_MINUTES = 15`), `max_runs_per_day` runaway guard (default 24), consecutive-failure breaker, `paused_error` | Clock only. A personal schedule that pauses (`oauth_required`, repeated failures, expired grant) writes **no** inbox row; only project schedules get `project_schedule_paused` |
| Unattended identity | `apis/shared/harness/grants.py`, `auth.py` | A headless grant pins a refresh token from an attended session, with its own consent and revocation. Acts only within `HEADLESS_GRANT_MAX_AGE_DAYS = 30` of the last login | Expiry is silent to the user until a run fails |
| Connector tokens | AgentCore Identity vault, `apis/shared/oauth/` | Vaulted per-user OAuth, readable with no live session | Third-party tokens lapse (GitHub's in about 7 days on prod) and the run simply pauses |
| Pauses | `PendingInterrupt` breadcrumbs, `PausedTurnSnapshot`, `paused_turn_ttl` in `apis/shared/sessions/models.py` | Tool approval, `ask_user_question`, OAuth consent, MCP elicitation. A headless pause stays resumable for `HEADLESS_PAUSED_TURN_TTL = 7 days` | Answerable only by opening that conversation. Nothing tells the user a pause is waiting |
| Inbox | `apis/shared/notifications/` (`INBOX#{email}` rows, 90-day TTL), SPA `notification-bell` | Project, quota-request and handoff events | No kinds for personal runs. No push channel: browser notifications fire only while a tab is open and hidden (handoffs), and nothing sends APNs or Web Push |
| Memory | AgentCore Memory (LTM); Memory Spaces (`apis/shared/memory/`, markdown, shareable) | What the agent knows **about the user**, plus readable and editable wikis | Neither is a structured store of **job** state: cursors, "already told you", open items |
| Governance (F6a) | `apis/shared/harness/governance.py` (`RunAuditRecorder`, `check_input`, `classify_output`) | Audit row per run; input and output checkpoints | Covers runs, not individual autonomous **actions**, and not "deliver to someone other than the owner" |
| Cost | `C#` rows, quota, release ledger | Every call metered and quota-checked | No per-job budget; nothing stops a 15-minute watcher on a frontier model from spending a user's month |
| Multi-agent | Handoffs, workflow runs (dev) | A run can hand work to other Agents | — |

---

## 2. The gaps

Ranked by how much of the loop each one closes. **G1–G3 are the minimum** for anything that feels proactive rather than scheduled; G4–G6 make it trustworthy; G7–G9 make it affordable and safe at scale.

### G1 — Job state: cursors and memory of what was already said

**Gap.** A watcher has to know what it has already seen and already told the user. Today a scheduled run starts from nothing every time. It either re-reads the whole window (expensive, and the user hears it twice) or the prompt says "since yesterday", which drifts and misses things. Conversation history doesn't solve it: a session per run means no shared history, and one long session grows the prefix forever. LTM is extracted and opaque. Memory Spaces are prose for people to read, not cursors.

**Proposed shape.** A small, structured, per-job store:

- `JOBSTATE#{jobId}` items under the owner (sessions-metadata, the `SCHEDPROMPT#` neighbourhood), holding:
  - `cursors`: one opaque watermark per connector, e.g. `{"gmail": "historyId:…", "calendar": "syncToken:…"}`;
  - `seen`: a bounded set of item fingerprints already reported, LRU-capped (for example 500);
  - `open`: up to N tracked items, e.g. "waiting on a reply from X, due Friday".
- Two injected tools bound only on headless runs of a job: `job_state_read()` and `job_state_write(patch)`. Writes are conditional on a version, so two overlapping runs can't both advance a cursor.
- The run prompt carries the state as a **per-turn** block (bounded), never in the system prompt. It changes every run, and the cacheable prefix must not.

**Cost note.** It saves money: reading only what's new is the single biggest lever on a watcher's input tokens.

**Gate.** Part of G2's flag. It is inert without a job that uses it.

### G2 — A silent outcome ("nothing to report")

**Gap.** `run_agent_headless` delivers every completed run as a session with an unread dot. A watcher that runs 24 times a day and finds something twice would leave 22 empty conversations. That is the opposite of proactive: it trains the user to ignore the dot.

**Proposed shape.**

- A run can end with an explicit outcome through a constant injected tool, `report(outcome: "nothing_new" | "notify", items?: [...])`. `nothing_new` is the default when the tool isn't called and no assistant text was produced.
- `nothing_new` writes the run record, the cost rows and the job state, **materializes no session, sets no unread dot, and writes no inbox row**. The run is still visible on the schedule's run history for debugging.
- `notify` materializes as today and writes one inbox item per reported item (G4).

**Triage split (cost).** A run that finds nothing should cost close to nothing. Two-stage runs:

1. A **triage pass** on a cheap model (a Task-models admin row, like titles and summaries) reads the new items since the cursor and answers `nothing_new` or a shortlist.
2. Only a shortlist escalates to a full turn on the job's own model, which writes the inbox items and any drafts.

The triage pass is metered like any side call (`callKind: "proactive_triage"`). It has no cacheable history to break, but its system prompt and tool set must be constant per deployment.

**Gate.** `PROACTIVE_RUNS_ENABLED` (in development, default off), app-api and the scheduled-runs Lambdas. While off, every run delivers exactly as today.

### G3 — Triggers that watch, not only a clock

**Gap.** The scheduler fires on a cadence. "Tell me when the provost replies" or "when a file lands in this folder" is a condition, not a time.

**Proposed shape, in two steps.**

1. **Polling with cursors (no new infrastructure).** A job is a schedule plus G1 state. A 15-minute `interval` with a triage pass is a watcher. This covers most connectors and needs only G1 and G2.
2. **Push triggers (later, per connector).** A trigger record `{source, filter, jobId}` and an app-api webhook receiver (`/triggers/{source}`, signature-verified) that enqueues the job instead of waiting for its next tick. Candidates where the provider supports it cheaply: Microsoft Graph change notifications, Google Calendar and Drive watch channels, GitHub webhooks. Each needs subscription renewal, which runs on the existing dispatcher tick.

**Not** on the turn path. A trigger enqueues a headless run; it never runs inside one.

**Gate.** Step 1 rides G2's flag. Step 2 is its own feature switch per source, since each adds a public endpoint and a subscription to manage.

### G4 — One inbox for agent-raised items

**Gap.** `apis/shared/notifications/` is the right table and lifecycle, but its kinds are project, quota and handoff events. A personal run has nowhere to say "this needs you".

**Proposed shape.**

- New kinds, ids and display text only, never message content:
  - `agent_item`, a G2 `notify`: `{jobId, runId, sessionId, title, urgency, sourceRef}`;
  - `agent_needs_approval` (G5);
  - `agent_needs_input` (a question or elicitation pause);
  - `schedule_paused`: the personal counterpart of `project_schedule_paused`, named for the schedule because a "job" isn't defined yet (open question 1). **Built as slice 0** (`apis/shared/scheduled_prompts/pausing.py`): every platform pause of a personal schedule now writes it.
- `urgency` is `now` / `today` / `fyi` and decides the channel (G6).
- Items carry a `dedupeKey` (the G1 fingerprint) so a retried run never notifies twice. That is the same idempotency rule the scheduler already applies to sessions.
- Actions on an item (`dismiss`, `not_useful`, `snooze`) are recorded as feedback for G7.

**Contract.** The inbox read (`GET /notifications`) is consumed by the SPA's bell and, next, by the iOS app. New kinds must be ignorable by an older client.

### G5 — Approvals answered later, from anywhere

**Build plan:** `docs/specs/answer-from-inbox.md` (slice 2). It puts the respond route under the existing inbox router, as `POST /notifications/{notificationId}/respond`.

**Gap.** The pause machinery already does the hard part. A headless run's pause stays resumable for 7 days, and resume is a POST with an `interrupt_responses` entry. But:

- `run_agent_headless` classifies only `oauth_required` and `client_tool_call`. A **tool-approval** or **question** pause reads as `completed`.
- The only way to answer is to open that conversation in the web app.

**Proposed shape.**

- The harness classifies `tool_approval_required`, `user_question_required` and `mcp_elicitation_required` into a `needs_user` outcome, carrying the interrupt ids.
- Each becomes a G4 inbox item with the **bounded, display-only** payload the SSE event already carries: tool name, a summary of the arguments, the question.
- A resume endpoint on app-api, `POST /inbox/{notificationId}/respond {interruptId, response}`. It checks the item is the caller's, then resumes through the existing paused-turn route, so it adds no second resume path. That makes the answer reachable from the bell, a phone notification action, or the iOS app.
- Expiry: the item shows when the pause expires (`paused_turn_ttl`) and resolves itself as "expired" afterwards, never as a dead button. That was the bug the 7-day TTL was introduced to fix.

**Cost and cache.** The resume continues the same session, so the prefix is unchanged and reads from cache if it is still warm. After the cache TTL, the resume pays one re-write. That is the same as any idle return, and the release ledger already measures it.

### G6 — Reaching the user: push

**Gap.** There is no push channel anywhere in the stack. A proactive agent that can reach the user only while a browser tab is open is not proactive.

**Proposed shape.**

- A per-user **device registry**: `PUSHDEV#{deviceId}` rows holding the platform, token, app version and `lastSeenAt`. It is registered by the iOS app after sign-in, and by the SPA for Web Push (§3a). It pairs with the signed-in devices list in `native-app-auth.md` PR 3: revoking a device's session also drops its push token.
- A sender behind the inbox write. An inbox row with `urgency: now` (and `today`, outside quiet hours) is sent through APNs (token-based auth, key in Secrets Manager) or Web Push (VAPID).
- **Content rule: the push carries the title and ids only.** The app fetches the body over the authenticated API, so no conversation text transits Apple's or a browser vendor's push service. This matches the inbox's own "ids and display names only" rule.
- Notification actions (Approve, Decline, Snooze) post to G5's respond endpoint after the app re-authenticates with its Keychain session. Approve never acts from the lock screen without authentication.

**Open-source lens.** Every deployment brings its own APNs key, team id and bundle id, all configuration with none in the tree. An empty `APNS_*` configuration means no iOS push, so the empty value is the switch, as with `NATIVE_AUTH_REDIRECT_URIS`.

**Gate.** `PUSH_NOTIFICATIONS_ENABLED`, a feature switch once finished. It costs money (SNS or a direct APNs connection) and is a product choice.

### G7 — Standing instructions and learning from dismissals

**Gap.** Duet's own stated failure mode is misjudging urgency. Without a way to say "always tell me about X, never Y, not after 6pm" and without learning from what the user dismisses, a watcher either nags or misses things.

**Proposed shape.**

- **Standing instructions** live in a Memory Space page the job binds, e.g. `attention.md`: plain markdown the user can read and edit, and that the agent may propose edits to. Edits proposed by the agent reuse the existing rule that a scheduled run's shared-memory saves become proposals. No new store is needed; the triage pass reads the page.
- **Quiet hours and channel rules** (`urgency` → channel, a do-not-disturb window, a timezone) are structured settings on the user's settings item. They are read by the push sender, not the model.
- **Feedback** from G4 actions is aggregated per job into the job state (G1) as counts by source and sender, e.g. "dismissed 9 of the last 10 from this sender". The triage prompt gets a bounded summary, never the raw history.

### G8 — Connection health as a first-class signal

**Gap.** A background agent fails silently when it loses access, and the user finds out only when they notice it went quiet. Three things lapse:

- the headless grant (30 days without a login);
- third-party OAuth tokens;
- a revoked Agent or skill binding.

**Proposed shape.**

- The worker already knows each case (`HeadlessAuthError`, `oauth_required`, `run_block_reason`). Each writes `schedule_paused` (G4) with the reason and the one action that fixes it: sign in, Connect, or edit the job.
- A daily, cheap **pre-flight** for active jobs checks the grant's age and vaulted token presence **without** running the model, and warns at "expires in 3 days" rather than after the fact.
- G6 pushes the `now`-urgency subset.

### G9 — Budgets and governance for acting alone

**Gap.** Quota is monthly and per user. A watcher has no ceiling of its own, and F6a audits runs, not actions.

**Proposed shape.**

- **Per-job budget**: `maxCostPerDay` on the job. Before each run the dispatcher reads the job's spend today, summed from its runs' `C#` rows by `runId` (no new metering). A job over budget pauses with `schedule_paused{reason: "budget"}`.
- **A scale guard in the run-frequency rules**: an interval under 1 hour requires a triage model to be set. A frontier model every 15 minutes is the scenario this exists to prevent.
- **Action audit**: every tool call made by a headless run that the tool's catalog row marks as write or destructive (MCP `destructiveHint`, or the approval card's own list) writes an `audit` row with the run, tool, target and approver. The governance seam already exists (`RunAuditRecorder`); this extends it from runs to actions.
- **Who receives**: G4 and G6 deliver to the owner only. Anything that sends to someone else (an email, a message) stays behind G5's approval. That keeps F6a's "deliver to self" premise true.

---

## 3. Latency and cost invariants

- **Nothing here touches the interactive turn path.** Triggers, triage, inbox writes and push all run in the scheduled-runs Lambdas or app-api after a run. The injected tools (`job_state_*`, `report`) exist only on headless runs of a job, so an interactive turn's `toolConfig` is unchanged.
- **The headless run's own prefix must stay stable across runs of a job.** Constant system prompt, constant tool set, job state and new items in the user message. Measure with the `C#` row hashes on a dev watcher before promoting G2.
- **A silent run should cost one triage call.** Target under $0.001 per quiet run on the triage model, stated as a measured number in the PR that ships G2, with the per-job-day figure for a 15-minute watcher.

## 3a. Clients: the web app first, the phone for reach

Every gap above is backend-first, but almost none is backend-only. The root `CLAUDE.md` asks for an endpoint and its clients to change in the same PR, and most users are in the web app. So **the web app is the first client of each slice**. The iOS app reuses the same endpoints and adds the one thing a browser does badly: reaching a user who isn't looking.

What the web app already has to build on:

- `notification-bell`, which renders inbox kinds with one `switch` per family (`describeQuota`, `describeHandoff`);
- the pause cards in a conversation (approval, question, consent, elicitation);
- a `/schedules` page and schedule form (`frontend/ai.client/src/app/schedules/`), with a **Schedules** menu entry (`sidebar-items.ts`, no flag);
- browser notifications for handoffs, which fire only while a tab is open and hidden. There is **no service worker**, so nothing reaches a closed tab.

| Gap | Web app half | iOS half |
|---|---|---|
| G1 Job state | On the job's page: what it is tracking, when each source was last checked, and "start over from now", which resets the cursors | Read-only, later |
| G2 Silent outcome | Run history on the job's page that shows quiet runs ("checked 22 times today, raised 2, cost $0.02"). Without it a silent job looks broken | — |
| G3 Triggers | Creating a watcher in the schedule form (source, condition, interval, triage model); later, connecting a push trigger | — |
| G4 Inbox | `describeAgent` in the bell for the `agent_*` kinds; urgency shown; Dismiss, Snooze and "Not useful" on each item | The inbox list, the app's next screen after read-only conversations |
| G5 Approvals | Approve, Decline or answer **in the bell**, without opening the conversation. The web app is the first caller of the respond endpoint; the conversation's own pause card is unchanged | Notification actions and the inbox, after re-auth |
| G6 Push | **Web Push** through a service worker (VAPID), registered in the same device registry as a `web` platform row, and opt-in from Settings. It is the floor for every user without the iOS app, including Android and desktop | APNs |
| G7 Standing instructions | Quiet hours and "what reaches me where" in Settings; the attention page edited through the Memory Spaces UI; "Not useful" feeding the job's counts | Quiet hours honoured by the sender; no editing UI |
| G8 Connection health | A Connections view, or a banner, that warns before the headless grant or a connector token expires, with Sign in or Reconnect. OAuth consent is easiest on a desktop browser anyway | Shows the warning, deep-links to the web app to fix it |
| G9 Budgets | A per-job budget field and today's spend on the job's page. For admins, watcher and triage spend under its own `callKind` on `/admin/costs` | — |

Rules for the web app half:

- **Frontend flags follow the backend ones.** `features.proactiveRuns` and `features.pushNotifications` hold only what the UI offers; the backend is the gate.
- **The bell never shows message content.** It renders from the inbox row's ids and display fields, as `describeHandoff` does.
- **Web Push carries title and ids only**, like APNs. The service worker fetches nothing on its own: clicking opens the app, which reads the item over the session cookie.
- **A service worker is new to the SPA.** It needs its own scope, cache policy (none: it is for push only, not offline), and a check that it doesn't interfere with the BFF cookie or the SSE stream. This deserves a short design note in slice 3's PR.

## 4. Sequencing

| Slice | Gaps | Delivers | Depends on |
|---|---|---|---|
| Slice | Gaps | Delivers | Web app half (same slice) | Depends on |
|---|---|---|---|---|
| **0 — Paused jobs say so** (built) | G4 (`schedule_paused` only), G8 (existing reasons) | A personal schedule that stops tells its owner | The bell's text for `schedule_paused`, linking to the schedule | Nothing. Small, and worth doing alone |
| **1 — Quiet watcher** | G1, G2, G3 step 1, G4 (`agent_item`) | A 15-minute watcher that is silent until something is new, then raises one inbox item | Watcher fields in the schedule form, run history with quiet runs, job state view, `agent_item` in the bell | Slice 0 |
| **2 — Answer from anywhere** (built) | G5 | Approvals and questions from a headless run land in the inbox and can be answered there | Approve, Decline and answer in the bell | Slice 1's inbox kinds |
| **3 — Push** | G6, plus the iOS app's inbox and notification actions | `now` items reach a closed tab and a locked phone; Approve works from the notification after re-auth | Web Push: service worker, opt-in in Settings | Slice 2; iOS read-only conversations; `native-app-auth.md` PR 3 (devices) |
| **4 — Trust** | G7, G8 pre-flight, G9 | Standing instructions, quiet hours, budgets, action audit | Quiet hours and channel rules in Settings, the Connections warning, the budget field, admin spend by `callKind` | Slice 1 |
| **5 — Push triggers** | G3 step 2 | Near-real-time reaction for connectors that support it | Connecting a trigger in the schedule form | Slice 1 |

Slices 1–4 are each one feature, gated by `PROACTIVE_RUNS_ENABLED` while in development. Slice 3's switch (`PUSH_NOTIFICATIONS_ENABLED`) and slice 5's per-source switches are permanent feature switches.

### How this changes the iOS plan

The iOS app's next feature is read-only conversations, then chat. This spec suggests a third use that may matter more on a phone: **the inbox**. List the items, open the run's session, and answer approvals. It should follow read-only conversations, before full chat. Web Push (§3a) does not make the native app redundant on iPhone: Safari delivers Web Push only to a site the user has added to the Home Screen, and APNs notification actions (Approve, Decline) are richer and more reliable there.

## 5. Validation use cases

Each should be configuration once its slice lands:

- **Reply watcher**: "tell me when anyone in this thread replies" (slice 1, Graph or Gmail cursor).
- **Deadline keeper**: tracks `open` items from email and calendar, and nudges the day before (slices 1 and 4).
- **Project monitor**: a Shared Project schedule that raises an item only when its files change in a way that matters (slices 1 and 5).
- **Drafted reply, approved from the phone**: the agent drafts, the approval card lands on the lock screen, Approve sends (slices 2 and 3).

## 6. Open questions

1. ~~**Job vs. schedule.**~~ **Decided (2026-11-25): a schedule with extra fields** for slice 1 (`docs/specs/quiet-watcher.md`); revisit at slice 5.
2. ~~**Triage model.**~~ **Decided (2026-11-25): one run on the schedule's own model**, steered to a small model for short intervals; measure quiet-run cost on dev before adding a second stage.
3. ~~**Session per notify.**~~ **Decided (2026-11-25): a new conversation per notify**, as scheduled runs do today. A per-watcher feed can come later.
4. **Project-scoped inbox.** Does a project schedule's `notify` go to every member's inbox or only the owner's? Today `notify_members` exists for completed runs; the same rule probably applies.
5. ~~**Web Push.** Is browser push worth it alongside the iOS app?~~ **Proposed yes (§3a):** the iOS app reaches iPhone users only, so Web Push is the floor for everyone else. The open part is the SPA's first service worker and how it coexists with the BFF cookie and SSE.
6. **Data in push payloads.** Is "title and ids only" sufficient for a useful lock-screen notification, or does a deployment want an opt-in for a one-line summary?
