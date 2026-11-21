# Workflow runs: several steps under one approval

**Status:** Step 1 built and dev-validated, 2026-11-20, behind `WORKFLOW_RUNS_ENABLED` (on in `development` only). §19 is its delivery plan and §19.1 what it showed. Step 2 (saved workflows, the designer and stages) built and dev-validated, 2026-11-21: `workflow-designer.md` §20.5. Steps 3 and 4 are proposed. Proposed 2026-11-19. It asks one design question of the handoffs work: should the
user's review sit on each hop, as it does today, or on the whole piece of delegated work? Everything else follows
from the answer.
**Author:** Phil Merrell (drafted with Claude)
**Targets branch:** `develop`
**Tracking:** #2186 (step 1)
**Related:**
- `docs/specs/agent-handoffs-and-consults.md`: Phase 0 handoffs, deliveries and file references, which this
  generalises and mostly keeps (§12 here says what happens to each phase)
- `docs/specs/multi-agent-patterns.md` §11: the worked example that prompted this, a fixed fan-out over one
  document, and §5, the "durable pipeline" this makes concrete
- `docs/specs/structured-extraction.md`, `docs/specs/decision-primitive.md`: the side-call engine that prompt and
  checklist steps would run on
- `docs/specs/scheduled-agent-runs.md`: `run_agent_headless`, which a run that outlives the tab needs
- `docs/specs/field-set-authoring.md`: the authoring pattern a saved workflow reuses
- `docs/specs/turn-path-ttft.md`: why none of this runs before a first token

---

## 0. Summary

A handoff today is one Agent, started by the user, reviewed by the user, and brought back by the user. That is
three clicks for one hop. A piece of work with four reviewers is a dozen clicks, in the right order, with the tab
open.

This proposal keeps the handoff as the unit of work and moves the review up one level:

- A **step** is one piece of delegated work. Today's handoff is a step.
- A **workflow run** is a set of steps the user approves **once**, with a shape, a set of shared files and limits.
  Inside what was approved, steps start and return with no further clicks. Anything outside it pauses for the
  user, as it does today.
- A **workflow** is a saved run shape. It is the thing a user designs and shares.

Today's handoff becomes a run of one step, so nothing that exists has to change behaviour.

The first version is small: several handoffs started from one review sheet, with their results returned as one
card. It reuses the Phase 0 store, the review sheet, file references, the relay and the notifications.

## 1. Problem

### 1.1 What prompted this

`multi-agent-patterns.md` §11 records the first outside workflow we were asked to host: a proposal reviewer with
four reviewer personas in parallel, a 15-section audit, a merge and a marked-up PDF. Two findings from working
through it:

- **Each reviewer is better as an Agent than as a prompt**, for one reason: the author of the proposal can open
  the specialist reviewer's conversation and argue with it. A prompt's answer has no conversation to continue.
- **The handoff design cannot run it.** A user can do it by hand on dev: mention each reviewer, send, wait, bring
  back each result (`DELIVERY_MAX_PER_TURN = 3`, so four results take two sends), then ask for the merge. Nothing
  can do it for them.

### 1.2 Why the handoff design is rigid here

The handoffs spec keeps a person at every hop, on purpose. Its later phases hold the same line:

- `suggest_handoff` (0c) only offers a chip the user must press, at most one a turn and three a session.
- `send_to_conversation` (Phase 2) lands as a card that waits for the user. Its unattended landing is opt-in and
  capped at two hops and four wakes an hour.

The reasons are sound: these conversations run as the user, against their quota, with tools that write to Canvas,
Gmail and the web, often with nobody watching. But "a person at every hop" is one way to meet those reasons, and it
is the expensive one. What the reasons actually require is that **nothing spends, writes or loops without the
user having agreed to it**. A person can agree to four reviewers at once.

### 1.3 What is not the problem

The parts of Phase 0 are not rigid. A handoff row with a status, a context card, files shared by reference, a
target's prompts relayed to the source, an inbox entry when it finishes: each is the right piece. The rigidity is
only that each handoff is alone.

## 2. What already exists (read from code, 2026-11-19)

| Piece | Where | What a run reuses |
|---|---|---|
| `HANDOFF#` rows: `USER#` partition, `SessionLookupIndex` GSI on the source session, conditional status transitions | `apis/shared/handoff/store.py`, `models.py` | A step is this row plus a `runId` and `stepId` |
| Statuses `open`, `running`, `needs_input`, `done`, `stopped`, `dismissed`, and `abandoned` after 24 hours | `models.py` (`HandoffStatus`, `ABANDON_AFTER`) | A step's statuses, unchanged |
| Create, get, dismiss; send-back and deliver | `apis/app_api/handoffs/routes.py` | The run's routes sit beside these |
| `handoff_context` and `deliveries` on the turn request | `apis/inference_api/chat/models.py` | Unchanged |
| Background send that does not navigate | `AgentHandoffService.send` → `ChatRequestService.submitHandoffRequest` | One call per step in the first version |
| Concurrent background streams, keyed per session | `ChatStateService`, `StreamParserService` | Already there; the handoffs spec sets no cap on concurrent handoffs |
| Files by reference | `apis/shared/files/linked_files.py` | Each step gets the run's files as reference rows |
| The brief, written from the source's prompt cache | `apis/inference_api/chat/handoff_brief.py` | Written once per run, not once per step (§6.3) |
| Relayed prompts from a paused target | `apis/shared/handoff/relay.py` | Unchanged, one per paused step |
| Inbox entries and browser notifications | `apis/shared/handoff/notify.py` | One for the run, plus one per step that needs input |
| Deliveries, at most three per turn, 2,000 tokens each | `delivery.py` (`DELIVERY_MAX_PER_TURN`, `SEND_BACK_MAX_TOKENS`) | The run's result is one delivery with parts (§7) |
| One headless turn as a user, on a new or existing session | `apis/shared/harness/runner.py` (`run_agent_headless`) | Step 3's server-side runs. It has no `handoff_context` parameter today |
| A side-call fan-out with bounded concurrency and metering | `apis/shared/extraction/batch.py` (`run_bounded`) | Prompt and checklist steps |

What does not exist: anything that groups handoffs, a saved shape, a result that returns without a click, and a
run that survives the tab.

## 3. The model

### 3.1 Step

One piece of delegated work. It has:

