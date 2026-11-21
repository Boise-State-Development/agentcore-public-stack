# Workflow Designer: authoring a saved workflow on a canvas

**Status:** Step 2 (D.1 to D.5) built and dev-validated, 2026-11-21, behind `WORKFLOWS_ENABLED` and `WORKFLOW_RUNS_ENABLED` (on in `development` only). §20 is its delivery plan and §20.5 what it showed. Prompt and checklist steps (D.6, D.7) and pinning (D.8) are decided later. It is the authoring half of step 2 in
`docs/specs/workflow-runs.md` §14, and it replaces that spec's §10 editor (step cards in a list) with a canvas.
**Targets branch:** `develop`
**Tracking:** #2218 (step 2)
**Mockup:** `docs/specs/mockups/workflow-designer-mockup.html` (standalone, clickable, drawn outside the SPA; this
file is the source, not the picture)
**Related:**
- `docs/specs/workflow-runs.md`: steps, runs, the approval sheet and its envelope (§3, §4), results (§7), the
  step-1 build and its UI (§19, §20). This proposal changes nothing about how a run executes
- `docs/specs/field-set-authoring.md`: the pattern this copies for the file (the server owns the format in both
  directions) and the lessons it keeps (declared order, a visible ceiling, keyboard first)
- `docs/specs/structured-extraction.md` §4: why a named shape lives in a skill
- `docs/specs/agent-designer.md` and `agents/agent-form/`: the page layout this borrows (a design surface beside
  a live preview)
- `docs/specs/multi-agent-patterns.md` §6.2 and §11: the long-term canvas, and the worked example used throughout

---

## 0. Summary

A workflow is a saved run shape: its steps, what each reads, and how the result comes back. The runs spec planned
to author it as a list of step cards. This proposal makes it a designer that looks like a CI run graph and is as
easy as a form:

- **The canvas arranges itself.** Steps sit in columns called stages. A user never places a card or draws a line.
  Ticking "reads Generalist's result" on a step is what moves it to the stage after Generalist, and the connector
  appears by itself.
- **The preview is the approval sheet.** The panel beside the canvas shows exactly what a person starting the
  workflow will see. Its Start button runs a real test, and the canvas becomes the live status view.
- **One graph for designing and watching.** The same component draws the editable canvas and the running one.
- **A top-level Workflows page**, beside Agents and Projects.
- **Stored as a skill reference file**, `references/workflow.<name>.md`, written and read only by the server. The
  designer edits structured JSON. The user sees a workflow and never has to think about the skill.

The first build runs on step 1's engine as it is: Agent steps, in one stage, started from one sheet. Stages and
the prompt and checklist step kinds are added afterwards without changing the designer.

## 1. Why a designer, and what changed since the runs spec

`workflow-runs.md` §10 said "a canvas is not needed until workflows branch", and listed a canvas as a non-goal
(§18). Two things changed:

- **The reference picture is a layout, not a drawing tool.** A CI run graph has columns of cards joined by
  connectors, and nobody positions anything in it. That shape costs little more than a list to build, and it
  shows the one thing a list hides: which steps wait for which.
- **Authoring is where users meet this feature.** A run is something that happens in a thread. A workflow is
  something a person makes, names and shares. The Agent Designer set the expectation for what making something
  here feels like: a design surface, a live preview, and no file format in sight.

What did **not** change: the platform runs the shape, a person approves every run, and the envelope is 8 steps,
4 at once, depth 1. The designer must never offer a graph the envelope refuses.

## 2. What already exists

| Piece | Where | What the designer reuses |
|---|---|---|
| The approval sheet for several Agents | `workflow-runs.md` §20.1 (PR-W.4a) | The Preview panel is this sheet |
| The run's row with a line per step, and the grouped result card | §20.2, §20.3 (PR-W.4b) | The test run's panel, and the list form of the graph |
| `WFRUN#` rows, create, approve, stop, collect | `apis/shared/handoff/runs.py`, the handoff routes | A test run and a saved workflow's run are ordinary runs |
| `WORKFLOW_RUN_MAX_STEPS = 8`, `WORKFLOW_RUN_CONCURRENCY = 4` | `runs.py` | The limits the canvas shows |
| A skill's reference bundle, with ownership, versions, sharing, role grants and bindings | `apis/shared/skills/`, `/skills/mine/*` | Where a workflow is stored |
| `parse_field_set` / `serialize_field_set`, the structured `PUT` with `?validate=true`, the check on every skill-file write | `apis/shared/extraction/field_sets.py`, `app_api/skills/routes.py` | The same pattern for `workflow.<name>.md` |
| The Agent Designer's page: a scrolling editor beside a preview pane, `fixed inset-0` | `agents/agent-form/agent-form.page.html` | The page layout |
| Agent version snapshots, the version list and the version diff | `apis/shared/assistants/version_resolution.py`, `AgentVersionSummary`, `AgentVersionDiffResponse` | "This Agent has changed" on a step (§10) |
| The sidebar's entries, each with an optional feature flag | `components/sidenav/sidebar-items.ts` | One new entry |

What does not exist: the file format, any route that reads or writes it, a way to start a run from a saved shape,
a graph component, and a run that starts from a page other than a conversation with turns (§6.3).

## 3. What the designer edits, and what is stored

### 3.1 The parts of a workflow

| Part | What it is | On the canvas |
|---|---|---|
| **Starts with** | What the person starting it is asked for: the files (none, exactly one, or any) and the request, with an optional default | A fixed card in the first column |
| **Step** | One piece of delegated work, of a kind: `agent`, `prompt` or `checklist` (`workflow-runs.md` §3.1, §6.4) | A card in a stage |
| **Reads** | What a step is given: the request, the run's files, and the results of other steps | Checkboxes on the step. They are the graph's edges |
| **Result** | What comes back to the conversation that started it: which steps' results, as a card or as a merge with an instruction | A fixed card in the last column |

The two fixed cards mean an empty workflow already has a shape: a start, an end, and one Add step between them.

### 3.2 Decision D1: a structured model on the wire, a skill reference file at rest

**The designer edits JSON. The server writes `references/workflow.<name>.md` in a skill. The SPA never parses or
writes the file.** This is `field-set-authoring.md` D2 again, for the same reason: a format with two
implementations has two answers.

What each choice costs:

| | Skill reference file (chosen) | A first-class `Workflow` entity |
|---|---|---|
| Storage | None new | New rows, and a way to list them per user, likely an index |
| Access | The skill's visibility and role grants, already enforced | A new RBAC noun and its admin surface |
| Sharing | The skill's | A second share model |
| Versions | The skill's | Built again |
| Bindings to Agents and projects | The skill's | Built again |
| Marketplace | Whatever skills get | A second listing type and review path |
| Field sets for checklist steps | In the same bundle, resolved by name | A reference across two entities |

The entity would only win if a workflow needed something a skill cannot give. The one candidate found is sharing
a workflow with people who cannot use one of its Agents, and §11 handles that on the sheet.

**The file holds no coordinates**, because the canvas arranges itself (§4). It stays a table a person can read,
and a diff of two versions is a diff of steps.

**The carrying skill is invisible in the designer.** Creating a workflow creates a skill to hold it, named after
it. A skill someone built by hand may also carry one or more workflow files, and each lists as a workflow.

### 3.3 No markdown mode in the designer

The field-set editor has an "Edit as markdown" mode because its format allows things the form cannot show. Here
the canvas can show everything the format allows, so the designer has no second mode. Anything the format carries
that the designer does not show (prose, an extra frontmatter line) rides through a structured save untouched, as
`FieldSetLayout` does for field sets. The raw file is still viewable from the skill's Supporting files.

## 4. The canvas

### 4.1 Decision D2: a graph that arranges itself

**Columns are stages. A step's stage is derived from what it reads, and is never stored or set by hand.**

- A step that reads no other step is in stage 1.
- A step that reads another step's result is in the stage after the latest one it reads.
- The Result card is always last. The Starts with card is always first.
- Connectors are drawn from each step to the steps that read it, and from Starts with to stage 1.
- Within a stage, steps keep their authored order. **Move up** and **Move down** change it, and that order is the
  order of the parts in the result.

So there are two controls and each does one thing: Reads decides *when* a step runs, and Move up / Move down
decides *where it is listed*. Neither needs a pointer.

```
 Starts with        Stage 1 · 4 at once        Stage 2            Result
┌──────────────┐   ┌────────────────────┐   ┌────────────────┐   ┌──────────────┐
│ The proposal │──▶│ Generalist         │──▶│ Review summary │──▶│ One report   │
│ 1 PDF        │   │ Agent · Sonnet 5.5 │   │ Prompt         │   │ Back to chat │
└──────────────┘   ├────────────────────┤   └────────────────┘   └──────────────┘
                   │ Colleague          │──▶        ▲                    ▲
                   │ Emotional outsider │──▶        │                    │
                   │ Fiscal outsider    │──▶────────┘                    │
                   │ Audit · Checklist  │────────────────────────────────┘
                   │ + Add step         │
                   └────────────────────┘
```

**Adding a step.** Each stage ends with **Add step**, which offers the three kinds with one line each on what
they are for. A step added under stage 2 starts out reading every step in stage 1, which is what puts it there.
The author unticks what it should not read.

**What the canvas refuses, and how.** It never shows an error for a shape the user could not have meant:

- A cycle cannot be made. On step A, the checkbox for a step that already reads A is disabled and says "reads
  this step".
- A ninth step cannot be added. The canvas shows "7 of 8 steps" from the start, and at 8 the Add step control
  says "8 of 8 steps" (`field-set-authoring.md` §5.3: a cap that appears only on save wastes an afternoon).
- A stage with more than four steps is allowed and labelled "4 at once", because the run queues the rest.
- A step cannot add a step, and a workflow cannot contain a workflow. Neither has a control.

**One cost, accepted.** "Runs after X but does not read X" cannot be expressed. Nobody has asked for it. If
someone does, it is a second checkbox column ("waits for"), not a change of model.

### 4.2 Moving around: scroll, drag the background, and Fit

The envelope bounds the graph. The tallest case is 8 steps in one stage, about 500 px. The widest is 8 steps in a
chain, 10 columns and about 2,300 px, which overflows most screens.

- **Pan is native scrolling.** Trackpad, scrollbars and arrow keys work with no code. The scroll region is
  focusable and labelled. A card scrolls into view when it takes focus. Dragging the empty grid also pans.
- **Zoom has two states: actual size and Fit.** Fit scales the whole graph to the canvas. It never goes below
  60 percent: under that a card cannot be read, so the canvas scrolls.
- **No wheel zoom and no pinch zoom.** They take over page scrolling and the browser's own zoom, which people
  with low vision rely on.

Stepped zoom can be added later without touching the model, because no coordinates are stored.

### 4.3 Drag is an extra

Dragging a card up or down within its stage reorders it, as Move up and Move down do. Dragging between stages
does nothing, because a stage is not a place a step can be put. Drag ships after the buttons, never instead of
them (`field-set-authoring.md` D4).

