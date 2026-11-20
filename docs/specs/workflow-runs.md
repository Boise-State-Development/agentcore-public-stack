# Workflow runs: several steps under one approval

**Status:** Accepted for step 1, 2026-11-20 (§19 is its delivery plan). Proposed 2026-11-19. PR-W.1 (the run row, its routes and the flag) built 2026-11-20. It asks one design question of the handoffs work: should the
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
   the merge instruction and the merge that starts by itself.
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

- A canvas or graph editor.
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
- **PR-W.3 — One brief per run.** Runtime, rides `HANDOFF_BRIEF_ENABLED`.
  - `handoff_brief: {handoffId, runId?}`. With `runId`, the Runtime reads the run row and asks once with a run
    variant of the instruction that names every step's Agent and request, and records `briefHash` on the run row
    and on each step row, so acceptance compares each step's card against it unchanged.
  - The final message is the only thing that differs from a single-handoff brief, so the cache reading is the
    same: a route test sends one body as a turn and as a run brief and asserts identical `get_agent` arguments.
- **PR-W.4a — Start a run.** SPA. The design is §20.1.
  - Several `@`-mentions in one message, up to 8, each a chip in the composer.
  - The run sheet (§4), one brief streamed into one shared card, a request per step that defaults to the shared
    one, the files as checkboxes.
  - On approve, the queue: four steps at once through `AgentHandoffService.send`, the rest started as earlier ones
    settle (`done`, `needs_input` or `stopped`). The queue lives as long as the tab; a reload shows unstarted steps
    as "Not started" with one Start button.
  - ⚠️ **Verify first:** five streams at once on one origin (the source and four steps) under HTTP/2 end to end.
    On HTTP/1.1 the browser's six-connection limit would stall the rest of the page.
- **PR-W.4b — The run in the source, and its result.** SPA. The design is §20.2 and §20.3.
  - The run's row in the source thread: **one card with a line per step** (decided 2026-11-20, over a single
    collapsed line). The header names the run and counts what needs the user; a segmented bar shows each step's
    state; each line is the step's handoff row in small (status mark, Agent, live status line, a link to its
    conversation). A step's relayed prompt opens under its own line, never behind a click, because a hidden
    prompt is the stall §8 is about. Under the lines: Bring back *N* results now (a partial result) and Stop run.
  - The grouped card on the composer, Preview and Edit per part, attached by default; the sent bubble rebuilds it
    from its own text.
  - The bell gets one entry for the run, plus one per step that needs input.
- **PR-W.4c — Step conversations nested in the sidebar** (open question 3). Backend and SPA, after W.1. The design
  is §20.4.
  - The step's `spawnedFrom` gains `runId`, written by the background `SET` after acceptance that already writes
    it (`HandoffTurn`), so nothing moves onto the first-token path.
  - ⚠️ **Verify first:** whether the session list already returns `spawnedFrom`; if not, add it, additive.
  - The sidebar shows a run's steps as one line under the source ("4 Agents · 2 of 4 finished") that expands to
    the step conversations. A step conversation whose source is not on the loaded page is listed as today.
- **Dev validation** (each PR's own session, after merge): the worked example's four reviewers on one proposal, with
  one step needing a tool approval answered from the source, one stopped mid-run, a partial result brought back,
  and the full result sent. The source's `C#` rows show no prefix change beyond the appended block, and the
  attach's TTFT number is recorded.

Settled with the developer on 2026-11-20: the run's row (W.4b), the sidebar (W.4c), the wording and the cost
rule (§17, questions 0 to 3).

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
  everything it shows in text. Fills, all non-text (3:1): done `bg-state-success-600`, running `bg-primary-500`,
  needs input `bg-state-warning-600`, stopped `bg-gray-400`, queued `bg-gray-200`, each with a dark pair. W.4b
  measures them in both themes, as PR-0.4a measured its labels.
- **A step line** is the handoff row's link in small: the same status marks (the pulsing dot with
  `motion-reduce:animate-none`, `heroExclamationCircle`, `heroCheckCircle`, `heroMinusCircle`), the Agent's name,
  the same status line from the same sources, and a chevron. Each line is one tab stop, and its accessible name is
  the row's. A queued step reads "Queued" and links nowhere until it starts.
- **The relayed prompt** is `<app-handoff-relay>` under its own line, open, never behind a click.
- **Below the card,** as siblings and never inside a link, in the row's `text-xs/5` link-button style: "Bring
  back *N* results now" while some are still running, "Bring back results" once all have settled, and "Stop run".
  Stop run asks no confirmation: the steps stopped so far keep their results, and each has Continue.
- **Not a live region,** as the row isn't. The toasts announce changes.

### 20.3 The result card (W.4b)

The look of `DeliveryCardsComponent`: the same container and footer, with one row per part.

- **Header:** "Results from 4 Agents" and, in meta, "3 of 4 finished".
- **Parts:** a row each, with the Agent's name and that card's Preview (the same `pre`) and Edit. A part that
  stopped or still waits is a muted row with no actions: "Fiscal outsider · Stopped, not included".
- **Footer:** "Goes with your next message" and Remove, as today. The card takes one of the three per-send slots.
- **The sent bubble** shows "From 4 Agents" with Show results, rebuilt from the message's own text like the
  single card (`extractDeliveryBlocks`).

### 20.4 The sidebar (W.4c)

- Under the source's session row, one line in `text-xs/5 text-gray-600 dark:text-gray-400`, indented to the
  title: "4 Agents · 2 of 4 finished". It is a disclosure button (`aria-expanded`) and is collapsed by default.
- Expanded, the step conversations are the ordinary `sessionRow` template, indented one step, with their own
  streaming, awaiting-input and unread indicators unchanged.
- Collapsed, the line shows the strongest state among its steps: the awaiting-input mark if any step waits,
  else the streaming dot if any runs. A waiting step is never hidden by the fold.
- Pinning, dragging and the date buckets treat a step conversation as today. It nests only when its source is in
  the same bucket on the loaded page; otherwise it is listed alone, as now.

### 20.5 Copy

| Where | Text |
|---|---|
| Sheet title, button | "Hand off to *N* Agents", "Start *N* Agents" |
| Toast on start | "Handed off to *N* Agents" |
| Row count | "*k* needs you", "*k* of *N* finished" |
| Queued line | "Queued" |
| Actions | "Bring back *k* results now", "Bring back results", "Stop run" |
| Result card | "Results from *N* Agents", "*k* of *N* finished", "Stopped, not included", "Still waiting, not included" |
| Toast when all settle | "*N* Agents finished · Open" (scrolls to the row) |
| Sidebar | "*N* Agents · *k* of *N* finished" |

No user-facing "workflow" or "run" in a noun phrase except "Stop run" (§17, question 1). Sentence case, no
exclamation marks, no "successfully".