- **A worker.** In the first version, always an Agent, in a conversation of its own. Later also a `prompt` (one
  instruction over the run's files) or a `checklist` (a field set or question set over them). Those two are side
  calls with no conversation.
- **An input.** The request, the context card and the run's files by reference. Later, the bounded results of
  the steps it comes after.
- **An output.** A bounded result (text, artifact ids, file ids) and a status.

An Agent step **is** a handoff. It is stored, streamed, paused, relayed and notified exactly as one is today. The
only new fields on its row are `runId` and `stepId`.

### 3.2 Workflow run

A set of steps started by one approval. It has:

- **A shape.** In the first version: every step in parallel, results returned together. Later a step may name the
  steps it comes `after`, which gives sequences and a final merge without a graph editor.
- **An envelope** (§4): the step list, the files shared, and the limits.
- **One result**, delivered to the conversation that started it (§7).
- **A status** derived from its steps: `running` while any step is, `needs_input` while any step waits on the
  user, `done` when all are terminal, with the count that finished.

### 3.3 Workflow

A saved run shape with a name: its steps, their workers, what each reads, and the merge instruction. It is what a
user designs once and starts many times, and what they share with others (§10).

### 3.4 Names

In conversation these were "step", "run" and "plan". The spec says **workflow run** and **workflow**, because
`run` already means one headless turn in this codebase (`run_agent_headless`, `/runs/now`, scheduled runs), and a
second meaning would be read as the first. "Workflow" also collides, mildly, with the Strands `workflow` tool,
which we do not use. Open question 1.

## 4. The approval and its envelope

**Decision: the user approves a run once. Inside the approved envelope, nothing waits for a click. Anything
outside it pauses.**

The approval sheet is today's review sheet, widened. It shows:

- every step: the Agent, its request, and the model it will run on;
- the context card each step receives, with the same Preview, Edit and Remove;
- the files being shared, as checkboxes, as on today's sheet;
- how the results come back (a card on the composer, or a merge started for them);
- an estimate of what the run will cost, and the limits below.

The envelope is what the user agreed to:

| Limit | First version | Why |
|---|---|---|
| The step list is fixed at approval | Yes | A step cannot add a step. This is what replaces the hop budget: there is nothing to loop |
| Steps per run | At most 8 | Bounds the worst case of one click. A guess, to be replaced by what people build |
| Steps running at once | 4 | Matches `DOCUMENT_EXTRACT_CONCURRENCY`'s default, chosen there against the Bedrock throttle alarm |
| Depth | 1 | A step's conversation can start a run of its own only when the user, in that conversation, approves one |
| Files | Only those ticked on the sheet | As today |
| Cost | An estimate, shown; the user's quota applies to every step as it does to any turn | A hard ceiling per run is open question 2 |

**What still pauses for the user**, relayed to the source as today: a tool approval, an OAuth consent, a question
from `ask_user_question`, a browser sign-in. A step that pauses holds only itself. The others carry on.

**What approval does not grant.** It is not a standing permission. It covers this run. A saved workflow started
again shows the sheet again. Skipping the sheet for a workflow the user has run before is a preference to consider
only after measuring how often the sheet is edited (§15).

## 5. Who writes the step list

The platform runs the shape. That is what guarantees every step runs: no model decides whether to call the fourth
reviewer. But the list itself can come from three places, and all three open the same sheet.

| Source | How it starts | State |
|---|---|---|
| **The user** | Mentioning one Agent, as today, or several in one message | One Agent is built. Several is step 1 |
| **An author** | Starting a saved workflow | Step 2 |
| **The model** | Proposing a run: "I'd get three perspectives on this" | One step is built: `suggest_handoff` and its chip (handoffs PR-0.5, #2177 and #2180). Step 4 extends it to several steps |

So a designed workflow and an orchestrating agent are the same object. They differ only in who wrote the list, and
in both cases a person approves it before anything runs.

## 6. How a run executes

### 6.1 First version: in the tab

The SPA starts each step with the call it already makes for one handoff, up to four at a time, and queues the
rest. Each step is a background stream in the same tab. This inherits §4.6 of the handoffs spec whole: a run lives
as long as the tab, the page-unload guard already covers it, and a dropped stream leaves that step `stopped` with
Continue.

That limit is acceptable for the first version because it is the cheapest way to learn whether anyone wants this,
and because the failure is visible and recoverable.

### 6.2 Later: on the server

A run that outlives the tab is executed by a worker that calls `run_agent_headless` once per Agent step. That
needs:

- a `handoff_context` passthrough on `run_agent_headless`, like the `file_upload_ids` passthrough the eval harness
  added;
- ⚠️ **Verify first** (already flagged in the handoffs spec §9.5): that the headless path resolves the same tool
  set the web client sends. A difference re-writes each step's cached prefix, on every step of every run;
- ⚠️ **Verify first:** where the executor runs. The scheduled-runs worker takes one schedule per invocation. A run
  needs something that starts N steps and survives a deploy.

### 6.3 One brief per run

Today each handoff may write its own brief from the source's prompt cache. A run writes **one** and gives it to
every step. Four steps reading four separately written briefs would disagree with each other and cost four side
calls for one job.

### 6.4 Prompt and checklist steps

A `prompt` step is one constrained or free-text call over the run's files. A `checklist` step is a field set or a
question set over them. Both run through `run_bounded`, write a priced `C#` row under their own `callKind`, and
have no conversation. They are what the audit half of the worked example needs, and they are step 2, not step 1.

They are also the cheaper kind of step where no follow-up conversation is wanted (§11).

*2026-11-21:* prompt steps are being built this way, as a new Runtime invocation with no session lease and one
`HANDOFF#` row of kind `prompt` per step (`workflow-designer.md` §21). Checklist steps are decided after them.

## 7. Results

**Decision: a run returns one result, not one per step.**

- The result is a single `DELIVERY#` row with **parts**, one per finished step, each bounded as a delivery is today
  (`SEND_BACK_MAX_TOKENS`), each naming its step and Agent, with its artifacts and files by reference.
- One row means the turn that carries it makes **one** conditional write beside the agent build, however many
  steps the run had. Separate cards cost a write each: on dev, one card blocked the turn for 0.1–0.2 ms and three
  for 4.3–4.5 ms (handoffs spec §11, PR-0.4), and eight would cost more.
- The model reads it as one `<handoff_context>` block per part, as it reads several cards today, so the wrapper
  and its parity fixture need a case, not a new format.
- A step that stopped, or is still waiting on the user, is named in the result as such. Partial results are the
  normal case, and the card says "3 of 4 finished".
- It is **attached by default**, because the user started the run. That is the rule Phase 0 already applies to a
  card the user sent themselves.

**The merge.** In the first version the card waits on the composer and the user sends it, with or without their
own words, and the source conversation's model writes the merge. From step 2, a workflow may carry a merge
instruction, and the sheet says the merge will start by itself when the run finishes. It does so only if the
source is idle and its composer is empty. Otherwise the card waits.

## 8. Pauses, stop and failure

- **A step pauses:** its prompt is relayed under its row in the source, as today. The run reads `needs_input`.
  Answering resumes that step only.
- **Stop:** stopping the run stops every running step with the cooperative cancel each already has, and no queued
  step starts. A step can also be stopped alone.
- **A step fails:** the run continues. The result names the failure.
- **The source is deleted:** steps are conversations of their own and are not deleted with it. Today's cascade
  (`delete_session_handoffs`) removes the source's `HANDOFF#` and `DELIVERY#` rows and leaves the targets. The run
  row goes the same way as the handoff rows.
- **The user opens a step's conversation and keeps working there:** allowed, and wanted. The run's result takes the
  step's result as it stood when the step first finished. Later work comes back the way it does today, with Send
  to *{title}*.

## 9. Storage and API (sketch)

No new table and no new index, following `HANDOFF#`, `DELIVERY#`, `TSUM#` and `EXTR#`.

```
PK:     USER#<user_id>
SK:     WFRUN#<run_id>
GSI_PK: SESSION#<source_session_id>      (SessionLookupIndex)
GSI_SK: WFRUN#<created_at>
```

The row holds the step list as approved, the workflow's name and fingerprint when it came from a saved one, the
file ids shared, the limits, and the id of the result delivery. Step state stays on the `HANDOFF#` rows, which gain
`runId` and `stepId`. A handoff row with no `runId` is a run of one step, so nothing is migrated.

Routes, on app-api beside the handoff routes and behind the same session dependency:

| Route | Does |
|---|---|
| `POST /sessions/{id}/workflow-runs` | Opens a run for review: writes the `WFRUN#` row and one `open` handoff row per Agent step, and returns the sheet |
| `POST /sessions/{id}/workflow-runs/{runId}/approve` | Fixes the envelope. Steps are then started by the SPA (first version) or the executor |
| `GET /sessions/{id}/workflow-runs/{runId}` | The run and its steps |
| `POST …/{runId}/stop` and `…/dismiss` | As named |

Runs replay on `GET /messages` beside `handoffs`. No new SSE event is needed in the first version: every step
already streams as its own conversation, and the run's status is derived from rows the SPA already watches.

## 10. Authoring a workflow

**Decision: a workflow is a reference file in a skill, read on the server. Not a new entity.**

`references/workflow.<name>.md`: frontmatter (`title`, `version`, the merge instruction), then one table whose
rows are the steps in order. This is the choice `structured-extraction.md` §4 made for field sets, for the same
reasons: the skill already has ownership, versions, sharing, role grants, and bindings to Agents and projects. The
SPA never parses the file.

A step row names its worker. An Agent is named by id and **pinned to a published version**, so editing a reviewer
Agent does not silently change every workflow that uses it (`multi-agent-patterns.md` §6.3). A checklist step
names a field set in the same skill.

The editor is the field-set editor's shape: step cards in authored order, moved with Move up and Move down, with a
markdown mode. A canvas is not needed until workflows branch, and `after` gives sequences without one.

*2026-11-20:* `docs/specs/workflow-designer.md` proposes a designer in place of this editor: a canvas that arranges
itself, with each step's stage derived from what it reads (so `after` becomes "reads"), beside a preview that is
the approval sheet. The file decision above stands. It also found that a turn cannot run a chosen Agent version
today, and proposes recording the version and saying when it has changed, with pinning as a later PR (§10 there).

The worked example, as a workflow:

| Step | Worker | Reads |
|---|---|---|
| Generalist | Agent, pinned | The proposal |
| Colleague | Agent, pinned | The proposal |
| Emotional Outsider | Agent, pinned | The proposal |
| Fiscal Outsider | Agent, pinned | The proposal |
| Audit | Checklist: 15 field sets | The proposal |
| Merge | The source conversation, with a merge instruction | Every result |

## 11. Cost, the prompt cache and TTFT

- **Nothing here runs before a first token.** A run starts from a user action outside any turn. The one write on a
  turn path is the result's attach, which is one conditional write beside the agent build (§7).
- **The source's cached prefix does not move.** Its conversation changes only when the result is attached, as an
  appended user message.
- **Each Agent step pays for the shared files by itself.** Four reviewer Agents have four system prompts, so they
  share no cached prefix, and a 40-page document is read four times at the uncached rate. That is the price of
  four conversations the user can continue. Where no follow-up is wanted, a `prompt` step is the cheaper worker,
  and ⚠️ whether prompt steps can share a cached document prefix is still to be measured
  (`multi-agent-patterns.md` §11.4 G3).
- **No tool spec changes in steps 1 to 3.** The model-proposed run (step 4) changes one: `suggest_handoff`
  (278 tokens, handoffs PR-0.5) gains an optional list of steps. That is one prefix rewrite for each user who
  holds the tool, so it is cheapest while the tool is granted to few people, and its system-prompt clause should
  wait until the shape is settled.
- **Every step is metered where it runs.** An Agent step's calls are its conversation's `C#` rows. The run row
  carries the step ids, so a run's cost is a sum the cost drill-down can already make.
- **The estimate on the sheet** uses the catalog's rates and the files' token counts from their rows. It is an
  estimate of input cost, and says so.

## 12. What happens to the handoffs spec

| Part | Under this proposal |
|---|---|
| Phase 0a, 0b, deliveries, file references, the brief, relay, notifications | Kept. A handoff is a run of one step |
| 0c `suggest_handoff` | Kept. It is a run of one step that the model proposed. Its tool, chip, Start route and replay (PR-0.5) are what step 4 extends to several steps, with an optional list on the tool and on the event |
| Phase 1, the consult | Its gap was "the result comes back without the user pressing anything". A one-step run with a merge instruction closes most of that. What remains is doing it **inside the turn**, which is only worth the hidden sessions and nested streaming if waiting for a background step proves too slow. Re-read §7's gate with runs in place |
| Phase 2a, agent-sent cards | A step's result already returns with the run. `send_to_conversation` for free-form messages stays deferred |
| Phase 2b, 2c, the hop budget | Not needed for runs. The envelope fixes the step list, so there is no chain to bound |
| §7's six-week gate | Still the right way to decide Phase 1. This proposal asks to build step 1 **before** that reading, on the evidence of one outside workflow. That is the decision to make (§17, question 0) |

The handoffs spec is not edited by this proposal. If it is accepted, each built step updates that spec in the same
PR, as its own rule says.

## 13. Flags

`WORKFLOW_RUNS_ENABLED` / `CDK_WORKFLOW_RUNS_ENABLED` on app-api, and `features.workflowRuns`. In development:
only `"true"` enables, and it requires `HANDOFFS_ENABLED`. The Runtime needs no variable in steps 1 and 2, because
a step is an ordinary turn with `handoff_context`. It is a capability a deployment may not want (it multiplies
what one click can spend), so it stays a **feature switch** once finished.

## 14. Build order

Each step is useful alone and stops if the one before shows no demand.

1. **Group handoff, in the tab.** Several Agents from one sheet, `WFRUN#`, `runId` on handoff rows, the
   concurrency queue in the SPA, one brief per run, the result as one delivery with parts, the run's row in the
   source. No authoring and no new step kinds. Enough to try with the worked example's four reviewers.
2. **Saved workflows, and prompt and checklist steps.** The reference file and its parser, the editor, `after`,
   the merge instruction and the merge that starts by itself. `workflow-designer.md` §14 proposes its PR plan.
   *2026-11-20:* accepted as `workflow-designer.md` D.1 to D.5, planned PR by PR in §20 there. Prompt and
   checklist steps are decided after D.5.
3. **Runs on the server.** The executor, the `handoff_context` passthrough, the two verify-firsts in §6.2.
4. **Runs the model proposes**, by extending `suggest_handoff` from one step to several.

Evaluating a workflow across variants (models per step, wording, checklist versions) is a separate design. It is
not in this proposal.

## 15. How we'll know

Counts only, on the rows and in EMF, as §4.7 of the handoffs spec does:

- runs created, by source (user, saved workflow, model) and by step count;
- runs approved, cancelled at the sheet, and **edited at the sheet** (a step removed, a request changed, a file
  unticked). A sheet nobody edits is a candidate for skipping; a sheet often edited is doing its job;
- steps opened by the user while running and after finishing. This is the case for Agent steps over prompt steps,
  so it is the number to watch;
- runs that finished whole, in part, or were stopped, and steps left `stopped` by a dropped stream, which decides
  when step 3 is worth building;
- results attached, edited and dismissed;
- cost and wall time per run, by step count.

## 16. Alternatives considered

- **Leave handoffs alone and add agents as tools.** The orchestrating agent calls each reviewer as a tool inside
  its turn. It is the direction `multi-agent-patterns.md` §8 already sketches, and it is faster to a single
  answer. It was not chosen as the base because the model decides which reviewers run, the reviewers'
  conversations are hidden, and it waits on the sub-agent runner and its two open verifications. It remains the
  right design for delegation the model starts mid-answer, and nothing here blocks it.
- **Run the graph with Strands' `Graph` in one turn.** Deterministic, but every node is hidden, the whole run sits
  inside one request, and a pause in one node holds the others.
- **A prompt panel only.** Cheapest per branch, and still wanted (§6.4), but it gives up the conversations.
- **Raise the limits on the existing phases** (more deliveries per send, a bigger hop budget). It keeps the clicks
  and loosens the safeguards, which is the wrong way round.

## 17. Open questions

0. **Build step 1 before the handoffs gate reads?** The gate was written so the cheap phase would show demand
   before more was built. Step 1 is small and reuses Phase 0, but it is still more, on one example.
   *Answered 2026-11-20: yes, build step 1 now.*
1. **The names.** "Workflow run" and "workflow", or something that does not lean on either overloaded word.
   *Answered for step 1, 2026-11-20:* code says `WFRUN#`, `/workflow-runs` and `WORKFLOW_RUNS_ENABLED`; what the
   user sees avoids "workflow" ("Hand off to 4 Agents", "Results from 4 Agents", "Stop run"). The user-facing name
   is decided in step 2, when saved workflows exist.
   *Answered for step 2, 2026-11-20:* "Workflows", for the saved thing and its page (`workflow-designer.md` §8.1).
   A run started by mentions keeps step 1's copy.
2. **A hard cost ceiling per run.** Between steps it is simple: a queued step does not start once the run has
   spent past the ceiling. Inside a running step there is no per-turn spend cap to call on today. Is the estimate
   plus the user's quota enough for the first version?
   *Answered for step 1, 2026-11-20:* yes. The estimate on the sheet, the user's quota and the 8-step cap; no
   hard ceiling until real runs show what they cost.
3. **Where do step conversations appear?** Four reviewer conversations per proposal will crowd the sidebar. Nest
   them under the source, or hide them until opened from the run's row?
   *Answered 2026-11-20:* nested under the source, as one line that expands ("4 Agents · 2 of 4 finished")
   (PR-W.4c).
4. **Whose Agents may be steps?** Any Agent the user can use, at a pinned version. Does the Agent's owner see that
   it is used in someone's workflow?
5. **Shared Projects.** Deliveries refuse a Shared Project task today. Does a run inside a project follow the
   same rule, or is a project workflow the thing members most want?
6. **Is the merge always the source conversation's model**, or may a workflow name an Agent to write it?
7. **A schedule as a fourth source of the step list.** A saved workflow on a clock is the durable pipeline in
   `multi-agent-patterns.md` §5. It needs step 3 first.

## 18. Non-goals

- A free-form canvas or graph editor. (`workflow-designer.md` proposes a canvas that arranges itself, where
  nothing is placed or drawn by hand.)
- Steps that add steps, or runs that start runs without the user.
- Agents messaging each other outside a run.
- Sharing a run, or its steps' conversations, with another user.
- Evaluating or tuning a workflow (§14).

## 19. Delivery plan, step 1 (group handoff in the tab)

*Added 2026-11-20, after checking §2 against `develop` at `5a2b1989e` (#2177 merged).* Four facts the proposal did
not have, each of which changes a PR below:

- **The composer takes one `@`-mention per message.** `ChatInputComponent.mentionedAgent` is a single signal, and
  the menu hides once an Agent is picked ("one mention per turn", Marketplace D11). Several Agents in one message
  is new composer work, not a parse of what is already there.
- **Mentions are never offered in a Shared Project** (`agentMentionsOffered` is false whenever `project()` is
  set), and `create_delivery` refuses a project task (`project_task`). A run cannot start in a project in step 1,
  so open question 5 does not block it.
- **A delivery has one `text`.** `Delivery` has no parts. The attach (`DeliveryTurn`, on the Runtime) renders one
  block per row and the request's `deliveries[]` carries one optional `text` per card. Parts change both, so the
  grouped result is a turn-path PR.
- **The brief instruction names one Agent** (`build_brief_instruction(request, agent_name, description)`), and
  `briefHash` is recorded on one `HANDOFF#` row. One brief per run needs a run variant of the instruction on the
  Runtime.

Order: W.0, then W.1. After W.1, W.2, W.3 and W.4a can run at once (W.2 and W.3 touch different Runtime files).
W.4b follows W.2, and W.4c follows W.1. Every PR is behind `WORKFLOW_RUNS_ENABLED` / `features.workflowRuns`, off by default, and none
changes a tool spec or the system prompt.

- **PR-W.0 — Land this spec.** Docs only: this file and `multi-agent-patterns.md` §11.
- **PR-W.1 — The run row, its routes and the flag.** Backend only.
  - `WORKFLOW_RUNS_ENABLED` in `feature_flags.py` (only `"true"` enables, and only with `HANDOFFS_ENABLED`), CDK
    config and its test, `CDK_WORKFLOW_RUNS_ENABLED` in `platform.yml`, the docs-site row (Kind: feature switch),
    and `features.workflowRuns: false` in all three environment files. App-api only; no Runtime variable.
  - `WFRUN#` rows (§9) in `apis/shared/handoff/runs.py`, on the sessions-metadata table and `SessionLookupIndex`.
    `runId` and `stepId` on `Handoff`, additive. Statuses on the run row: `open` → `approved` | `dismissed`; the
    rest is derived from its steps when read.
  - Routes beside the handoff routes, behind `get_current_user_from_session`, flag checked after auth:
    `POST /sessions/{id}/workflow-runs` `{steps: [{agentId, request}], request}` writes the run and one `open`
    mention row per step, and returns each step's card exactly as the mention create does, plus the run;
    `…/{runId}/approve` `{steps: [{stepId, request?, removed?}], fileIds}` fixes the envelope, conditioned on
    `open`; `GET …/{runId}`; `…/{runId}/stop` (marks the run stopped so no queued step starts; the running steps
    are stopped by the SPA's existing cancel); `…/{runId}/dismiss` (dismisses every `open` step).
  - The estimate (§4, §11): the create response carries what the sheet needs to price a run's first turns
    without another call: each step's model id and its catalog input rate, and the card's tokens. The SPA adds
    the ticked files' tokens. ⚠️ **Verify first:** whether `GET /files` gives a file's token count; if not, add
    it there, read from the row's facts (`tokens.text`, pages), never by reading the file.
  - Replay: `workflowRuns` on `GET /messages` beside `handoffs`, first page only, nothing while the flag is off.
    Run rows go with the session delete cascade.
  - Metrics (§15): `WorkflowRunCreated` by step count, `WorkflowRunApproved`, `WorkflowRunCancelled`,
    `WorkflowRunEdited` by what changed.
  - Acceptance: 2 to 8 steps, distinct Agents, else 422; a project task refused; a step row with a `runId` is
    accepted on its first turn by the unchanged `HandoffTurn` (a test drives it); an approved run's unstarted
    steps never read as `abandoned`; with the flag off every route is 404 after auth.
  - *Built (PR-W.1, #2189).* Where it differs from the plan above. Later PRs read these shapes, not §9's sketch.
    - **Flag.** CDK holds it as its own `workflowRuns.enabled`, not under `handoffs`. App-api gets
      `WORKFLOW_RUNS_ENABLED=true` only while both are on, and `workflow_runs_enabled()` checks `handoffs_enabled()`
      as well.
    - **The run row** (`apis/shared/handoff/runs.py`) has two fields §9 does not list. `sourceMessageIndex` lets
      W.4b place the run's card before any step has been sent; `edits` records what the approval changed.
      `resultDeliveryId` and `briefHash` exist and stay empty. An `open` run a day old reads `abandoned`, as its
      unsent steps do.
    - **Step rows** carry `runId`, `stepId` and, once approved, `runApprovedAt`. That field is the smallest change
      that keeps an approved step from reading `abandoned`. Ordinary mentions, and steps of a run still at its
      sheet, age out as before. `runId` and `stepId` are on `HandoffView`; `runApprovedAt` is not.
    - **Create** takes `{request, steps: [{agentId, request?}]}` and answers `{run, excerpt, excerptHash,
      excerptTokens, turnsCovered, contextBlock, sourceMessageIndex, sourceTitle, steps: [{stepId, handoffId,
      agentId, agentName, request, prompt, modelId?, inputPricePerMillionTokens?}]}`. The card is rendered once, and
      every step row is written by `_open_mention`, the helper the single mention create now uses too. A Shared
      Project task gets a 409 with the deliveries' `project_task` error.
    - **The estimate's inputs.** `modelId` is the Agent's own `modelConfig` model after retirement redirects, and
      the rate is that row's catalog input rate. An Agent with no model gets neither: the step runs on the model
      the send names, which the SPA already prices from `GET /models` (it carries `inputPricePerMillionTokens`).
      ⚠️ **Verified:** `GET /files` gave no token count. It now carries `estimatedTokens` and `pages`, from the row
      only (`document_estimate.facts_tokens` with no model: `tokens.text`, plus `tokens.native` for a PDF sent
      natively). Both are `null` until a file is prepared, and for a reference row.
    - **Approve** also takes an optional `request` (the edited shared request, which every step still on the shared
      one follows) and `filesUnticked`, a count like `handoff_context.filesUnticked`, which is what
      `WorkflowRunEdited{file_unticked}` reads. A step the body doesn't list is kept as created. At most
      `HANDOFF_MAX_FILES` (5) file ids.
    - **Stop** is a 409 for a run never approved, and idempotent once stopped. **Dismiss** is a 409 once approved.
    - **Metrics** add `WorkflowRunStopped`. `WorkflowRunCreated` carries `stepCount` and `runSource: "user"` as
      properties, and `WorkflowRunEdited` counts once per kind of change.
    - **Replay.** `workflowRuns` lists every run whatever its status, each step with its row's `HandoffView`, without
      `relayedPrompts` (a sent step is in `handoffs` with them). A conversation with no runs costs one query.
    - **For W.4a, from review.** Nothing on the server checks a step's `handoff_context.fileIds` against the run's
      approved `fileIds` in step 1: acceptance keeps any file that is the caller's, ready and in the source, as it
      does for a single handoff. So W.4a must send each step exactly the approved list. `estimatedTokens` is
      model-neutral: it does not apply the per-model tokenizer factor (0.95× on Haiku 4.5 and Sonnet 4.6, 1.40×
      otherwise, `tokenizer_costs`). The sheet's estimate must apply that factor per step, or say it doesn't.
- **PR-W.2 — The grouped result.** Backend, on the turn path.
  - `parts: [{stepId, agentName, fromSessionId, fromTitle, status, text, textHash, artifacts, files}]` on
    `Delivery`, additive; each part bounded at `SEND_BACK_MAX_TOKENS`.
  - `POST …/{runId}/collect` builds the row from each step's last assistant message through the send-back
    renderer, names a step that stopped or still waits as such, and is exactly-once through a condition on the
    run's `resultDeliveryId`. The SPA calls it when the last step settles, or when the user brings back a partial
    result.
  - The attach stays one conditional `UpdateItem` per row. `DeliveryTurn` renders one `<handoff_context>` block per
    part, in step order. The request gains `deliveries[].parts: [{stepId, text}]` for edited parts, optional,
    so a stale SPA and the terminal client are unaffected.
  - Files: at most `DELIVERY_MAX_FILES` per part and 10 per run, with the reference rows written in chunks of 25.
  - A `deliveryCases` case with parts in `handoff-prompt.fixture.json`, read by both sides' tests.
  - **TTFT:** the `delivery_attach` line's `waitMs` and `attachMs` on dev with 4 and 8 parts, stated in the PR, for
    the developer's acceptance.
  - *Built (PR-W.2).* Where it differs from the plan above. W.4b reads these shapes.
    - **The row.** `Delivery` gains `runId` and `parts: [{stepId, agentName, fromSessionId, fromTitle, status,
      text, textHash, artifacts, files}]`, in step order; the row's own `text`, `artifacts` and `files` stay empty.
      Its `fromSessionId` / `fromTitle` name the first finished step, for a reader that doesn't know parts. The
      replay (`deliveries` on `GET /messages`) and the collect response carry `runId` and `parts` (each file without
      its storage keys, no `textHash`); a plain card's view has no `parts` key at all. Worst case ~290 KB against
      DynamoDB's 400 KB (eight parts of four-byte text at the cap, five artifacts a part, ten files); plain English
      text is under 100 KB.
    - **One set of checks.** `create_delivery(parts=[DeliveryPartRequest…], runId)` runs every rule of a card on
      each part, against that part's own conversation (`load_linked_senders` reads the receiver once and every
      sender beside it; `_checked_content` is the one place the artifact and file rules live, for a card and a
      part alike). Only a `done` part carries anything, so only its conversation is a sender: a part that
      stopped, waits or runs carries its status and reads nothing, so a step still starting can never refuse the
      whole result for want of its `spawnedFrom`. A result with no finished step is refused (`empty`).
    - **Collect.** `POST /sessions/{id}/workflow-runs/{runId}/collect` answers `{deliveryId, created, status,
      delivery}`. Order: build and check the parts, then claim the run's `resultDeliveryId` (conditioned on
      `approved`/`stopped` and the attribute unset), then write the row under that id; a failed write releases the
      claim. A second call, or the loser of a race, gets the winner's card with `created: false`. 409 for a run never
      approved, 422 when no step has finished.
    - **A finished step's part** is its answer as it stood when it first finished (§8), not its latest:
      `answer_message(at_or_before=…)` takes the latest assistant message stamped no later than the step row's
      `done` write plus 60 s (Memory may stamp a turn's events when it flushes them, just after the status write),
      the artifacts that message produced, and the step conversation's own files made by then, newest first, five
      a part and ten a run in step order. `_send_back_message` now calls the same `answer_message`.
    - **Part statuses.** `done`, `stopped`, `needs_input`, `running`, read from the step row. A step that never
      started reads `running` while the run is approved (it is queued in the tab) and `stopped` once the run is
      stopped; a dismissed, abandoned or missing row reads `stopped`. A removed step has no part.
    - **"Bring back *k* results now"** fixes the run's one result at that moment. A step that finishes later is not
      added: it comes back from its own conversation with Send to *{title}* (§8). The run's steps are not drawn
      as handoff rows, so there is no Bring back result under them.
    - **What the model reads.** One block per part (`render_delivery_parts`), joined like separate cards, each 0a's
      wrapper. Not planned: the block names the **Agent** and then the step conversation's title (`Generalist ·
      Review of the proposal`, `delivery_part_label`), because every step starts from the same card and usually the
      same request, so the titles alone read alike and the model could not tell whose answer is whose. A part with
      no result is the same wrapper around a fixed note (`DELIVERY_PART_STATUS_NOTES`), so a stopped or waiting
      step is named. A finished part the user emptied gives no block.
    - **Attach.** Unchanged for a card. A parts row is still one conditional `UpdateItem`; its parts' files go in
      one reference-row write (`write_reference_rows`, now in chunks of 25), and a failed write drops every part's
      file lines, never the text. `deliveries[].parts: [{stepId, text}]` carries edited parts only; an edit for a
      step that has no result, or a `parts` on a plain card, is ignored. `DeliveryEdited` counts the edited parts
      as its value. The prefetch hints the tool gates from every part's files, as from a card's.
    - **Prompt cache.** The blocks reach only the appended user message. Pinned two ways: an AST test on the
      invocation route (`test_delivery_blocks_prompt_cache.py`) that the turn's `DeliveryTurn` is used only to
      prefetch, hint, start and render, that its blocks go into `join_delivery_blocks(…, final_message)` alone,
      and that no system-prompt, tool or agent builder reads it except through the file-kind hint; and a test that
      a result with no files flips no gate.
    - **Parity.** `renderDeliveryParts`, `deliveryPartLabel` and `DELIVERY_PART_STATUS_NOTES` in `handoff-prompt.ts`,
      pinned by a `deliveryCases` case with parts that covers every status, an artifact, a file, a forged tag and an
      emptied part. `extractDeliveryBlocks` reads a sent run result back as one block per part unchanged.
    - **Metrics.** `WorkflowRunCollected` (`stepCount` = finished parts, `runDetail` `complete` or `partial`) and
      `DeliveryParts` (the part count as its value). `DeliveryFiles` counts every part's files.
    - **TTFT.** Measured 2026-11-20 with the real `DeliveryTurn` against dev's sessions-metadata and file-uploads
      tables, from a laptop under a synthetic user (rows deleted after). A `GetItem` round trip is 35 ms from
      there, about twice dev's in-region figure, so read the comparison, not the absolutes. Nine sends per
      shape; each part ~6,000 characters with one artifact; the last part of a run `stopped`, one part edited.
      `overlap` is how long the route runs between `start()` and `blocks()`; 0 ms is the worst case. Medians:

      | Shape | Files | `attachMs` | `waitMs`, 0 ms overlap | `waitMs`, 15 ms overlap |
      |---|---|---|---|---|
      | 1 card (baseline) | 0 | 41.7 | 41.8 | 24.2 |
      | 4 parts | 0 | 42.5 | 42.8 | 28.9 |
      | 8 parts | 0 | 41.7 | 41.9 | 26.2 |
      | 1 card | 2 | 80.9 | 81.0 | 63.1 |
      | 4 parts | 4 | 82.0 | 82.2 | 65.7 |
      | 8 parts | 8 | 85.3 | 86.0 | 69.0 |

      A run result costs what a card costs: one round trip for the flip, plus one for the reference rows when it
      carries files, as a card with files already does (PR-0.4a). Eight parts add nothing measurable without
      files and ~4 ms with them. Eight separate cards would have been eight writes, and three per send at most. A
      send without deliveries runs nothing new. The in-region `delivery_attach` numbers are recorded in this PR's
      dev validation. ⚠️ The developer accepts this number before merge.
- **PR-W.3 — One brief per run.** Runtime, rides `HANDOFF_BRIEF_ENABLED`.
  - `handoff_brief: {handoffId, runId?}`. With `runId`, the Runtime reads the run row and asks once with a run
    variant of the instruction that names every step's Agent and request, and records `briefHash` on the run row
    and on each step row, so acceptance compares each step's card against it unchanged.
  - The final message is the only thing that differs from a single-handoff brief, so the cache reading is the
    same: a route test sends one body as a turn and as a run brief and asserts identical `get_agent` arguments.
  - *Built (PR-W.3).* Where it differs from the plan above, or settles something it left open:
    - **Request.** `handoff_brief: {handoffId, runId?}`; without `runId` the request, the instruction and the frames
      are today's, byte for byte (pinned in `test_handoff_run_brief.py`). With it, the Runtime reads the `WFRUN#`
      row beside the `HANDOFF#` read (one `gather`), and only then: an ordinary turn and a single brief read nothing
      new. A run that is not the caller's, starts from another conversation, is not `open` (approved, dismissed, or
      a day old and so `abandoned`) or does not keep `handoffId` as a step is the existing `not_open` fallback; a
      failed read is too. **No new fallback reason.** Both frames add `runId` on a run's brief, additively.
    - **The instruction** (`build_run_brief_instruction`, `BRIEF_RUN_INSTRUCTION_VERSION = 1`) names the shared
      request and every kept step's Agent with its description, and a step's own request only where it differs
      from the shared one. It asks for one brief for the work as a whole and not a section per Agent, since each
      gets its own request separately. Descriptions are read concurrently with the caller's access check, as the
      single brief's is. **Bound:** shared request 4,000 chars, per step name 120, description 300, own request
      1,000, at most 8 steps, so the whole instruction is at most 18,000 chars (about 4.5k tokens with every part
      at its cap; a run of four with short requests is under 1k). A cut part ends in `…`.
    - **The cost row** carries `briefInstruction` (`"run"` or `"handoff"`) and `briefInstructionVersion`, and a
      run's also `runStepCount`. `BRIEF_INSTRUCTION_VERSION`'s comment had said the single brief's version was on
      the row; it was not until this PR, so a single brief's row gains those two fields too. Its request does not
      change.
    - **Recording.** `briefHash`, `briefTokens` and `briefCacheStatus` go on the run row
      (`WorkflowRunStore.record_brief`, conditioned on `open`) and on every kept step's `HANDOFF#` row, all written
      concurrently, each conditioned on its row being `open`. Acceptance (`HandoffTurn`, unchanged) then compares
      each step's card with its own row's `briefHash`; a step whose write failed falls back to its `excerptHash`.
    - **Metrics.** `HandoffBriefWritten` / `HandoffBriefFallback` keep their names and gain the properties
      `briefScope: "run"` and `runStepCount` (the kept steps; absent for a fallback decided before the run was read).
    - **IAM.** No change: the Runtime's `DynamoDBTableAccess` grant on sessions-metadata has no `LeadingKeys` or
      sort-key condition, so it covers `WFRUN#` rows as it covers `HANDOFF#` ones.
    - **Until W.4a sends `runId`,** the SPA's brief for a run is a single brief for the step it names.
- **PR-W.4a — Start a run.** SPA. The design is §20.1.
  - Several `@`-mentions in one message, up to 8, each a chip in the composer.
  - The run sheet (§4), one brief streamed into one shared card, a request per step that defaults to the shared
    one, the files as checkboxes.
  - On approve, the queue: four steps at once through `AgentHandoffService.send`, the rest started as earlier ones
    settle (`done`, `needs_input` or `stopped`). The queue lives as long as the tab; a reload shows unstarted steps
    as "Not started" with one Start button.
  - ⚠️ **Verify first:** five streams at once on one origin (the source and four steps) under HTTP/2 end to end.
    On HTTP/1.1 the browser's six-connection limit would stall the rest of the page.
  - *Built (PR-W.4a).* Where it differs from the plan above:
    - ⚠️ **Verified 2026-11-20: the browser leg is HTTP/3 or HTTP/2, never HTTP/1.1.** On dev, `nextHopProtocol` for
      the page and every `/api` request (the stream is `POST /api/chat/stream`, same origin) read `h3`, with `h2`
      where QUIC wasn't used; `curl --http2` against `dev.boisestate.ai` and `/api/health` negotiated `2`.
      CloudFront multiplexes every request on one connection either way, so the six-connection limit doesn't apply
      and the concurrency stays at the run's `limits.concurrency` (4). CloudFront's leg to the origin is separate
      per request and isn't bounded by the browser.
    - **The composer.** `mentionedAgent` stays the first mention, so every one-mention path is unchanged;
      `additionalMentions` holds the second to eighth, only while `ChatInputComponent.multipleMentionsOffered`
      (`features.handoffs`, `features.workflowRuns`, mentions offered, and the page's `offerMultipleMentions`,
      which only the thread's full-page composer sets). Each mention is the existing highlighted `@Name` (the
      composer has no chip beside the input since the D11 highlight), an Agent already mentioned isn't offered
      again, and the menu stays shut with the caret inside a committed `@Name`. The send carries
      `mentionAgentIds` only for two or more; drafts keep them as `additionalMentionAgentIds`.
    - **Routing.** `session.page` opens the run sheet for two or more mentioned Agents in a thread with messages,
      setting aside the conversation's own bound Agent (`runMentionTargets`); one left is today's single handoff.
    - **The sheet** is its own component (`WorkflowRunSheetComponent`), so `HandoffReviewSheetComponent` is
      untouched. Its Preview asks which step with a select and builds that step's prompt with the single sheet's
      own `previewPrompt`, given the step's request and handoff id (so the file lines name that step's reference
      rows). The last step left has no Remove.
    - **The send path.** `AgentHandoffService.send` and a run's steps share one private `launch` (the row, then
      `submitHandoffRequest`); a step goes through `startRunStep`, with no per-step toast and `runId`/`stepId` on
      its row. `WorkflowRunQueueService` (root, keyed by run id) frees a slot when the step's row settles or its
      stream's promise settles, whichever comes first, so a refused or undelivered send doesn't stall the run.
    - **The estimate** prices card + request + ticked files per step at that step's model (the Agent's, else the
      send's selected model), times the tokenizer factor (`TOKENIZER_FACTORS` in `workflow-run-estimate.ts`,
      mirroring `tokenizer_costs.py`). `GET /files` gives one model-neutral count per file, so the factor is applied
      to the whole of it, page images included: a slight over-estimate on the newer tokenizer. A missing rate, a
      rate of 0 (the SPA's placeholder default model) or a file with no count says "Cost estimate unavailable".
    - ⚠️ **After a reload, the card.** The run row keeps the approved `fileIds` but not the card as reviewed, and
      `handoff_context.text` is absolute (absent means "removed"). So the queue writes the approved envelope to
      local storage (`aps.workflowRun.<runId>`, 24 hours) until the last step has started, and "Not started" →
      Start resumes from it. In a browser without that envelope the strip says the steps can be started only from
      where they were approved. Storing the reviewed card (or its hash plus the brief) on the run row at approve
      would remove that limit; it is a backend change, left for a later PR.
    - "Not started" renders above the composer (`WorkflowRunResumeComponent`) until W.4b's run row exists to hold it.
    - **Contrast, measured** in the running SPA (every text node against its composited background): lowest
      4.53:1 dark and 7.56:1 light. Brand-text links take `dark:text-primary-accessible-dark` on the page and
      `dark:text-primary-50` inside the card's raised `dark:bg-white/5` box, where `-accessible-dark` measured
      3.98:1. ⚠️ The single review sheet's own links have no dark pair at all (1.67:1 measured on this sheet before
      the fix); left as it is here so its code is unchanged, and worth its own small fix.