### 4.4 Accessibility

- **The canvas is a list of stages, each a list of steps**, in DOM order. A stage is a labelled group. A card is
  a button whose name carries what the picture shows: "Review summary, prompt step, stage 2, reads 4 results".
- **Connectors are hidden from assistive technology.** Every edge is already stated in text on the card it ends
  at and in that step's Reads.
- **Focus stays with what moved.** Move up, Move down and a Reads checkbox keep focus on their control after the
  graph redraws. A polite announcement says what changed: "Review summary moved to stage 2".
- **Status is never colour alone.** A run-mode card shows a mark and a phrase, as the run's row does
  (`workflow-runs.md` §20.2).
- **Reduced motion** stops the running pulse and the connector transition.
- **At phone width** the same DOM stacks: stages become headed sections one above another, connectors are not
  drawn, and each card's own text carries the order.
- Colours, focus rings and the Fit control are measured in both themes before merge, as PR-0.4a measured its
  labels.

## 5. The page

The Agent Designer's layout: a full-screen page with a header (back, the workflow's name, saved state, Share,
Save workflow), the canvas on the left taking the spare width, and a panel on the right with two tabs.

**The Step tab** edits the selected card:

| Card | Fields |
|---|---|
| Starts with | Files (none, exactly one, any), whether a request is required, a default request |
| Agent step | Name, the Agent (picked from those the author can use, shown by name and icon), its model as read-only meta, the run's request or its own, Reads, Move up / Move down, Remove |
| Prompt step | Name, the instruction (a textarea, the widest control, as `find` is on a field card), model, Reads, Move, Remove |
| Checklist step | Name, the field sets it runs (from this workflow's skill), Reads, Move, Remove |
| Result | A card on the composer or a merge that starts by itself, the merge instruction, which results it includes |

**The Preview tab** is §6.

**House style.** The canvas is the page's one bold surface: the graph-paper grid and two still blobs in the brand
blue, with frosted cards on top. The panel is a dense form, so it stays plain. Tokens are
`.claude/skills/tailwind-ui/references/app-conventions.md`'s: `rounded-2xl`, `text-sm/6` and `text-xs/5`,
`primary-accessible` for the one solid button and for brand text, neutral fills for tiles and chips, `state-*`
for status, and a `dark:` pair on everything.

## 6. Preview and test run

### 6.1 Decision D3: the preview is the approval sheet

**The Preview tab shows the sheet a person starting this workflow will see**, built by the server from the
current draft: the steps with their models, the file it asks for, the one brief, and the estimate.

- **It is free.** Building the sheet makes no model call. It is one `PUT …?validate=true` with the draft, which
  also returns anything wrong with it, in the parser's words.
- **A sample file makes it concrete.** With one attached, the estimate is a real figure and each step's full
  prompt can be opened, still with no model call. This is the "dry run", without a separate mode.
- **It says what a person may change.** The sheet's Edit and Remove controls are shown as they will be, so the
  author sees that a starter can drop a step.

### 6.2 The test run is a real run

**Start test run** on that sheet starts the workflow for real: the author's quota, real conversations, real
results. Pressing it is the approval, so "a person approves every run" holds here too.

- The canvas switches to run mode (§7). The panel shows W.4b's run row, with relayed prompts under the step that
  raised them, and then W.4b's result card.
- A step that pauses holds only itself, as in any run.
- Everything it made can be opened afterwards. A note on the sheet says so, and says it spends quota.
- **Try in a chat** is the lighter path: it opens a new conversation with the sheet already up.

**Not chosen: a throwaway preview run**, like the Agent Designer's unpersisted `preview-` session. Step
conversations have to be real for handoffs, relays and results to work, so a preview run would be a second
execution path that could disagree with the real one.

### 6.3 What a test run needs that step 1 does not have

- ⚠️ **Verify first:** a run needs a source conversation, and W.1's create needs that session's row, which today
  exists only once a conversation has turns (W.4a's review). A test run, Try in a chat, and Start on the
  Workflows page all begin from a new conversation. D.2 has to make that work, and should say how the source is
  titled ("Test: Proposal review").
- ⚠️ **Verify first:** the tab runs the queue in step 1. Confirm the queue and its streams survive leaving the
  designer route, since `ChatStateService` is app-wide and the designer is one more page in the same tab.
- Test conversations collect in the sidebar under their source (W.4c). Whether they need their own grouping is
  open question 8.

## 7. Decision D4: one graph, two modes

**One `WorkflowGraphComponent` draws both the designer's canvas and a run's status.** It takes the steps and
their reads, and optionally a status per step.

| Mode | Cards show | Connectors | Add step, Move, Reads |
|---|---|---|---|
| Design | Kind and model | Neutral | Shown |
| Run | The status mark and phrase from the run's row, and elapsed time when finished | Drawn in the brand colour once the step they leave has finished | Hidden |

**In the chat thread, W.4b's row stays.** It is the list form of the same data and it is dense enough for a
thread. For a run that came from a workflow with more than one stage, the row gains **View as graph**, which
opens the graph in run mode. A run of parallel Agents with no stages is one column, so the row alone is right for
it, and step 1's UI is unchanged.

**On the approval sheet** for a saved workflow, a small read-only graph sits above the step rows once stages
exist (D.5), so the person approving sees what waits for what.

## 8. Where it lives, and how one starts

### 8.1 Decision D5: a top-level Workflows page

**A sidebar entry, Workflows, beside Agents, Projects, Artifacts and Schedules.** Customize holds things a user
attaches to something else (tools, skills, connectors). A workflow is something a user opens and runs.

`field-set-authoring.md` D1 refused a top-level list for field sets because a set has no owner on screen and is
never shared by itself. Neither is true of a workflow.

Routes, all `canMatch` on the flag:

```
/workflows                         the list: yours, then shared with you
/workflows/new                     the designer, empty or from a template
/workflows/:skillId/:name          the designer
```

The sidebar entry is one line in `sidebar-items.ts` with `feature: 'workflows'`, visible by default where the
flag is on, as Projects is.

**The name.** "Workflows", in the sidebar and in copy. The runs spec left the user-facing name open until saved
workflows existed (§17, question 1); this answers it. Step 1's copy ("Hand off to 4 Agents") is unchanged for a
run started by mentions.

### 8.2 Three ways in

1. **Save as workflow**, on a finished run's row in a conversation. The user has already done it by hand with
   mentions, and the designer opens with those steps filled in. This is the entry point to lead with, because the
   run they just watched is the proof that it works.
2. **From a template.** The worked example ships as the one sample: four reviewers and a result.
3. **From blank.** Starts with, Result, and one Add step.

### 8.3 Starting a saved workflow

- From the Workflows page: **Start** opens a new conversation with the sheet up.
- From a conversation: the composer's `+` menu lists the user's workflows.
- Either way the same sheet opens, titled with the workflow's name, and it shows again on every start
  (`workflow-runs.md` §4).

## 9. The file and the API (sketch)

### 9.1 The file

Frontmatter for the workflow's own facts, one table whose rows are the steps in authored order, and a section for
any instruction too long for a cell. D.1 fixes the exact grammar; this shows its shape.

```markdown
---
title: Proposal review
version: 3
files: one
request: Review this proposal against the solicitation
result: merge
---

| step           | kind      | worker              | reads                                           |
|----------------|-----------|---------------------|-------------------------------------------------|
| generalist     | agent     | <agent id> @ 5      | request, files                                  |
| colleague      | agent     | <agent id> @ 3      | request, files                                  |
| emotional      | agent     | <agent id> @ 2      | request, files                                  |
| fiscal         | agent     | <agent id> @ 2      | request, files                                  |
| audit          | checklist | nsf-audit           | files                                           |
| review_summary | prompt    | <model id>          | generalist, colleague, emotional, fiscal        |

## Result

Reads: review_summary, audit

Write one report: the inline notes first, then the audit table.

## Step: review_summary

Merge the four reviews into inline notes. Keep each reviewer's strongest point and say where they disagree.
```

- *2026-11-20, PR-D.1:* the request is a `## Request` section, not a frontmatter line; the exact grammar is
  the *Built (PR-D.1)* note in §20.3.
- Ids appear only in the file. The designer shows names and icons.
- `reads` is the whole graph. There is no `after` column and no stage column.
- The parser enforces the envelope: at most 8 steps, no cycle, a step reads only steps that exist, a checklist
  names a field set in the same skill. A file that breaks one is refused with a message that says which.
- **The check runs on every skill-file write, whether or not the flag is on**, as it does for field sets, so a
  broken file cannot collect quietly until a deployment turns the feature on.

### 9.2 Routes

On app-api, under `/workflows`, behind `get_current_user_from_session`, with the flag checked after auth. Shared
code in `apis/shared/workflows/`, a pure module beside the parser, as `field_sets.py` is.

| Route | Does |
|---|---|
| `GET /workflows` | Lists workflows in skills the caller owns, and in skills they can use |
| `POST /workflows` | Creates the carrying skill and the file. With `fromRun: {sessionId, runId}`, fills the steps from that run |
| `GET /workflows/{skillId}/{name}` | The structured definition, each step with its display name, icon, model and what has changed (§10) |
| `PUT /workflows/{skillId}/{name}` | Writes it. With `?validate=true`, writes nothing and returns the normalised definition and the sheet it would open. A save that changes nothing writes nothing and does not move `version` |
| `DELETE /workflows/{skillId}/{name}` | Removes the file, and the carrying skill if the designer made it and it holds nothing else |
| `POST /sessions/{id}/workflow-runs` | Gains an optional `workflow: {skillId, name}`. The server reads the file and builds the steps. The `WFRUN#` row records the workflow's name and a fingerprint of the definition (`workflow-runs.md` §9) |

No new table, no new index, no new SSE event.

## 10. Decision D6: record the Agent's version now, pin when a turn can

`workflow-runs.md` §10 says an Agent step is pinned to a published version. **A turn cannot do that today.**
`resolve_invocation_agent` serves the current published snapshot to everyone but the owner, and the owner's own
draft to the owner. Neither a handoff nor a turn request carries a version.

So this comes in two parts:

- **From D.1, the file records the version** of each Agent when the step was added or last reviewed. The format
  never has to change for pinning.
- **From D.2, the designer and the sheet say when an Agent has moved on.** A step whose Agent has a newer
  published version shows "This Agent has changed since you added it. A run uses its published version, now
  version 4", with **See what changed** (the existing version diff) and **Mark as reviewed**. The sheet carries
  the same line. That meets the reason for pinning, which is that an edit to a reviewer must not change a
  workflow *silently*.
- **True pinning is its own PR (D.8)**, because it is a turn-path change: an optional version on the step's
  turn, read where the published snapshot is read now, and part of the agent cache key. It also needs a policy
  answer first (open question 2): may a run use a snapshot that has been superseded, and what happens after a
  takedown.

An Agent's owner who puts their own Agent in a workflow runs their draft when they test, and others run the
published version. The designer says so on that step.

## 11. Access, sharing and what happens when a step is unavailable

- **Who can start a workflow:** anyone who can use the skill that carries it. Nothing new is checked.
- **Each Agent step is checked as the starter**, as a mention is. Sharing a workflow does not share its Agents.
- **A step the starter cannot run** is shown on the sheet with the reason, and Start is off until they remove it
  or get access. This is the Agent Designer's block-on-missing rule (D5 there), with the sheet's existing Remove
  as the way forward. Whether a removed step's readers should still run is open question 4.
- **The Share dialog** is the skill's. It should also say when the people being added cannot use one of the
  steps' Agents. That needs a per-recipient check and is open question 3.
- **Shared Projects:** a run cannot start in a project in step 1 (`workflow-runs.md` §19). The designer does not
  change that.
- **Export and import** travel with the skill. Agent ids do not survive a move to another deployment, so an
  imported workflow opens with those steps marked "Choose an Agent".

## 12. Cost, the prompt cache and TTFT

- **Nothing here runs before a first token.** Designing, previewing and saving happen outside any turn. A
  workflow's file is read when a run is created, which is a user action outside a turn.
- **No tool spec and no system prompt changes.** The carrying skill's `SKILL.md` is written once and does not
  list the steps, so editing a workflow does not move the prefix of an Agent that has the skill bound.
  ⚠️ **Verify first:** that a reference file's name or count does not reach any prompt through the skill's
  disclosure.
- **The preview makes no model call.** A test run costs what a run costs, and the sheet says so before Start.
- **Stages (D.5) append.** A later step receives earlier results in its first message, as a delivery's parts are
  received today. Nothing cached is rewritten.
- **The estimate** is the sheet's, unchanged, and applies the per-model tokenizer factor (W.1's review).

## 13. Flags

`WORKFLOWS_ENABLED` / `CDK_WORKFLOWS_ENABLED` on app-api, and `features.workflows`. In development: only `"true"`
enables, and only with `WORKFLOW_RUNS_ENABLED`. Off by default. No Runtime variable.

It exists because the work lands over several PRs. When it is finished, the recommendation is to **fold it into
`WORKFLOW_RUNS_ENABLED`** instead of keeping a second permanent switch: a saved shape adds nothing the sheet and
the envelope do not already bound (open question 5).

## 14. Build order

Each PR is useful alone. D.1 to D.4 need nothing new from the run engine or the Runtime.

- **PR-D.0: land this spec.** Docs only. It also adds dated notes to `workflow-runs.md` and
  `multi-agent-patterns.md` that point here (§19).
- **PR-D.1: the format and its routes.** Backend. The parser and writer in `apis/shared/workflows/`, the check on
  every skill-file write, the flag with its CDK, workflow variable and docs-site row, and the `/workflows` routes
  including `?validate=true`. Agent steps only. Tests pin the format, the envelope refusals and the no-op save.
- **PR-D.2: start a saved workflow.** Backend and a little SPA. `workflow` on run create, the name and
  fingerprint on the `WFRUN#` row, a run from a new conversation (§6.3), the composer's `+` entry, and the
  "this Agent has changed" line on the sheet. After this a hand-written workflow file runs.
- **PR-D.3: the Workflows page and the designer.** SPA. The sidebar entry, the list, `WorkflowGraphComponent` in
  design mode, the Step tab for Agent steps and the two fixed cards, the Preview tab as the sheet, Try in a chat,
  Save as workflow on a run's row, the one template, Fit and background pan. Checked in the real app in both
  themes and at 375 px.
- **PR-D.4: the test run on the canvas.** SPA. Run mode on the graph, the run row and result card in the panel.
- **PR-D.5: stages.** Run engine and SPA. Reads of other steps' results, a step that starts when what it reads
  has settled, earlier results in a later step's first message, the merge that starts by itself and its
  instruction, View as graph on the run's row, the small graph on the sheet.
- **PR-D.6: prompt steps.** The side-call path for a run (`workflow-runs.md` §6.4), its `callKind`, and the kind
  in Add step. ⚠️ **Measure first:** whether prompt steps can read the document from a shared cached prefix
  (`multi-agent-patterns.md` §11.4, G3).
- **PR-D.7: checklist steps.** The same path over field sets, with the field-set editor reached from the step.
- **PR-D.8: true version pinning** (§10), after open question 2 is answered.

After D.4 a user can design, preview, test, save, share and start a workflow of up to 8 Agents. Until D.5 the
canvas is one column between Starts with and Result, which is plainer than the mockup.

**Not chosen: stages first.** Building D.5 before D.3 would make the first designer people see the multi-column
one. It was not chosen because the designer would ship later and on engine work nobody had yet used.

## 15. How we'll know

Counts only, on rows and in EMF:

- workflows created, by way in (from a run, from the template, from blank);
- workflows saved more than once, and steps per workflow;
- previews opened, test runs started, and test runs per workflow before its first share;
- runs started from a saved workflow, against runs started by mentions. This is the number that says whether
  authoring earns its place;
- sheets edited at start (a step removed, a request changed), per `workflow-runs.md` §15;
- "this Agent has changed" lines shown, reviewed and ignored, which sizes D.8;
- Fit pressed, and workflows with more than four steps, which says whether stepped zoom is ever needed.

## 16. Alternatives considered

- **A free-form canvas** (place cards anywhere, draw edges). It invites cycles, branches and a ninth step, so
  most of the work would be error states. It needs stored coordinates, which ends the readable file. It is the
  hardest surface to make keyboard and screen-reader usable. At 8 steps it is mostly empty space to tidy.
- **Explicit stages the user drags steps between.** Closer to the picture, but it makes order and data two
  things to keep in agreement: a step in stage 2 that reads nothing from stage 1, or reads something in stage 3.
  Deriving the stage from Reads leaves one thing to get right.
- **The list of step cards** from `workflow-runs.md` §10. Cheapest, and it is what the canvas becomes at phone
  width and for a screen reader. As the only view it hides what waits for what once stages exist.
- **A first-class `Workflow` entity.** §3.2.
- **Under Customize, beside Skills,** or **inside the Agent Designer.** §8.1. A workflow holds Agents, not the
  other way round.
- **A throwaway preview run.** §6.2.
- **Wheel and pinch zoom, or stepped zoom from the start.** §4.2.

## 17. Open questions

1. **Does the carrying skill show under Customize → Skills?** Recommended: yes, marked "Workflow" with a link to
   the designer, because a checklist step's field sets live in that skill and their editor is reached from it.
   Hidden is cleaner for users but leaves files nobody can see.
   *Answered 2026-11-20: shown, marked Workflow, with a link to the designer (§20).*
2. **Pinning policy (D.8).** May a run use a superseded snapshot of an Agent? What does a takedown do to a
   workflow that names the Agent?
3. **Sharing.** Should Share warn when a recipient cannot use a step's Agent? Does an Agent's owner see that it
   is used in someone's workflow (`workflow-runs.md` §17, question 4)?
4. **A step the starter cannot run.** Block until it is removed (recommended), or drop it and run the rest as a
   Shared Project harness does? Once stages exist: does a step that read a removed step still run?
   *Answered 2026-11-20: block until it is removed. Once stages exist, a step that reads a removed step is
   removed with it, and the sheet says so (§20).*
5. **The flag when finished.** Fold into `WORKFLOW_RUNS_ENABLED` (recommended), or keep as a feature switch.
6. **Who writes the merge.** Always the source conversation's model, or may the Result card name an Agent
   (`workflow-runs.md` §17, question 6)? The Result card is where that choice would sit.
7. **Templates beyond the one sample.** Where they are kept, and whether workflows get a marketplace listing of
   their own or ride the skill's.
8. **Test conversations.** Nested under their source like any run, or grouped and cleared separately?
9. **"Runs after but does not read."** Left out (§4.1). Revisit on the first request.

## 18. Non-goals

- Branches, conditions, loops, or a step that adds a step.
- A workflow inside a workflow.
- Runs on the server, and a schedule as a trigger (`workflow-runs.md` step 3 and §17, question 7).
- Comparing variants of a workflow (models per step, wording), which the runs spec also sets aside.
- The in-turn composition canvas in `multi-agent-patterns.md` §6.2. This is the canvas for the cross-conversation
  layer only. If that one is built, it should reuse the graph component and the reads-decide-the-layout rule.

## 19. What this changes in other specs

PR-D.0 adds a dated note at each place below. The notes say what this proposal would change. The text they sit
beside is rewritten when the first build PR lands, as each spec's own rule says.

| Spec | Note |
|---|---|
| `workflow-runs.md` §10 | The list editor and "a canvas is not needed until workflows branch" are superseded by this proposal. The file decision stands. `after` becomes "reads". Pinning becomes record-then-pin (§10 here) |
| `workflow-runs.md` §14, step 2 | Points at §14 here for the PR plan |
| `workflow-runs.md` §17, question 1 | Answered for step 2: "Workflows" |
| `workflow-runs.md` §18 | The non-goal is narrowed from "a canvas or graph editor" to "a free-form canvas" |
| `multi-agent-patterns.md` §6.2 | The first canvas is this one, on the conversations layer |

## 20. Delivery plan, step 2

*Added 2026-11-20, after step 1 shipped (`workflow-runs.md` §19.1) and after checking §2, §6.3, §10 and §12
against `develop` at `bcb0aef9a`.*

### 20.1 Settled with the developer

- **Step 2 is D.1 to D.5:** saved workflows of Agent steps, the designer, the test run, stages and the merge.
  Prompt and checklist steps (D.6, D.7) are decided after D.5, from §20.4's measurement. Pinning (D.8) waits for
  open question 2.
- **The merge starts by itself** once the run settles, only if the source is idle and its composer is empty, and
  the sheet says so before Start. Otherwise the card waits on the composer, as in step 1.
- **The carrying skill is shown under Customize → Skills,** marked Workflow, with a link to the designer
  (open question 1).
- **A step the starter cannot run blocks Start** until it is removed. Once stages exist, removing a step also
  removes the steps that read it, and the sheet names them (open question 4).

### 20.2 What the code check changed

- **A run can start from a new conversation with no new backend route.** W.1's create needs only the source's
  metadata row, not its turns: an empty transcript renders an empty card. `PUT /sessions/{id}/metadata` already
  creates an absent row with a title. The step-1 guards are in the SPA (`hasMessages()` in `session.page.ts`,
  `multipleMentionsOffered` in the composer). So D.2 creates the row with the workflow's name as its title, then
  opens the run. Prewarm writes no row and cannot stand in.
- **The queue already survives leaving the conversation.** `WorkflowRunQueueService`, `ChatStateService` and the
  step streams are `providedIn: 'root'`, and their unload listeners are on the window. The §6.3 verify-first is
  answered. What does not exist off the session page is run *state*: it is hydrated when a source's messages
  load (`message-map.service.ts`). D.4's panel has to load the run itself (`GET …/workflow-runs/{runId}`).
- **The sheet opens pre-filled.** `WorkflowRunService.open(sourceSessionId, RunDraft)` takes any Agents and a
  request, so a saved workflow reuses the sheet rather than building a second one.
- **A carrying skill must never be active in a chat by itself.** An active skill puts its name and description
  in `<available_skills>` (the system prompt), and a top-level `references/field-set.*.md` mounts
  `document_extract`, which changes `toolConfig`. ⚠️ **D.1 verifies first** how a user's own skills become active
  on a turn, and keeps a carrying skill out unless an Agent or project binds it. Within an active skill, the file
  list reaches the model only in the instructions read on activation ("Bundled files"), which is a tool result,
  not the prefix.
- **The skill-file check covers field sets only.** `BundleFiles.add_resource` runs `validate_field_set_upload`
  on every write whatever the flags, but parses only `field-set.<x>.md`, so a `workflow.<name>.md` passes
  unchecked today. D.1 adds the workflow check beside it. Limits that apply: 1 MiB a file, 50 files a skill.
- **Nothing bumps a skill's version on save.** Versions are cut only when something pins one (`pin_current`).
  The file's own frontmatter `version` is what a no-op save must leave alone, as the field-set PUT does; the
  generic upload route writes even when nothing changed, so `/workflows` writes through its own route.
- **"See what changed" has no user route.** An Agent's `listing.published_version` is readable by anyone who can
  use it, so the file can record it and the sheet can say "now version 4". The version list and diff routes are
  admin-only. D.2 ships the line and **Mark as reviewed** without the diff link. A user-facing diff is a
  follow-up, and the line says what it can without one.
- **Confirmed: a turn cannot run a chosen Agent version** (`resolve_invocation_agent` takes none, and
  `InvocationRequest` has no field for one). §10 stands.
- **A step's result can be rendered for a later step.** `_part_for` (`app_api/handoffs/workflow_runs.py`) takes
  the step conversation's answer at its `done` time and bounds it at `SEND_BACK_MAX_TOKENS`, and
  `render_delivery_parts` already wraps parts for a message. Neither depends on collect, so D.5 reuses both.
- **Nothing sends a turn by itself today,** and there is no shared "composer is empty" signal: the draft is
  component state plus local storage. D.5's automatic merge needs both: a source-idle check
  (`chatState.isSessionLoading`) and a composer-empty signal it can read without the composer on screen.
- **Prompt and checklist steps need a new Runtime invocation.** `run_bounded` is a semaphore with no model call.
  The only side call that writes a priced `C#` row with a `callKind` is the handoff brief, routed on the Runtime
  through `_handoff_brief_invocation`, and app-api makes no Bedrock call. That, with G3's cache measurement, is
  why D.6 and D.7 are left out of step 2.
- **The run row has no workflow fields.** `WorkflowRun` gains `workflow {skillId, name, fingerprint}` and the
  Agent versions it was started with, in D.2.

### 20.3 The PRs

Every PR is behind `WORKFLOWS_ENABLED` / `CDK_WORKFLOWS_ENABLED` / `features.workflows` (in development: only
`"true"` enables, and only with `WORKFLOW_RUNS_ENABLED`), and none changes a tool spec or the system prompt.

| PR | Side | Needs | What it ships |
|---|---|---|---|
| D.1 | Backend | — | The format and its routes, as §14 says. Also: the carrying skill kept out of turns (§20.2), the skill's `kind: workflow` mark for Customize, `reads` parsed in full now (so the format never changes) while run create refuses a step that reads another step until D.5 |
| D.2 | Backend + SPA | D.1 | Start a saved workflow: `workflow` on run create, the run row's workflow fields, a run from a new conversation (§20.2), the composer `+` entry, Start on a workflow, the blocked-step rule, and the "this Agent has changed" line with Mark as reviewed |
| D.3 | SPA | D.1 | The Workflows page and the designer in one column, as §14 says: the sidebar entry, the list, `WorkflowGraphComponent` in design mode, the Step and Preview tabs, Save as workflow (a new button on the run's row, which has no menu), the one template, Fit and pan. The Customize badge and link |
| D.4 | SPA | D.2, D.3 | The test run on the canvas: run mode on the graph, and the run's row and result card in the panel, loaded by run id |
| D.5a | Backend + SPA | D.2 | Stages in the run engine only: run create accepts reads; a step starts only when what it reads has settled; just before its send, app-api renders the read steps' results into that step's context (`_part_for`, `render_delivery_parts`), never on a turn's path; the result card takes the Result's reads; removing a step on the sheet removes its readers |
| D.5b | SPA (+ D.5a's row field) | D.5a, D.4 | Reads enabled in the designer, multi-column stages on the canvas, View as graph on the run's row and the small graph on the sheet (moved from D.5a, see below), and the merge that starts by itself (§20.1), with its instruction on the run row |

**Scope change (2026-11-21, settled by the orchestrator).** D.5a is the engine only. Enabling Reads in the
designer, multi-column stages on the canvas, View as graph and the small graph on the sheet move to D.5b with the
automatic merge, because D.4 was editing `WorkflowGraphComponent` at the same time. Until D.5b, a workflow with
stages is written through `PUT /workflows/…` (or by hand), and the designer still shows its Reads as "needs
stages".

*Built (PR-D.1).* The format is `apis/shared/workflows/format.py` (standard library only) and the routes are
`app_api/workflows/routes.py`. Where it differs from the plan, and what it settled:

- **The grammar.** Frontmatter holds `title` (required, one line), `version` (a positive integer), `files`
  (`none` / `one` / `any`, default `any`) and `result` (`card` / `merge`, default `card`). Any other frontmatter
  line is kept and nothing reads it. The step table is found by a header naming `step`, `kind` and `worker`, and
  `reads` is its fourth column. Any other column is kept cell by cell, and its cells follow their step by name
  through a structured save. A `worker` is an Agent id, optionally `@ <n>` for the `listing.published_version`
  recorded when the step was added. `reads` is a set, held and written in one order: `request`, `files`, then
  steps in table order. Sections start at `## Request`, `## Result` or `## Step: <name>`, each at most once, and
  run to the next of those, so an instruction can hold its own table. `## Result` opens with an optional
  `Reads:` line; without it the result takes every step. Prose before and after the table is kept.
- **The request moved out of the frontmatter** (§9.1's sketch had `request:` there), into `## Request`, because
  a request can span lines. A `request:` frontmatter line is refused with a message that says so.
- **The envelope** the parser enforces: at most 8 steps (pinned to `WORKFLOW_RUN_MAX_STEPS`), no step named
  twice or named `request` / `files`, no read of a missing step or of itself, no cycle (the message names the
  steps on it), and requests within `HANDOFF_REQUEST_MAX_CHARS`. `prompt` and `checklist` are reserved and
  refused with "not available yet". A blank workflow (no steps) is valid at rest. What step 1's engine cannot
  start yet is the sheet's to say, not the format's: fewer than 2 steps, the same Agent twice, a step that reads
  another step (until D.5a), and `result: merge` (until D.5b) are `blockers` on the sheet, so the format never
  changes when stages ship.
- **`version` is the server's.** A save whose definition equals the stored one (whitespace and read order
  ignored) writes nothing and leaves `version` alone; any other save moves it by one. The body's `version` is
  never read. The stored file's prose, frontmatter and extra columns are re-applied on the server
  (`carry_layout`), so unlike field sets the SPA never sees or sends a layout.
- **The check on every skill-file write** is `validate_workflow_upload`, beside `validate_field_set_upload` in
  `SkillBundleFiles.add_resource` (the user and admin uploads and `my_skill_write_file`) and `put_imported_files`,
  and beside it in `bundle_import` (a broken workflow is skipped as `invalid workflow: …`). It runs whatever the
  flags say. A `workflow.<x>.md` with a bad name is refused, since it would never list.
- **The carrier.** `POST /workflows` creates a private user skill with `kind: "workflow"` (a new optional field on
  `SkillDefinition`, written only when set, never read by an access check and not a snapshot field, so it moves
  no pinned version) and returns it on `GET /skills/mine` for Customize. Its instructions are a fixed sentence
  that never lists the steps. `DELETE` removes the carrier only when it has `kind: "workflow"` and no other file.
- **Verify-first, answered: a carrier is never active on a turn by itself, and no turn-path change was needed.**
  A plain turn's skills are exactly the request's `enabled_skills` intersected with what the user can reach
  (`_apply_enabled_skills_filter`); absent or empty means none (Skills v2 D6). The SPA sends only skills whose
  preference the user turned on, and an untouched skill is off (`GET /skills/` `isEnabled`), and nothing writes
  that preference for them. An Agent's or project's bindings replace the selection outright
  (`_apply_agent_skills_override`). So a carrier's name and description reach `<available_skills>`, and a field
  set in it reaches `toolConfig`, only when the user turns it on in the picker or an Agent or project binds it.
  The carrier does show in the composer's picker, as every owned skill does.
- **`fromRun`** takes a run that was started (`approved` or `stopped`) from the caller's own conversation; an
  `open` or dismissed run is 409. Its kept steps, in order, become steps named after their Agents, each reading
  `request, files`, with the Agent's current published version. A step's own request is kept only where it
  differs from the run's shared request.
- **The document.** `GET`, `POST` and `PUT` answer `{workflow, sheet, written?}`. Each step carries its Agent as
  the caller sees it (`canRun`, then name, emoji, image, `hasIcon`, `modelId` with its input rate, as W.1's
  create prices a step, `publishedVersion`, `changed` and `runsDraft` for the Agent's owner). An Agent the caller
  cannot use shows no name. `sheet` is `{startable, blockers: [{code, message, step?}]}`. `workflow.fingerprint`
  (`workflow_fingerprint`, version excluded) is what D.2 records on the `WFRUN#` row.
- **Access.** Reading is the skill's: its owner, or anyone `resolve_accessible_skill_ids` lets reach it (role
  grants; ACTIVE only). Writing and deleting are the owner's. A shared skill lists for the people it is shared
  with, and each Agent step is judged as the caller, so sharing a workflow shares none of its Agents.
- **For D.2 and D.3.** The list reads each workflow file once (one S3 GET each, concurrently); its rows carry
  `problem` for a file that no longer parses, and `GET /workflows/{skillId}/{name}` answers such a file with 400
  plus its markdown. No user-facing Agent version diff exists, as §20.2 says.

*Built (PR-D.2).* Start a saved workflow. Where it differs from the plan, and what it settled:

- **Run create.** `POST /sessions/{id}/workflow-runs` takes `workflow: {skillId, name, removedSteps?}` in place of
  `steps` (both, or neither with fewer than two steps, is a 422). The file is read with the skill's access rules,
  as the caller: another user's private workflow, a missing one and a bad name are all 404, and a shared one
  still judges each Agent as the starter. With `WORKFLOWS_ENABLED` off the `workflow` form is 404; a run of
  mentions does not need the flag. The file's request is the shared one unless the body's `request` is not blank.
- **Blockers refuse.** Create answers 422 `{detail: {code: "workflow_blocked", message, blockers}}` with the
  sheet's own blockers while any applies, so the Workflows page and run create cannot disagree: the sheet logic
  moved to `apis/shared/workflows/sheet.py` (with the document models and W.1's `step_model`, re-exported from
  `app_api/workflows/models.py`), and both route modules import it from there. A file that no longer parses is 422.
- **`removedSteps` was not in the plan.** A step the starter can't run blocks the run before it exists, so the
  sheet needs a way to drop it without a run to approve. The sheet opens *blocked* from `GET /workflows/…`,
  listing the step and why; removing it (and, through `without_steps`, every step that reads it, so the cascade
  is ready for D.5a) creates the run without them. Too few steps left is the `too_few_steps` blocker.
- **The run row** gains `workflow: {skillId, name, title, fingerprint, version, files, removedSteps?}`, and each
  step `stepName` and `agentVersion`, all optional and written only when set. `agentVersion` is the Agent's
  published version when the run was created, which is what a non-owner's step runs (§10); `version` and `files`
  were added so approve reads the rule the run was opened with, not a file edited since. `WorkflowRunView` and
  the `workflowRuns` replay carry them. `WorkflowRunCreated` has `runSource: "workflow"` for these runs.
- **`files` at approve**: `none` takes no file and `one` exactly one, else 422 before anything is written. The
  sheet keeps Start off until the rule holds, swaps the ticked file under `one`, and hides the list under `none`.
- **A new conversation.** `WorkflowStartService.start(ref, {sessionId?})` (`src/app/services/workflows/`) reads
  the workflow first, so one that is gone leaves no empty conversation. Without a `sessionId` it takes the
  prewarmed id if there is one, `PUT /sessions/{id}/metadata` with the workflow's title, adds the sidebar row and
  navigates, then opens the sheet. A conversation this tab started with no turns (staged by an attachment) gets
  its row the same way; any other keeps its title. The first turn keeps the title, because the Runtime names a
  conversation only when its row is new (`ensure_session_metadata_exists`). No brief is asked for an empty
  conversation. The empty-conversation layout renders the run sheet too. The step-1 guard is unchanged and now
  a pure function, `mentionsOpenRunSheet`, with a spec.
- **The sheet** is `WorkflowRunService.open(…, RunDraft)` with `draft.workflow`, titled with the workflow. A
  changed step shows "This Agent has changed since it was added. A run uses its published version, now version
  N.", plus "You own it, so your run uses its draft." for the Agent's owner. **Mark as reviewed** is the
  workflow owner's only, a `PUT /workflows/{skillId}/{name}` with that step's `agentVersion` set to the
  published one; the run already opened is unchanged. A `one`-file workflow in a conversation with no files says
  to attach one with + and *look again*.
- **The composer.** The `+` menu's Workflows submenu lists `GET /workflows` (the caller's first, files that no
  longer parse left out), behind `features.workflows`, `features.workflowRuns` and `features.handoffs`, and never
  in a Shared Project or an embedded preview. `features.workflows` is on in `environment.development.ts`.
- **For D.3.** Wire types are in `src/app/services/workflows/workflow.models.ts` and the client in
  `workflows-api.service.ts` (`list`, `get`, `save`, and `definitionOf` to turn a document back into a PUT body).

- **Fixed after D.3's dev check: the run in a conversation with no turns.** Two gaps on this start path.
  *The row.* The run's row (Bring back results, Stop run, Save as workflow) and its result card are drawn only
  in the thread, and the empty-conversation layout drew none. `ChatContainerComponent` now draws the thread
  when the conversation has a run to show (`showsInThread`: approved or stopped), messages or not; while the
  sheet is still open it keeps the empty state with the sheet in it. Such a run's `sourceMessageIndex` is
  `-1`, which no message matches, so the message list draws it above the first turn (`leadingRunRows`), and
  at the tail only while an older page hides that turn. *The first send.* It was refused as "Already
  responding" with nothing streaming. No turn lease held it: the composer's first keystroke prewarmed the
  conversation's runtime session, the Runtime began a snapshot restore (2,017 ms), and the send 270 ms later
  got a raw **409 from the Runtime data plane**, before the container. Any send in the seconds after a
  conversation's first keystroke could hit it, not only a workflow's. app-api's chat proxy now waits out a
  raw Runtime 409 and resends the turn (`agentcore-runtime-v2.md` §5a); a container's own 409 still arrives
  as a 424 and is never resent.

*Built (PR-D.3).* The SPA is `frontend/ai.client/src/app/workflows/`; the one backend change is in
`app_api/workflows/`. Where it differs from the plan, and what it settled:

- **A save can't clobber another tab.** `PUT /workflows/{skillId}/{name}` takes an optional `expectedVersion`.
  When the stored file has moved on it answers 409 `{detail, code: "version_conflict", document}` with the
  current document, or `{code: "workflow_deleted", document: null}` when the file is gone, and writes nothing.
  Without it a save behaves as before, and `?validate=true` never checks it. The designer always sends it and
  answers a 409 with "This workflow changed somewhere else" and **Reload the saved workflow**.
- **Nothing is stored until Save.** `/workflows/new` (blank, or `?template=proposal-review`) edits a draft only
  the page holds. The first Save is `POST /workflows {title}` then `PUT … expectedVersion: 1`, and the URL becomes
  the workflow's own with `Location.replaceState`, so the page and its selection stay put. Save is off while any
  step has no Agent, and says why. A step records its Agent's published version from the server's last
  description of it; an Agent picked since is described first (one `?validate=true`), so no step reads as
  changed the moment it is saved.
- **The canvas** is `WorkflowGraphComponent`: Starts with, one column per stage, Result. Stages come from
  `stagesOf` (a step's stage is one after the latest step it reads), so the file's reads already draw the right
  graph, though D.3 can only make stage 1. Each column is a labelled group with a list of buttons named "Fiscal
  reader, Agent step, stage 1, reads the request and the files". The connectors are an SVG measured from the
  cards and `aria-hidden`. Fit scales to the canvas, never below 60%; a drag on the empty grid pans; there is no
  wheel or pinch zoom. Below `md` the columns stack and no connector is drawn. The designer route sets
  `data: { chrome: 'minimal' }` rather than hiding the sidenav by hand.
- **The Step tab.** Starts with edits the files rule and the default request; the format has no "request
  required" flag, so neither does the tab. An Agent step picks from the author's own Agents and pins
  (`AgentMentionService`, the `@` menu's list), with an Agent already used by another step disabled. Its model
  is read-only. Move up and Move down keep focus on the button that moved (the other one at an end) and announce
  the new position politely; Remove moves focus to the neighbouring card. Reads of other steps show disabled
  with "needs stages", and the Result offers only the card.
- **The Preview tab** is the server's sheet from a debounced `PUT …?validate=true`, never a model call: the
  steps with their models, the files rule, the request, the blockers in the parser's words, and each step's
  input rate with the request-only floor of the sheet's own estimate (`estimateRunInputUsd`). It needs a saved
  workflow, because validate is a route on one. Start test run and Try in a chat both call D.2's
  `WorkflowStartService.start(ref)`, a new conversation with the sheet up; they are off while the draft has
  unsaved edits (a run reads the saved file) or the sheet has blockers, and say which. The run drawn on the
  canvas is D.4.
- **One client.** The designer, the list and Save as workflow use D.2's `WorkflowsApiService` and wire types
  (`src/app/services/workflows/`), which gained `listQuietly`, `create`, `read`, `validate`, `write` (the
  checked save, mapping the 409) and `remove`. Those send `SUPPRESS_ERROR_TOAST`, because each caller shows its
  own error; D.2's `list`, `get` and `save` are unchanged.
- **Save as workflow** is on a run's row once it is `stopped`, or `approved` and every step has settled. The
  workflow is named after the source conversation (`POST /workflows {title, fromRun}`), or "Untitled workflow"
  while that still has the placeholder title; a repeated title gets a suffixed skill id.
- **The Customize badge** is the row's existing chip ("workflow") on an active carrier, with **Open in designer**
  beside it while `features.workflows` is on. The workflow's name comes from the carrier's
  `workflow.<name>.md` filename, never its content.
- **No Share.** `resolve_accessible_skill_ids` never returns another user's skill, so a workflow can't be shared
  person to person today; "You can use" on the list is role grants only. Sharing is a later decision.
- **Measured before merge** (both themes, transitions off, against the canvas and against the strongest blob):
  card text 7.1–17.6:1, the warning line 4.73:1 at worst, the selected border 3.45:1 at worst. The app's focus
  ring, `primary-500` in both themes, is 1.67:1 on the dark canvas, so every workflows control adds
  `dark:focus-visible:outline-primary-accessible-dark` (4.53:1 on the canvas, 3.95:1 on a card). The app's other
  surfaces still use `primary-500` in dark.

*Built (PR-D.4).* SPA only, in `frontend/ai.client/src/app/workflows/`. Where it differs from the plan, and
what it settled:

- **Start test run stays on the designer.** `WorkflowStartService.start(ref, {navigate: false, titlePrefix:
  'Test: '})` makes the source the way D.2 does (`PUT /sessions/{id}/metadata`, then run create through the
  sheet) and does not navigate; it never claims the prewarmed id, which belongs to the new-conversation page.
  `start(ref)` and `start(ref, {sessionId})` are unchanged, and Try in a chat still calls `start(ref)`.
- **A third panel tab, Test run,** shown while the workflow has a test run. It hosts the starter's own
  `WorkflowRunSheetComponent`, unchanged: its only host assumption is `reviewFor(sessionId)`, which holds off
  the session page too. A note above it says the run spends quota and where everything it makes can be
  opened. After Start the tab shows W.4b's `WorkflowRunRowComponent` (relayed prompts answered under each
  step's line, each line a link to its conversation) and, once collected, `DeliveryCardsComponent` for the
  test conversation, with "Open the test conversation". The row gained one input, `offerSaveAsWorkflow`
  (default true), which the tab turns off: the run already is a saved workflow's.
- **Run mode is the tab, not the run.** The canvas draws the run while the Test run tab is open and the run has
  started; the Step tab gives the editable canvas back, so editing mid-run is still possible. In run mode a
  step card shows the run row's mark and phrase (`runProgress` / `stepState`, worded by `handoffStatusLine`),
  "· 1m 04s" once done, measured from the run's `approvedAt` to the step row's `updatedAt` (so a queued step's
  wait for a slot is in it); the cards are not buttons, Add step is hidden, the running pulse has
  `motion-reduce:animate-none`, and a connector is `primary-500` / dark `primary-300` at 2px once the node it
  leaves has finished (Starts with, once the run started). Draft steps match run steps by `stepName`. The
  columns still come from `stagesOf`, so nothing changes for D.5b's stages.
- **Loading and freshness.** `WorkflowTestRunService` (root) remembers each workflow's `{sessionId, runId}`,
  and keeps it in `sessionStorage` (this tab only, wrapped in try/catch). In the tab that started it, approve
  upserts the run into `WorkflowRunStateService` and the steps stream in this tab, so the row and the canvas
  read the live handoff rows exactly as the row in a thread does; nothing polls. A run the tab doesn't hold
  (after a reload) is read once with `GET /sessions/{id}/workflow-runs/{runId}`, which carries each waiting
  step's `relayedPrompts`; one that turns out never approved (the sheet was cancelled, or a reload came before
  Start) is forgotten and the tab goes back to Preview. After a reload no step streams here, so the row offers
  Start for the steps left, as in a thread. The read also seeds each step's handoff row into
  `HandoffStateService` (`seedRows`, which never replaces a row the tab holds), because the relay registers a
  waiting step's prompt from those rows. Dev validation found the approval card missing after a reload until
  this was added (#2237).
- **Leaving and coming back.** The queue, streams and run state are root, so leaving mid-run is fine, and
  opening the workflow's designer again selects its Test run tab. Close test run is offered once nothing is
  left running, and keeps the conversations.
- **Measured before merge** (the running SPA, both themes, against the composited card or panel): a step's
  phrase 7.49:1 light / 10.5:1 dark, the waiting phrase 4.99:1 / 10.69:1, the panel's note 10.3:1 / 9.96:1,
  its links 10.14:1 / 4.53:1. Connectors (non-text, 3:1): finished 10.14:1 / 3.98:1, neutral 4.63:1 / 6.82:1.
  Every new focus ring has its dark pair (`primary-accessible-dark`, 4.53:1).
- ⚠️ **Found, not fixed here:** `ng-icon` puts its colour in `@layer ng-icon`, which is declared after
  Tailwind's layers and so outranks `utilities`, and sets its width and height unlayered. So a `text-*` or
  `size-*` class on an `<ng-icon>` never applies anywhere in the SPA: the icon inherits its parent's colour
  and is `1em`. The run row's and the canvas's status marks render in the text colour, not green or amber.
  The state is still said in words, so nothing relies on the colour, but the marks don't look as designed.
  Filed as its own fix (#2232).

*Built (PR-D.5a).* Stages in the run engine. Where it differs from the plan, and what it settled:

- **Create.** The `reads_step` blocker is gone, from the sheet and so from run create. Each `WFRUN#` step keeps
  `reads`, the other steps it reads by `stepName` in step order (steps only; the request and the files still go
  to every step), and `workflow.resultReads` keeps the steps the Result takes (absent means all). Both are
  written only when set, so a run of mentions and every row before this read exactly as before, and the replay
  and `GET …/workflow-runs/{runId}` carry `reads` on each step.
- **Approve** refuses (422) a kept step that reads a removed one, naming both. The sheet never sends one.
- **Settled is done or stopped.** A dismissed, abandoned or gone row, and a never-started step of a stopped run,
  count as stopped. `needs_input` has not settled: the user's answer is still to come. A reader starts whatever
  its reads' outcome, and its first message names a stopped step with the run result's own status note.
- **`POST /sessions/{id}/workflow-runs/{runId}/steps/{stepId}/prepare`** returns `{stepId, message, readsBlock,
  parts: [{stepId, stepName, agentName, status}]}`. Each read step is `_part_for`'s part (its answer as it stood
  when it first finished, capped at `SEND_BACK_MAX_TOKENS`), checked by `prepare_delivery` against the source
  as a result's parts are, and wrapped by `render_delivery_parts`, so the blocks are byte-for-byte what a run
  result carries (a test pins it). `message` is `readsBlock`, a blank line, then the step's request. It is
  owner-scoped (`(session, user)` through the run row's key and its session), 409 while a read step has not
  settled ("Waiting for *X* to finish"), for a run never approved and for a removed step, and **writes
  nothing**, so it is idempotent by construction. A step that reads nothing prepares as its request.
- **Where the blocks go: the step's own text, not a stored row.** The SPA sends `message` as the user text of
  the step's first send, and the Runtime puts the run's card in front of it as for every step. So the
  Runtime, the turn path, `HandoffTurn` and the `HANDOFF#` row (which the target's first turn reads before
  its first token) are unchanged, and nothing larger is read there. The step's row keeps the plain request.
- **Text only.** The blocks carry no `<artifact>` or `<file>` lines. Those are references into the read step's
  conversation, and the reading step's conversation could not open them. Carrying a read step's artifacts and
  files across (reference rows written into the reader's conversation once it exists) is a follow-up.
- **The queue.** `RunStepLaunch.reads` names the read steps' handoff ids. A pending step starts once each has
  settled, in step order among the ready ones and still within `limits.concurrency`. Its slot is held while
  `…/prepare` runs, and the line reads "Queued" or "Waiting for *X*" then, never "Not started". The tab sees a
  step finish from its stream a moment before the Runtime writes its row, so a 409 is retried
  (`PREPARE_ATTEMPTS` 6, waits of 0.5 s, 1 s, … ); any other failure frees the slot and leaves the step "Not
  started". A pending step whose read will never settle in this tab (never started, not queued, not running)
  leaves the queue, with anything reading it, so the tab's unload guard never waits on it.
- **Reload.** `unstartedSteps` carries each step's reads, so Start after a reload queues the steps left and the
  reader waits on them as above. The run row's line for a step not started whose reads have not settled is
  "Waiting for *X*" (`waitingFor` on `RunStepLine`, `waitingLabel`, both in `workflow-run-progress.ts`); once
  they have, it is "Not started" with Start, as in step 1.
- **The sheet.** Remove on a saved workflow's step takes every step that reads it, transitively (`removalOf`),
  its accessible name says so ("Remove Generalist and the 2 steps that read it"), and a status line names them
  after. A step every other step reads cannot be removed, and the line says why. The component's inputs and
  outputs are unchanged.
- **Collect** takes the kept steps the Result reads, in step order, stopped ones named; if every step it names
  was removed at the sheet it takes every kept step rather than bring back nothing. A one-part result shows in
  the source as a single card, not "From *N* Agents".
- **The step's first bubble.** A handoff's first message that opens with delivery blocks (live, or from
  `displayText` after a reload) shows them as cards and the bubble shows only the request; a step sent with
  its card removed no longer reads its first result as "Context from".
- **Not in D.5a:** the designer and canvas, the automatic merge, and `WorkflowStartService`.
- **Found in dev validation and fixed after it (#2241).** After a reload mid-run the steps kept running on the
  Runtime and finished, but nothing re-read their rows, so a reader queued behind them waited until the next
  reload. `WorkflowRunRefreshService` now re-reads the run with one `GET …/workflow-runs/{runId}` a tick, while a
  run on screen or in this tab's queue has a step pending on the server and not streaming here: 5 s, doubling
  to 30 s while nothing moves, paused while the page is hidden, stopped once nothing is pending. The rows go
  through `HandoffStateService.advanceRows`, which only takes a copy written later and never one this tab
  streams. That also clears step 1's stale "Working on" line after a reload. Prepare measured 1.2–1.6 s on dev
  (it is not on a turn's path); it now skips the artifact and file lookups its text-only blocks throw away.

*Built (PR-D.5b-1).* Stages in the designer, SPA only, in `frontend/ai.client/src/app/workflows/`. D.5b was split:
this half is the designer, and D.5b-2 is the run side (View as graph, the small graph on the sheet, the merge that
starts by itself). Where it differs from the plan, and what it settled:

- **Reads.** The Step tab lists every other step's result with its stage. A step that already reads this one,
  directly or on the way (`readersOf`), is disabled and says why ("Reads this step, so this step can't read it",
  or "Reads *X*, which reads this step"), so the form can't make the cycle the format refuses. A step never lists
  itself. A line under the list keeps the rules in view: a step runs in the stage after the latest step it reads,
  it can't read itself or a step that reads it, and "*n* of 8 steps". A change that moves the step says so
  politely ("Colleague moved to stage 2"), and focus stays on the checkbox. The server's `PUT` still decides: a
  refusal shows its message in the header's alert, as any save error does.
- **Columns.** Still `stagesOf` over the draft, so nothing about the graph's inputs changed. Every stage ends
  with Add step (`add` emits the stage's 0-based index, as it always did for stage 1), and a last column, shown
  only in design mode once there is a step, starts the next stage. A step added to stage *N* reads the request,
  the files and every step of stage *N*−1, which is what puts it there (§4.1); the author unticks the rest.
  That column is one frosted box like a card, so a connector from an earlier stage to the Result passes behind
  it rather than through its text. Connectors follow reads in both modes, and a step connects to the Result only
  when the Result takes it.
- **The Result.** Card or merge (`result: merge`), the merge instruction (shown in merge mode, focused when
  merge is picked; a blank one is sent as none), and which steps' results it includes. All ticked is stored as
  no `Reads:` line, so steps added later are included too; the last one can't be unticked, because the format
  refuses a result that reads no step; and removing the only step the Result named gives it every step again for
  the same reason. The canvas card counts what the Result reads ("Starts by itself · 3 results", "Back to chat ·
  1 result"), not every step, and a merge is titled "A merge". Until D.5b-2 lifts the sheet's `merge_result`
  blocker, a saved merge workflow shows that blocker on the Preview tab.
- **`WorkflowGraphComponent` changed only additively,** so the run row and the review sheet can embed it with a
  run's `stages`, `status` and `finished`. The new controls render only in design mode. Its heading and
  description ids are unique per instance (a static counter, as the run sheet's are), so two graphs on one page
  never point `aria-labelledby` at each other's headings, and an optional `label` names its scroll region
  (default "Workflow canvas", or "Test run canvas" in run mode), for D.5b-2's "Graph of this run".
- **Measured before merge** on the running SPA, both themes, against the canvas or panel and the strongest blob:
  the Reads hints 7.23:1 light / 6.82:1 dark; the new-stage box's text 10.22:1 / 12.51:1 (5.68:1 / 9.18:1 for
  its hint over a blob); its dashed border 4.63:1 / 6.82:1; the Result card's count 7.49:1 / 10.52:1. Every new
  control's focus ring has the dark pair `primary-accessible-dark` (4.53:1 on the panel, 3.93:1 on a card), where
  the app's `primary-500` would be 1.67:1. At 375 px the columns stack, the new-stage box with them, and nothing
  scrolls sideways.

*Built (PR-D.5b-2).* The run-side half of D.5b: the run graphs and the merge that starts by itself. The designer
half (Reads, stage columns, the Result card) is D.5b-1. Where it differs from the plan, and what it settled:

- **`result: merge` starts.** The `merge_result` blocker is gone from the sheet, so from run create too. The run
  row's `workflow` gains `resultMode: "merge"` and `resultInstruction` beside `resultReads`, written only for a
  merged result, so a card run's row reads exactly as before. A merge with no instruction of its own keeps
  `DEFAULT_MERGE_INSTRUCTION` ("Combine these results into one answer."), resolved on the server once
  (`merge_instruction` in `apis/shared/workflows/sheet.py`). The document's `sheet` carries the same text as
  `mergeInstruction`, so the sheet and the row never word it differently.
- **One turn, through the existing paths.** The merge is an ordinary send in the source conversation: its text is
  the instruction, and the run's one result card rides it through `deliveries`, attached by default as any card is.
  The source conversation's model writes it (§17, question 6 stays open). No route, request field, SSE event,
  `toolConfig` or system-prompt change. Nothing new runs on the turn path: the checks below are synchronous reads
  in the tab before the send, and the turn appends one user message to the source's history.
- **Exactly once is the run's existing claim.** Only the collect that made the card (`created: true`, which is the
  server's `claim_result`) may merge, and only the watch's own collect in the tab that started or resumed the run
  asks. A second tab, a reload and the row's Bring back results get `created: false` and never merge. Nothing else
  records that a merge happened.
- **Checked at the moment of sending, not when the run settled** (`WorkflowRunMergeService.holdFor`): the source
  is the conversation on screen, it is not streaming, no run sheet is open on it, its composer is empty, and the
  run's card is on the composer and is the only card there. Otherwise the card waits, as in step 1, and the row
  offers **Merge now**, which keeps a typed draft where it is.
- **Why "on screen", which narrows §20.1.** A send is built from the page's selections, and the session page
  restores a conversation's last model when it is opened. A merge sent from another page would run on that
  page's model and binding; a model switch re-writes the source's whole cached prefix. So the page registers its
  send for its own conversation (`register`), and a merge only starts there. Away from the source, the results
  wait on its composer. The sheet says so before Start: "The merge starts by itself when every Agent finishes,
  if this conversation is open and nothing is typed in its composer. Otherwise the results wait on your
  composer." The designer's test run, whose conversation is never on screen, always waits.
- **The composer-empty signal** is `ComposerEmptyService` (`session/services/session/`): one boolean per draft
  key, written by the composer every time it mirrors its draft (`syncDraft`, the send's `persistDraftNow`, its
  destroy), and seeded on first read from the draft's `localStorage` row (`isEmptyDraft`), so a draft left before
  a reload counts.
- **Found in dev validation, fixed after it: a run's own file held the merge.** A saved workflow that takes a file
  is started with the file attached with + for the sheet, and the file stays on the composer after Start, so the
  composer never read as empty and the merge never started by itself in the main case (`files: one`).
  `ComposerEmptyService` now keeps which files are attached, and the merge ignores the run's own `fileIds`; typed
  text, a queued follow-up, a folder or any other file still holds it. The sheet also gained a fixed header and
  footer with its body scrolling between them, so Start stays on screen with five steps, the files and the merge
  notice. Dev validation of both runs (the merge by itself, the card waiting on a draft, Merge now, append-only
  `C#` rows, both graphs) is on #2251; the two runs cost $0.341.
- **Start takes the run's files off the composer** (the developer's call after #2256's dev check, #2257). Once
  the approve succeeds on a saved workflow's sheet, `ComposerDraftStorageService.releaseAttachments` drops the
  run's `fileIds` from the source's stored draft and announces it, the composer showing that draft takes those
  cards down, and `ComposerEmptyService` is told, so a reload doesn't bring them back. Typed text, a queued
  follow-up, folders, any other file and the result cards stay; Cancel and a refused Start clear nothing. The
  merge's rule that the run's own files are no draft stays, for drafts written before this. Every place Start is
  offered goes through `WorkflowRunService.start`. The designer's test run has nothing to release: its "Test:"
  conversation has no composer, so nothing can be attached there, and a `files: one` workflow's test run cannot
  start (its sheet asks for a file it has no way to take).
- **The sheet fits its column on small screens.** A fixed 14rem reserve clipped the sheet's title at 375 px under
  a composer with a file card. The chat page now passes `fitAbove`: the sheet measures everything else in its
  column (the composer, with its cards or a long draft) and caps itself to the room above it, below a 72 px top
  reserve, at most 42rem and at least 240 px. In a conversation with no turns, the empty layout puts its column at
  the bottom and hides the greeting while a sheet is open, as the thread's footer does, so the column's bottom
  edge is fixed and the cap never feeds back on the sheet's own height.
- **The run row** shows "Merge: *instruction*" under its heading. **View as graph** (a disclosure, closed by
  default, behind `features.workflows`) draws the run on `WorkflowGraphComponent` in run mode, as it is: stages
  from each step's `reads`, edges to the Result from `resultReads`, each card's phrase the row line's own, its
  elapsed time once done, finished connectors in the brand colour. It is not offered where the host turns Save as
  workflow off (the designer's test run, whose canvas already draws the run). The Result card reads "Merged in
  this conversation" or "One card". The builders are pure (`workflow-run-graph.ts`).
- **The sheet's small graph** is **Show as a graph**, a disclosure on a saved workflow's sheet: the kept steps
  (a removed step and its readers drop out), each saying "Starts first" or "After *A* and *B*" with its model.
- **Two graphs on one page** (two rows' View as graph, or a row and the sheet) rely on D.5b-1's per-instance
  ids. Each passes its own `label`: "Graph of this run" on the row, "Graph of the steps to start" on the sheet.
- **Measured** (the built stylesheet in the in-app browser, both themes, against the composited surface): the
  instruction 7.56:1 light / 6.82:1 dark, its label 10.3:1 / 12.05:1; View as graph 10.3:1 / 12.05:1; Merge now and
  Show as a graph 10.6:1 / 4.53:1; the sheet's notice 14.04:1 / 12.59:1 and its meta line 7.23:1 / 5.99:1. Every
  new focus ring has its dark pair: `primary-500` 10.6:1 on white, `primary-accessible-dark` 4.53:1 on
  `gray-900` (`primary-500` would be 1.67:1).

**Order.** D.1 first. Then D.2 and D.3 at once (D.2 touches the sheet, the composer and the run service; D.3 is
new pages and the graph, so their files do not overlap). D.4 after both. D.5a after D.2 and can run beside D.4.
D.5b last. At most three writing sessions at a time.

**Turn path.** Only D.5a comes near it, and it stays off: a later step's earlier results are written by app-api
before the SPA sends that step, so the step's turn reads a ready row, as every step does today. Its PR states the
step-start latency it adds in the tab, which is not TTFT.

**Dev validation** (each PR's own session, after merge): the worked example as a saved workflow. Four reviewers
in stage 1 and a merge, started from the Workflows page in a new conversation, with one Agent edited since it was
added, and one step removed on the sheet so its reader goes too. The source's `C#` rows show only appended
blocks, and the merge starts by itself when the composer is empty and waits when it is not.

### 20.4 Before deciding D.6 and D.7

*Done 2026-11-21: measured and decided in §21.*

Measure G3 (`multi-agent-patterns.md` §11.4) on dev: whether N prompt branches over one document read it from a
shared cached prefix when one call warms it first, per model. Then choose between a side call on a new Runtime
invocation (as the brief is routed) and a prompt step as a model-only conversation, which reuses everything step 1
built and costs a conversation per step.

### 20.5 Step 2 outcome (2026-11-21)

Every step-2 PR is merged and passed its dev check: D.1 (#2221), D.2 (#2223), D.3 (#2225), D.4 (#2233), D.5a
(#2236), D.5b-1 (#2249) and D.5b-2 (#2251). D.5b was split in two when it started, by files: the designer half
(Reads, stage columns, the Result card) and the run half (View as graph, the small graph on the sheet, the merge
that starts by itself), so the two could be built at once without touching the same files.

Seven fixes came out of the dev checks, each merged and checked on dev:

- #2234: a conversation a saved workflow started shows its run row, and its first send goes through.
- #2238: a waiting step's approval card comes back in the designer's test run after a reload.
- #2242: a run's steps stay current after a reload, so a step waiting on earlier results starts without a second
  reload. This also clears step 1's stale "Working on" line (§19.1 in `workflow-runs.md`).
- #2243: a send the Runtime refused because its session was still starting is resent every 0.5 s, not after a
  growing wait; and no "No messages yet" under a run's row.
- #2247: the sidebar no longer reads a finished run on every page load. A run's steps carry `runSteps` (how many
  the run kept, fixed at approve), so a run between stages is still read.
- #2256: the run's own file on the composer no longer holds the merge; the run sheet keeps Start on screen.
- #2260: Start takes the run's own files off the composer, so the next message doesn't send them to the source's
  model again; the sheet fits a phone screen.

**Measured on dev:**

- **The source's prompt cache.** A merge is one appended turn. In every merge run checked, the source's next call
  was a `hit` that read the whole cached prefix (64,686 and 68,104 tokens in two runs), with `toolConfigHash` and
  `systemPromptHash` unchanged and nothing wasted. The merge turn carried no document (`documentsAttached: 0`).
- **Prepare** (the earlier steps' results rendered into a later step's first message) took 1.0–1.6 s in the D.5a
  checks. It runs in the tab before the step's send, never before a turn's first token. Skipping the artifact and
  file lookups changed little: the time is in reading the earlier steps' messages and the checks on them.
- **The sidebar on a page load.** Before #2247, 5 run reads, all for finished runs. After: none for a finished
  run, one for a running run or a run between stages.
- **The first send in a conversation that is still starting.** One resend, 3.0 s from send to answer, of which
  1.3 s was the resend's own turn prelude. #2234's earlier "1.8 s after the session started" was measured the same
  way and so overstated the old poll's cost.
- **Cost of the checks.** The D.5 dev checks cost $0.75 across all their runs: $0.33 for D.5a's three runs, and
  $0.42 for D.5b's five runs.

**Open after step 2.** None blocks using it on dev.

- **A workflow that takes a file can't be test-run from the designer.** Its sheet asks for a file, and the test
  conversation has no composer to attach one.
- **Cancelling a test run leaves an empty "Test:" conversation** (§17 question 8 is where it would be settled).
- **Save as workflow on a run that a saved workflow started** makes a duplicate workflow.
- **"This Agent has changed"** compares published versions, so it couldn't be checked with the demo Agents,
  which were never published. A draft edit showed its marker in the review.
- **A run's sidebar line** refreshes only when the session list reloads, not in place.
- **Prepare carries text only:** an earlier step's artifacts and files don't reach the later step.
- **From step 1, still open:** one result per run, Stop run reaching only steps streaming in this tab, no bell
  entry for a finished run, and a resumed step without the mention's attachments (§19.1 in `workflow-runs.md`).
- **Focus rings** that predate this work still have no dark pair, and `<ng-icon>` ignores `text-*` and `size-*`
  app-wide (#2232).

**Still to decide:** §17 questions 5 (the flag once finished), 6 (who writes the merge), 7 (templates) and 8
(test conversations); D.6 and D.7 after §20.4's measurement; D.8 after question 2. The flags are on in
`development` only.

## 21. Delivery plan, step 3: prompt steps (D.6)

*Added 2026-11-21, after measuring G3 (§20.4) on dev and checking the side-call path and the run engine against
`develop` at `d1e5f51ee`.*

### 21.1 G3, measured

Direct Converse calls on dev (no platform path exists for a prompt step yet), with the `us.*` ids: five NSF
reviewer lenses over the four-page compact sample proposal. The system prompt and the PDF come to ~10,080 tokens
on both models. Two trials per model, each cell cold (a nonce at the head of the system prompt).

| Layout | Branches 2–5 that read the document | Input cost of 5 branches, Haiku 5.5 | Sonnet 5.5 | Wall time, Haiku / Sonnet |
|---|---|---|---|---|
| Document first, a cachePoint after it, one call to warm, then 4 at once | 16 of 16, all ~10,080 tokens | $0.0020 | $0.036 | 16.7 s / 24.5 s |
| Today's turn layout: the text first, then the document, one cachePoint at the end (`prompt_builder.py`) | 0 of 16 | $0.0072 | $0.144 | 16.8 s / 25.3 s |
| Document first, all 5 at once, no warm call | 0 of 20 | $0.0071 | $0.143 | 8.6 s / 13.2 s |

- **The saving is 3.6× on Haiku 5.5 and 3.9× on Sonnet 5.5**, on input. Output costs the same in every layout.
- **Today's layout costs more than no caching at all.** Every branch writes the whole prefix at 1.25× and nothing
  reads it: $0.144 against $0.114 uncached on Sonnet.
- **The warm call must finish first.** A request sent at the same time cannot read a write still in flight. The
  price is about one branch's latency.
- **Agent steps today** (the four PDF runs on dev): every step's first call was a `first_write` of its own ~8.5k
  prompt. Each Agent has its own system prompt, so nothing ahead of the document is shared.
- At the worked example's 19 branches on Sonnet 5.5, document input is ~$0.05 warmed and ~$0.53 in the turn
  layout. The measurement cost $1.20 ($0.06 Haiku, $1.14 Sonnet), almost all of it output.
- **Not measured, and it bears on D.7:** the prefix order is tools, then system, then messages. Checklist branches
  that each carry their own strict-tool schema, as `document_extract` does, cannot share the document.

### 21.2 Settled with the developer

- **A prompt step is a side call on a new Runtime invocation** (§20.4's first option), because the saving needs a
  request layout we choose. A step conversation on the turn path gets the second row of §21.1.
- **D.7 is decided after D.6 ships.** A checklist step can't reuse D.6's saving as `document_extract` is built
  (§21.1's last bullet), and G1 and G2 are open. D.7 starts with its own measurement: strict schema per branch
  against the field list in the instruction, on cost and on accuracy against the answer key.

### 21.3 The design

**The step.** A prompt step is a `HANDOFF#` row of a new kind, `prompt`. It has no Agent and no conversation.
Keeping the row means every reader that joins a step on `handoffId` keeps working: the run view, the replay, the
SPA's handoff state and the run refresh.

- **Fields:**
  - `modelId`
  - `request`: the instruction, as an Agent step's today
  - `outputText`, capped by `cap_delivery_text` at write
  - `outputHash`, `outputTokens`
  - `stoppedReason`
  - `updatedAt`, which collect uses as the finish time
- **Statuses:** `open → running → done | stopped`. A prompt step never asks the user anything, so it has no
  `needs_input`.
- **Stop reasons:** `user_stopped`, `too_large`, `model_refused`, `quota`, `timeout`, `error`.
- **Writes:** every move is one conditional update. `open → running` is the claim, so a step never runs twice
  (two tabs, a resend).

**The format.** `kind = prompt` and `worker = <model id>`. The `## Step: <name>` section is the instruction, and
reads work as they do for Agent steps. The envelope is unchanged: 8 steps.

- **The parser accepts `prompt` whatever the flag**, like every other parse check. Run create refuses a prompt
  step while the flag is off, and the sheet shows it as a blocker.
- **The model needs a catalog row** and passes `grants_model_access` as the starter. This is checked at create
  (a blocker on the sheet) and again on the Runtime before the call, with retirement resolved first. With a row,
  every call is priced, so `UnmeteredModelCall` has nothing to catch.

**The invocation.**
- **The request** is `InvocationRequest.workflow_prompt: {runId, stepIds}` (optional, additive, at most 8
  ids). The tab's run queue sends it through app-api's generic `/chat/stream`, on the **source** session id, so
  it lands on the source's Runtime session.
- **It takes no session lease.** The user can keep chatting in the source meanwhile, and a source send never
  preempts a step. Each step's claim is the guard instead.
- **It does not go through `_run_invocation`.** That means no agent build, no history, no tools, and none of the
  source's system prompt. The brief's path builds the whole source agent first, which a prompt step neither needs
  nor wants on its prefix.
- **It runs its own checks:** the flag, the run is the caller's and `approved`, each step is `open` with its
  reads settled, model access, and quota.

**The request layout** is §21.1's measured one:
- `system` is one constant prompt-step frame per deployment. It names the task and bounds the answer.
- `messages` is one user message: `[documents…, cachePoint, earlier results (if the step reads any),
  instruction]`.
- Documents go in the run's `fileIds` order under fixed names, so the prefix is byte-identical across the run's
  steps.
- The first claimed step is called alone to warm the prefix. The rest go through `run_bounded` with the run's
  `limits.concurrency`. A later stage's prompt steps read the same prefix while it is within its 5-minute TTL.
- **Prompt steps that are ready together go in one invocation**, which is what lets one call warm the rest.

**The documents.**
- **A PDF goes native**, the whole document, when it is within the model's page limit. A new read-through helper
  in `apis/shared/files/` does this: `readable_view` resolves references, then the S3 read.
- **Other kinds go as text**, rendered the way documents-as-text on Converse renders them.
- **A prepared large document goes as its text.**
- **Over the document budget the step stops with `too_large`.** The budget is 0.5 × the model's chat ceiling,
  which is 50K on Haiku 5.5 and keeps under its 100K 5× line. v1 does not truncate.

**The output.**
- `maxTokens` is 16,000, with the model's default effort. Reasoning counts against the limit on Haiku 5.5 and
  Sonnet 5.5, and at the first plan's 4,000, 3 of 10 steps were cut off mid-sentence on dev (2026-11-21).
  An answer that still reaches the limit is kept, ends with "[cut off at the output limit]", and its `C#` row
  carries `truncated: true`. The step timeout is 180 s: 16,000 tokens at Sonnet 5.5's slowest measured rate
  (116 tokens a second), with margin.
- The answer is stored capped at `SEND_BACK_MAX_TOKENS` (2,000), as a delivery part is.
- The model sees its own instruction, the documents and any earlier results it reads. It sees nothing else of the
  run.

**Metering.**
- One `C#` row per call, on the source session, with `callKind: "workflow_prompt"`, no `messageId`, and
  attributes `{runId, stepId}`.
- `ledger_rollups.CALL_KINDS` gains the kind, because an unlisted kind counts as `other`. The admin cost view
  maps it too.
- It is never a turn and never moves the context meter.

**The stream.** It exists on its own stream only, never on a turn's:
- `workflow_prompt_step {runId, stepId, status, stoppedReason?}` is sent as each step settles, then `done`.
- No text is streamed. The run row shows the step's status, and the answer is read from the step's row.
- It is added to the SSE table. The terminal client never sees it, since it never sends `workflow_prompt`.

**Stop.**
- The invocation re-reads the run row every 2 s. On `stopped` it cancels calls in flight and stops claimed steps
  with `user_stopped`.
- Each step has a 180 s timeout (see the output, above).
- A closed tab does not cancel. The calls finish and write their rows, and the run refresh picks them up.

**Results.**
- `_part_status`, `_part_for` and `_read_part` take a prompt row's `outputText`. A prompt step has no
  `targetSessionId`, no artifacts and no files.
- `_prepare_run_result` gains a text-only branch for a part with no sender conversation.
- `delivery_part_label` names the step and the model.
- The caps are unchanged, and so is the parity fixture's shape. It gains a case.

**The SPA.**
- **The queue's `start()` branches on kind**, sends ready prompt steps together, and settles each one from its
  frame.
- **Progress:** `stepState` reads a prompt row's own status.
- **The run row** shows a prompt step without a link, with its answer in a block that opens.
- **The sheet** labels it "Prompt · *model*" and estimates it at the catalog rate. The estimate stays an upper
  bound: it does not count the cache read.
- **The designer:** Add step offers Prompt. The Step tab gives a model picker (only models the user can use) and
  the instruction. The graph card names the model.

**The flag.**
- The backend flag is `PROMPT_STEPS_ENABLED` / `CDK_PROMPT_STEPS_ENABLED`, on app-api and on the Runtime. The
  Runtime copy is written only while on, so the default spends none of the V2 environment budget.
- The SPA flag is `features.promptSteps`.
- It is in development: only `"true"` enables, and only with `WORKFLOWS_ENABLED`.
- It is the first workflow flag on the Runtime.

**Cost and TTFT.** Nothing new runs on a turn's path. No turn's `toolConfig` or system prompt changes. The
source's prefix is never touched: a prompt step's answer reaches it as a delivery part, appended like any other.

### 21.4 The PRs

| PR | Side | Needs | What it ships |
|---|---|---|---|
| D.6a | Backend (shared, app-api), infra | — | The format's `prompt` kind. The `prompt` `HANDOFF#` kind and its fields in `models.py`, `runs.py` and the store's readers. Create, approve and the sheet: blockers, model and rate from the catalog. Collect and prepare with text-only parts, and the parity case. The flag: reader, CDK on both services, `platform.yml`, docs-site row |
| D.6b | Runtime | D.6a merged first | `workflow_prompt` on `InvocationRequest`, its dispatch in `routes.py`, a new `inference_api/chat/workflow_prompt.py`, the whole-document helper, the claim/finish/stop writes in a new `apis/shared/handoff/prompt_steps.py`, metering with `CALL_KINDS` and the admin cost mapping, the frames and the SSE table row |
| D.6c | SPA | D.6a, D.6b merged | `features.promptSteps`, the queue's prompt branch, progress, the run row, the sheet, and the designer's Add step, Step tab and card |

- **D.6a and D.6b run in parallel**, split by file ownership:
  - **D.6b owns** `prompt_steps.py`, the Runtime module, `routes.py`, `ledger_rollups.py` and the admin cost
    mapping. It writes the row's fields by the names in §21.3.
  - **D.6a owns** everything under `apis/shared/handoff/` except `prompt_steps.py`, plus `apis/shared/workflows/`,
    `app_api/`, `feature_flags.py` and infra.
  - **Merge order:** D.6b imports `prompt_steps_enabled()` from D.6a, so it merges second and rebases first.
- **Dev check:** each PR's own session validates it on dev after merge. D.6c's check is the end-to-end one: the
  NSF panel as five prompt steps over the sample proposal on Haiku 5.5 and Sonnet 5.5. It shows the warm read on
  the `C#` rows, a stop, a `too_large`, and a stage that reads a prompt step.