- **PR-W.4b — The run in the source, and its result.** SPA. The design is §20.2 and §20.3.
  - The run's row in the source thread: **one card with a line per step** (decided 2026-11-20, over a single
    collapsed line). The header names the run and counts what needs the user; a segmented bar shows each step's
    state; each line is the step's handoff row in small (status mark, Agent, live status line, a link to its
    conversation). A step's relayed prompt opens under its own line, never behind a click, because a hidden
    prompt is the stall §8 is about. Under the lines: Bring back *N* results now (a partial result) and Stop run.
  - The grouped card on the composer, Preview and Edit per part, attached by default; the sent bubble rebuilds it
    from its own text.
  - The bell gets one entry for the run, plus one per step that needs input.
  - *Built (PR-W.4b).* Where it differs from the plan above, or settles what it left open:
    - **The row** is `WorkflowRunRowComponent`, placed by the message list after the message at the run's
      `sourceMessageIndex` (or at the tail when that message isn't on the loaded page), for every run that is
      `approved` or `stopped`, live from approve and replayed from `workflowRuns`. Those runs' steps are taken
      out of the handoff rows, so a step is never drawn twice; a step of a run the SPA doesn't know (the flag
      off) stays an ordinary row. Each step's state comes from one pure function (`workflow-run-progress.ts`):
      this tab's live handoff row first, then the run's replayed copy, with a step streaming in this tab
      `running` whatever its row says. The status line is the handoff row's own, moved into a shared
      `handoffStatusLine` so the two cannot word a state differently.
    - **States the plan didn't name.** A step a reload left unstarted reads **"Not started"** (pause mark, no
      link) and the row offers **Start *k* Agents** under the card; a step queued in this tab reads "Queued". A
      stopped run's never-started step reads "Didn't start". The header's title is "*source title* · *N*
      Agents", or "Handed off to *N* Agents" before the conversation has a title.
    - **The reviewed card is on the run row.** `…/approve` takes `card: {text?, removed}`. The text is capped as
      the Runtime caps a card (`cap_context_text`, ~16k characters) and the field is bounded at 200k; a removed
      or blank card is kept as `{removed: true}`, exactly as a blank `handoff_context.text` reads. It comes back
      on `GET …/{runId}`, the approve response and the `workflowRuns` replay, so Start resumes the steps left in
      **any** browser from the row's own card and files. The `aps.workflowRun.<runId>` local-storage envelope is
      gone, with **no fallback read**: the flag is on only in dev and the envelope lived a day, so the only runs
      it could have helped are dev runs approved in the hour before the deploy. A run approved before this PR
      offers no Start and says "These weren't started. Mention the Agents again to start them."
    - ⚠️ **What a resumed step does not carry.** The mention message's own attachments, invoked skills and
      folders (`fileUploadIds`, `invokedSkillIds`, `folders`) ride only the steps the approving tab started; the
      row keeps the approved shared files and the card, not those. `filesUnticked` is sent as 0 on a resumed
      step, so `HandoffFilesUnticked` undercounts there. Both are small; storing them is a follow-up if a run's
      steps are ever routinely resumed.
    - **Stop run** posts `…/stop`, closes this tab's queue (no pending step starts) and calls each step's own
      cooperative cancel (`ChatHttpService.cancelChatRequest`) for every step streaming in this tab, exactly as
      Stop in that conversation would. A step close to its end can still finish (its row goes to `done`, not
      `stopped`), so Stop run promises no running step stops. A step running in another tab stops only from there.
      No confirmation. A 409 (never approved) is ignored; any other failure says the steps here were stopped and
      to try again.
    - **When the result comes back.** A run started or resumed in this tab is watched (`WorkflowRunResultService`)
      until every kept step is `done` or `stopped`. Then it toasts "*k* Agents finished · Open", where *k* is the
      steps that **finished** (a run with a failed step does not claim all *N*), Open scrolls to the run's row (the
      single row's focus request, pointed at the first step), and it collects quietly. Not after Stop run: a run
      the user stopped is collected only from its row's Bring back results. A run nobody watched here (a reload)
      is never collected on its own. A step's own "finished" and "stopped" toasts are dropped for a run's steps;
      "needs your input" stays.
    - **Bring back.** "Bring back *k* results now" while some step is still queued, running or waiting on the
      user; "Bring back results" once all have settled; both disappear once the run has its `resultDeliveryId`,
      and "Added to your next message" shows while the card waits on the composer. A collect answering
      `created: false` puts the card back only while it is still `pending`; an `attached` one says "These results
      were already sent". The row offers no per-step Bring back for a step that finishes after the result was
      fixed: it comes back from its own conversation with Send to *{title}* (§8).
    - **The card** is `DeliveryCardsComponent` with a parts branch: one card in one of the three per-send
      slots, a `divide-y` row per part, Preview (the same `pre`) and Edit for a finished part, and a muted row with
      no actions for the others. `DeliveryStateService.editPart` keeps edits per step (an edit back to the part's
      own text is no edit, and edits survive a reload of the list); the send carries
      `deliveries: [{deliveryId, parts: [{stepId, text}]}]` for edited parts only, and the live bubble is
      `renderDeliveryParts` over the edited parts, so it reads as the reloaded one does.
    - **The sent bubble** is rebuilt from the message's own text: `extractDeliveryBlocks`, then
      `groupRunResults`. The stored text carries no run marker and nothing may be added to it here, so a run's
      blocks are recognised by what `renderDeliveryParts` writes: consecutive blocks each labelled for the next
      of one run's step Agents, in step order (an emptied part gave no block, so steps can be skipped, never
      reordered). The Agents come from the conversation's `workflowRuns`, so a run off the replayed page shows as
      separate cards, as before. A lone block is grouped only for a run of one. Pinned by the fixture's parts
      case (`handoff-prompt.spec.ts`).
    - **The bell (gap, follow-up).** One bell entry for the run needs a write that neither side makes today:
      the per-step `handoff_*` entries are written by inference-api after each step's status write
      (`apis/shared/handoff/notify.py`), and a run entry would be a Runtime change or a new app-api write at
      collect. Neither is in this PR; each step keeps its own entries, unchanged. Follow-up: a
      `workflow_run_done` entry, written once when the last step settles, deduped by run id.
    - **Contrast, measured** in the running SPA, every text node of the row, the card and the bubble against
      its composited background: lowest **4.84:1 light** and **5.13:1 dark**. The bar's fills (non-text, 3:1):
      light done 3.22, running 10.6, needs input 3.19, stopped 4.84; dark 7.98, 3.99, 8.27, 3.67. Two changes
      from §20.2's list made that true: the queued segment is a `gray-500` **outline**, not a `gray-200` fill (no
      pale gray clears 3:1 on white), and stopped is `gray-500` (`gray-400` measured 2.6:1 on white); dark running
      is `primary-300` (`primary-400` measured 2.59:1). The relayed prompt inside the row is
      `HandoffRelayComponent` unchanged, measured with it as the single handoff row measures it.
- **PR-W.4c — Step conversations nested in the sidebar** (open question 3). Backend and SPA, after W.1. The design
  is §20.4.
  - The step's `spawnedFrom` gains `runId`, written by the background `SET` after acceptance that already writes
    it (`HandoffTurn`), so nothing moves onto the first-token path.
  - ⚠️ **Verify first:** whether the session list already returns `spawnedFrom`; if not, add it, additive.
  - The sidebar shows a run's steps as one line under the source ("4 Agents · 2 of 4 finished") that expands to
    the step conversations. A step conversation whose source is not on the loaded page is listed as today.
  - *Built (PR-W.4c).* Where it differs from the plan above, or settles what it left open:
    - **The link.** `set_session_spawned_from` takes an optional `run_id`, and `HandoffTurn._link_target` passes
      it only for a step, so a single handoff's link is written exactly as before. It is the same background `SET`
      after acceptance; a test pins that the context block is back before that write runs.
    - ✅ **Verified: the session list already reads `spawnedFrom`, and returned none of it.** `GET /sessions` reads
      `SessionRecencyIndex`, which projects ALL, and `SessionMetadata` keeps extra fields; only
      `SessionMetadataResponse` dropped it. It now declares `spawnedFrom: {sessionId, handoffId?, runId?}`
      (the row's `settled` stays off the wire, and a malformed link drops the link, never the list). The pinned
      list and `GET /sessions/{id}/metadata` build the same response, so they carry it too. No read per row.
    - **Where the count comes from.** The list carries the link, not the steps' states, and a queued step has no
      conversation yet, so the line can't count from the rows it nests. `WorkflowRunSidebarService` counts with
      `runProgress` over the same sources and signals as the run's row: the run as `WorkflowRunStateService`
      holds it (the source's own `GET /messages` replay), else the run read once with
      `GET /sessions/{source}/workflow-runs/{runId}`, plus this tab's live handoff rows. That read is the
      sidebar's, never on the turn path: once per run on the loaded page, and again only when one of its step
      rows changes in the list (`lastMessageAt` or `awaitingInput`). Until it answers, or if it fails, the line
      reads "*n* Agents" for the steps it lists, with no count.
    - **A step started in this tab** nests at once, before the list reloads: the link is also read from this
      tab's live handoff rows (`runId` and `targetSessionId`), the same fact written at acceptance.
    - **Nesting is one level deep.** A step conversation hosts nothing itself, so a run started from a step lists
      its own steps alone. A source that is a project task hosts nothing either (project tasks are grouped under
      the project), and a pinned source is out of its bucket, so its steps list alone.
    - **The expanded rows** are in the run's step order where the run is known, else the list's order.
- **Step titles name their Agent** (after W.4c, settled with the developer on 2026-11-20). Every step gets the
  same request and card as its first message, so every step's generated title read the same ("Proposal Review
  Request" four times). The title reaches every surface (the sidebar, the top nav, search, the bell,
  notifications, and the "From …" card that Send to *{title}* makes), so the fix is in the title, not in one view.
  - *Built.*
    - **Only a run step.** The title task (`generate_conversation_title`, Nova Micro, already concurrent with the
      stream) takes an optional `agent_name_for_title`. The route passes `HandoffTurn.run_step_agent_name` only
      when the turn carries a `handoff_context`; that waits for the acceptance the stream's head starts beside the
      agent build, never starts it, and reads nothing, then gives the accepted row's `agentName` only when the row
      has a `runId`. A single handoff, an ordinary conversation and a later turn get exactly today's title (pinned
      by tests). `HandoffTurn.for_turn` is decided just before the title task now, instead of after the quota
      check; it is pure, so nothing moves onto the first-token path. A turn refused before its stream starts gives
      up the wait after 30 s and keeps the plain title.
    - **Format.** `"{Agent} · {generated}"` (`run_step_title`, `apis/shared/handoff/context.py`). The 50-character
      cap covers the whole title and the generated part is the one shortened (`...`), so the Agent's name is never
      cut. **A name too long to leave 15 characters of the generated title (over 32 characters) keeps the title as
      generated:** naming the Agent would mean cutting its name, and the run result's labels still name it in
      full. A failed generation keeps "New Conversation", never prefixed.
    - **No doubled name.** `delivery_part_label` writes the title alone when it already starts with
      "{Agent} · ", so a part reads "Budget reader · Proposal review" once. `deliveryPartLabel` matches it, pinned
      by the parity fixture's "run result whose titles already name their Agent" case, and `groupRunResults`
      still recognises the blocks (its "{Agent} · " prefix rule is unchanged). The composer's result card shows the
      Agent in bold and the rest of the title after it (`partTitleAfterAgent`), so it shows the name once too.
    - **What the model reads.** Titles reach the source's prompt only through a run result's part labels. Those
      change for runs started after this ships, in the new user message the result is attached to; no stored
      history changes, so the source's cached prefix is untouched. Nothing reaches the system prompt or
      `toolConfig`, and the step's own prompt never sees its title.
- **Dev validation** (each PR's own session, after merge): the worked example's four reviewers on one proposal, with
  one step needing a tool approval answered from the source, one stopped mid-run, a partial result brought back,
  and the full result sent. The source's `C#` rows show no prefix change beyond the appended block, and the
  attach's TTFT number is recorded.

Settled with the developer on 2026-11-20: the run's row (W.4b), the sidebar (W.4c), the wording and the cost
rule (§17, questions 0 to 3).

### 19.1 Step 1 outcome (2026-11-20)

Every step-1 PR is merged and passed its dev check: W.1 (#2189), W.2 (#2195), W.3 (#2193), W.4a (#2194), W.4b
(#2199), W.4c (#2208), the step titles (#2214, then #2216 so the top nav names the Agent once), and two fixes the
checks found. #2203 stops the queue starting a step inside an unloading page. #2212 fixes Send to *{title}*
returning 404 until a reload: every tool round left the SPA's live message ids one behind the server's, because
the stream has no frame for the stored tool-result message. That also misfiled feedback and rewind targets on
tool-using answers. Feedback rows from before the fix are not backfilled.

**Measured on dev:**

- **The source's prompt cache.** A run result is appended blocks only. On the source's `C#` rows every
  result-carrying call was a `hit` that read the whole previous prefix, with `toolConfigHash` and
  `systemPromptHash` unchanged and nothing wasted. The run brief (W.3) read the source's cache in full (14,284
  tokens, exactly the prior turn's read plus write), and the next real turn read the same 14,284 with its
  `historyHash` lineage intact, so the brief reached nothing in the history.
- **TTFT.** The run result's attach waited **0.3–0.4 ms** in region (`delivery_attach.waitMs`). That is in line
  with a single card (0.1–0.2 ms) and under three separate cards (4.3–4.5 ms). The write itself (27–42 ms) runs
  behind the agent build.
- **Start.** Approve answered in 254 ms. Three steps started within 1 ms of each other and finished in 12–17 s.
  With five Agents, four ran and the fifth queued (`concurrency: 4`).
- **The estimate.** The sheet said about $0.02. A hand recomputation from the catalog rates, the file's tokens
  and the tokenizer factors gave $0.016.

**Open after step 1.** None blocks using it on dev.

- **One result per run.** After "Bring back *k* results now", a step that finishes later comes back only through
  Send to *{title}* in its own conversation. A second collect, or parts appended while the card is pending, is a
  step-2 candidate.
- **Stop run** stops the queue and the steps streaming in this tab. It can't reach a step running server-side
  after a reload, and a short step can finish before the Stop lands (the handoff Stop path reacts after the fact).
- **No bell entry for a finished run** (`workflow_run_done`). It needs an app-api or Runtime write.
- **A step started from "Not started"** carries the card and the request, but not the mention's attachments,
  skills or folders.
- **A run needs a source with turns.** The multi-mention composer only appears in a thread with messages, and
  create needs the session row. *(A saved workflow can start one from a new conversation since PR-D.2, and its row shows there
  since the fix recorded under `workflow-designer.md` §20.3.)*
- **Display leftovers.** A partial card keeps "Still waiting" for steps that finished later. "Answered ·
  *X* resumed" stays under a finished step's line. A step line that was running at a reload reads "Working on"
  until the next reload. *(Fixed in step 2 by #2242: `workflow-designer.md` §20.5.)*
- **Dark-mode focus rings** are missing (1.67:1) on the single review sheet's links, the project heading and the
  sidebar row menus. All three predate this work, and one small fix covers them.

## 20. What the user sees, step 1

*Added 2026-11-20, from a mockup the developer reviewed and liked.* The mockup was drawn outside the SPA, so this
section is the source, not the picture. **Every surface is an existing component's look, widened.** A run should
read as "a handoff to several Agents", not as a new feature with a new style. Tokens are
`.claude/skills/tailwind-ui/references/app-conventions.md`'s: `rounded-2xl`, `text-sm/6` body and `text-xs/5`
meta, `primary-accessible` for the one solid button and for brand text, `state-*` for status, a `dark:` pair on
every class. These are dense surfaces inside the thread, so the house style's glass, blobs and grid stay off them.
Dense lists are rows divided by `divide-y`, never cards inside a card.

### 20.1 The approval sheet (W.4a)

The look of `HandoffReviewSheetComponent`, as a second component or a mode of it, whichever keeps the
single-Agent sheet's code unchanged.

```
┌ Hand off to 4 Agents ──────────────────────────────────────────── × ┐
│ review this proposal against the solicitation              Edit     │
│ ─────────────────────────────────────────────────────────────────── │
│ Generalist            Sonnet 5.5                     Edit   ⊖       │
│ Colleague             Sonnet 5.5                     Edit   ⊖       │
│ Emotional outsider    Haiku 5.5                      Edit   ⊖       │
│ Fiscal outsider       Haiku 5.5                      Edit   ⊖       │
│ ─────────────────────────────────────────────────────────────────── │
│ ▸ One brief for all 4 · ~2k tokens      Preview · Edit · Remove     │
│ ☑ proposal.pdf · 41 pages                                           │
│ About $0.90 to start, before any follow-up (estimate)               │
│                                        Cancel   [ Start 4 Agents ]  │
└─────────────────────────────────────────────────────────────────────┘
```

- **Container:** the sheet's own classes (`rounded-2xl border border-gray-200 bg-white p-4 shadow-sm`,
  `dark:border-gray-700 dark:bg-gray-900`), `role="region"`, `aria-label="Hand off to 4 Agents"`. Esc cancels, as
  today.
- **The shared request** on top, editable as today. It is every step's request until a step's own Edit is used,
  which opens an inline textarea under that step's row and marks it "Own request".
- **The steps** are rows: the Agent's icon and name (`text-sm/6 font-medium`), its model as meta, Edit, and
  Remove as an icon button labelled "Remove *{Agent}*". Removing the second-to-last step leaves a run of one,
  which still starts.
- **The card, the files and Preview full prompt** are the single sheet's controls, unchanged. Preview asks which
  step's prompt to show, because the requests may differ.
- **The estimate** is a `text-xs/5 text-gray-600 dark:text-gray-400` line that says "estimate" and "before any
  follow-up". It updates as files are ticked.
- **One primary button**, "Start *N* Agents" (`bg-primary-accessible`), disabled while the brief writes, as Send
  is today. Cancel is the ghost button.
- **The composer:** each mentioned Agent is the existing mention chip, repeated. A ninth mention is refused with a
  helper line: "Up to 8 Agents in one handoff".

### 20.2 The run's row in the source (W.4b)

The look of `HandoffRowComponent`, with one difference: the card is a `<section>`, not a link. Each step line inside
it is the link.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Proposal review · 4 Agents                           1 needs you     │
│ ▬▬▬▬▬▬▬▬▬ ▬▬▬▬▬▬▬▬▬ ▬▬▬▬▬▬▬▬▬ ▬▬▬▬▬▬▬▬▬                               │
│ ✓  Generalist · Finished                                          ›  │
│ ✓  Colleague · Finished                                           ›  │
│ ●  Emotional outsider · Reading section 4                         ›  │
│ !  Fiscal outsider · Waiting for your approval to run budget_check ›  │
│      [relayed prompt: Approve / Deny]                                │
└──────────────────────────────────────────────────────────────────────┘
   Bring back 2 results now · Stop run
```

- **Container:** the row's card classes (`rounded-2xl border border-gray-200 bg-white px-4 py-3`, dark pair) on a
  `<section aria-labelledby>` whose heading is the source title and the count.
- **Header:** the title (`text-sm/6 font-medium`). On the right, a count of what needs the user ("1 needs you", in
  `text-state-warning-700 dark:text-state-warning-400`), or "2 of 4 finished" in gray once nothing does.
- **The progress bar** has one segment per step, `aria-hidden`, because the header and each line already say
  everything it shows in text. Fills, all non-text (3:1), as built and measured (§19 PR-W.4b): done
  `bg-state-success-600` / dark `-500`, running `bg-primary-500` / dark `primary-300`, needs input
  `bg-state-warning-600` / dark `-500`, stopped `bg-gray-500`, and queued or not started a `ring-gray-500`
  outline (a `bg-gray-200` fill is about 1.2:1 on white).
- **A step line** is the handoff row's link in small: the same status marks (the pulsing dot with
  `motion-reduce:animate-none`, `heroExclamationCircle`, `heroCheckCircle`, `heroMinusCircle`), the Agent's name,
  the same status line from the same sources, and a chevron. Each line is one tab stop, and its accessible name is
  the row's. A queued step reads "Queued" and links nowhere until it starts. A step a reload left unstarted reads
  "Not started", and the row offers "Start *k* Agents" under the card; a stopped run's never-started step reads
  "Didn't start".
- **The relayed prompt** is `<app-handoff-relay>` under its own line, open, never behind a click.
- **Below the card,** as siblings and never inside a link, in the row's `text-xs/5` link-button style: "Start
  *k* Agents" (steps a reload left unstarted), "Bring back *N* results now" while some are still running or
  waiting on the user, "Bring back results" once all have settled, and "Stop run" until they have. Once the
  result is made: "Added to your next message" while it waits on the composer. Stop run asks no confirmation: the
  steps stopped so far keep their results, and each has Continue. A step close to its end can still finish.
- **Not a live region,** as the row isn't. The toasts announce changes.

### 20.3 The result card (W.4b)

The look of `DeliveryCardsComponent`: the same container and footer, with one row per part.

- **Header:** "Results from 4 Agents" and, in meta, "3 of 4 finished". Remove is the header's ×, as on a single
  card ("Remove the results from 4 Agents").
- **Parts:** a row each, with the Agent's name, its conversation's title in meta, and that card's Preview (the
  same `pre`) and Edit. A part that stopped or still waits is a muted row with no actions: "Fiscal outsider ·
  Stopped, not included", "Emotional outsider · Still waiting, not included" (a part still running reads the
  same).
- **Footer:** "Goes with your next message", as today. The card takes one of the three per-send slots.
- **The sent bubble** shows "From 4 Agents" with Show results, rebuilt from the message's own text like the
  single card (`extractDeliveryBlocks`, then `groupRunResults`, §19 PR-W.4b). Shown, it lists each part:
  "Agent · title" over its text, or the muted "not included" line.

### 20.4 The sidebar (W.4c)

- Under the source's session row, one line in `text-xs/5 text-gray-600 dark:text-gray-400`, indented to the
  title: "4 Agents · 2 of 4 finished". It is a disclosure button (`aria-expanded`) and is collapsed by default.
- Expanded, the step conversations are the ordinary `sessionRow` template, indented one step, with their own
  streaming, awaiting-input and unread indicators unchanged.
- Collapsed, the line shows the strongest state among its steps: the awaiting-input mark if any step waits,
  else the streaming dot if any runs. A waiting step is never hidden by the fold.
- Pinning, dragging and the date buckets treat a step conversation as today. It nests only when its source is in
  the same bucket on the loaded page; otherwise it is listed alone, as now.
- *Built (PR-W.4c).* As above, with these settled in the build:
  - **Open by default only for the conversation you're in.** The line is collapsed unless one of its steps is
    the current conversation (so opening a step from the run's row shows it highlighted in the sidebar); the
    user's own toggle wins after that, for the life of the page.
  - **The collapsed mark** is needs your input (a step's own row says it, or the run counts a `needs_input`
    step), else streaming. Unread is not shown on the line: the run's toast says when its steps finish, and the
    unread dot's dark shade (`primary-400`) measured 2.59:1 on the sidebar. The mark carries the row's own
    screen-reader text, so the line's name reads "4 Agents · 2 of 4 finished (needs your input)".
  - **Before the run is read** the line reads "*n* Agents" for the steps it lists; "1 Agent" in the singular.
  - **A finished run is not read for its line.** A step conversation's `spawnedFrom` on the list carries
    `runSteps`, the run's kept step count (set on each kept step's `HANDOFF#` row at approve, which fixes it,
    and copied at acceptance in the same background write that sets `spawnedFrom`, from the row acceptance
    already returned), and `settled` once its handoff is done (the flag the Runtime already wrote to stop
    following the step's turns; a `done` row is frozen). A run whose listed steps are all settled **and** number
    `runSteps` reads "*n* Agents · *n* of *n* finished" from the list, and `GET …/workflow-runs/{runId}` runs
    only when its line is opened (for the step order), once. Anything less is read as before: a step still
    going or stopped, a step not started yet (a later stage waiting on the ones it reads, or "Not started"), a
    step off the loaded page or in another bucket, a step known only from this tab's live rows, or a row from
    before `runSteps` existed. So a doubt always costs a read, never a false "finished", and a page load reads
    only the runs still going, however long the user's run history.
  - The steps' list is `ml-3.5 border-l`, the project group's indent. The line takes `aria-controls` only while
    open, so it never names an element that isn't there.
  - **Contrast, measured** in the running SPA against the composited sidebar: the line's text and chevron
    **6.87:1 light**, **6.82:1 dark**; the nested rows 9.36:1 / 6.82:1. Marks (3:1): needs your input 4.57:1
    light, 10.3:1 dark; streaming (`secondary-500`) 4.09:1 / 3.94:1. The focus ring is `primary-500` light
    (9.63:1) and **`primary-300` dark (3.98:1)**: `primary-500` measured 1.67:1 on the dark sidebar.

### 20.5 Copy

| Where | Text |
|---|---|
| Sheet title, button | "Hand off to *N* Agents", "Start *N* Agents" |
| Toast on start | "Handed off to *N* Agents" |
| Row count | "*k* needs you", "*k* of *N* finished" |
| Queued line | "Queued" |
| Actions | "Bring back *k* results now", "Bring back results", "Stop run" |
| Result card | "Results from *N* Agents", "*k* of *N* finished", "Stopped, not included", "Still waiting, not included" |
| Toast when all settle | "*k* Agents finished · Open" (scrolls to the row; *k* is the steps that finished) |
| Not started (after a reload) | "Not started", "Start *k* Agents"; a stopped run's: "Didn't start" |
| Sidebar | "*N* Agents · *k* of *N* finished" |
| A step conversation's title | "*Agent* · *generated title*", e.g. "Budget reader · Proposal review" (§19, step titles) |

No user-facing "workflow" or "run" in a noun phrase except "Stop run" (§17, question 1). Sentence case, no
exclamation marks, no "successfully".
