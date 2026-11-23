# Agent plan tool (`update_plan`)

**Status:** Draft, not started. No code exists yet.
**Flag:** `PLAN_TOOL_ENABLED` / `CDK_PLAN_TOOL_ENABLED` (in development, off everywhere), plus the `update_plan` catalog row (`enabledByDefault: false`)
**Related:** `docs/specs/agent-state-feedback.md` (the live status line this sits beside), `docs/specs/ask-user-question.md` (the built-in tool this one is modelled on, and its trigger-rate findings), `docs/specs/eval-harness.md`, CLAUDE.md § SSE Event Types → `agent_status` / `tool_group_summary`

## 1. Problem

A long agentic turn shows no overall plan, before or during the work. An award intake, a Canvas grading pass, a `document_extract` batch over a dozen files, a `run_code` session, or a browser crawl can run for minutes and twenty tool calls. While it runs, the user sees three things:

* the loader's live step ("Using list_assignments", from `agent_status`);
* collapsed rails, each summarised after the fact (`tool_group_summary`);
* the answer at the end.

All three are about the current step or a finished one. Nothing answers *how far along is this, and what is it going to do next*, so a slow turn and a stuck turn look the same. A user who would have redirected at step 2 ("skip the budget section") finds out only at the end what the agent decided to do.

The model has the matching problem. On a twenty-call turn its first statement of intent is tens of thousands of tokens back, and long turns drift: a step gets dropped, or the last third forgets a constraint from the first. Coding agents solve both problems with one mechanism. The model writes its plan through a tool, ticks steps off through the same tool, and the client renders the list. Claude Code's `TodoWrite` and this desktop app's `update_plan` both work this way.

## 2. What this is not

* **Not the user's todo list.** `docs/specs/google-tasks-todo.md` federates the *user's* tasks to Google Tasks. This is the *agent's* working plan for one task. It is never written anywhere outside the conversation.
* **Not workflow runs.** `docs/specs/workflow-runs.md` hands steps to *other Agents* in other conversations. This is one agent narrating its own steps in one conversation. (§9 notes one place where they could meet later.)
* **Not a status event.** `agent_status` reports what the harness observes. The plan reports what the model *intends*, which only the model knows.

## 3. Shape

`update_plan` is a static built-in tool, registered like `ask_user_question` (`agents/main_agent/tools/tool_registry.py`), with two differences: it never pauses, and it is granted per role through its own catalog row.

| Property | Value | Why |
|---|---|---|
| Registration | static registry, behind `plan_tool_enabled()` | Unregistered while off, so the tool never reaches `toolConfig` |
| Access | `update_plan` catalog row, `enabledByDefault: false`; rolled out by binding it to long-running Agents (§5.2) | A product choice that costs prefix tokens, and the strongest guard against plans on simple tasks |
| Identity captured | none | Agent-cache eligible; no injected-tool bypass |
| Concurrency | in `PARALLEL_SAFE_TOOLS` | It reads history and writes nothing, so it can run beside the real work in the same batch |
| New SSE event | **none** | The SPA reads the `toolUse` input it already receives |
| New rows / tables | **none** | The plan is the `toolUse` inputs already stored on the message |
| `GET /messages` change | **none** | Reload rebuilds the plan from the stored message content |

The no-new-event, no-new-row design is the main idea of this spec. The plan is *already* persisted, byte for byte, because the model's tool calls are part of the conversation. Every other side channel we have (`TSUM#`, `SRCV#`, `EXTR#`) exists because its data must stay *out* of the content blocks. The plan is the reverse: the model wrote it into the content blocks, so it belongs there. Its display data costs nothing extra to persist.

## 4. Contract

### 4.1 Input

One call carries exactly one of these forms:

```jsonc
// Replace the whole plan (first call, or a re-plan)
{"items": [{"content": "Read the award notice", "status": "in_progress"},
           {"content": "Extract the budget table", "status": "pending"}]}

// Complete the in-progress step and start the next pending one
{"advance": true}

// Out of order: complete steps 2 and 3 (1-based), start step 5
{"done": [2, 3], "start": 5}
```

Limits:

* 3–12 items. The minimum is a restraint rule (§5.3).
* `content` at most 100 characters.
* `status` is one of `pending`, `in_progress`, `completed`.
* At most one item may be `in_progress` after the call is applied.

Both limits keep a plan short enough to read at a glance and keep the model's output small.

**Why deltas.** Every update is output tokens, which cost 5× input, and the input then lands in history for the rest of the session. A seven-step plan updated six times costs roughly 900 output tokens when the full list is resent each time, against about 60 for `advance` calls. This desktop app's own plan tool uses the same split: full list once, small updates after. Whether the platform's models handle deltas reliably is an open question for the eval in §7. If they do not, the fallback is full-list-only, which changes only the schema.

### 4.2 Result

The model receives one bounded line:

```
Plan updated: 2 of 5 done. Now: Extract the budget table.
```

That is about 15 tokens. It repeats the current step back to the model at the point in history where the model will read it next, which is the part of the drift fix that a write-only tool would miss. Echoing the *whole* plan would repeat that effect more strongly, but it costs about 100 tokens a call for the rest of the session. Measure before choosing it (§7, open question 2).

### 4.3 Errors

An error result says exactly what to send instead. Each one is a wasted model round trip, so the messages must be fixable in one try:

| Case | Result text |
|---|---|
| `items` with fewer than 3 steps (§5.3) | `A plan needs at least 3 steps. For shorter work, skip the plan and do the work.` |
| New plan with no real tool call in the same batch (§5.3) | `Start a plan only alongside the first tool call that does the work. If this task needs no tools, skip the plan.` |
| Delta with no plan in view (e.g. compaction summarised it away) | `No plan to update. Send the full list with "items".` |
| Index out of range | `Step 7 does not exist; the plan has 5 steps.` |
| Two `in_progress` after applying | `Only one step can be in progress.` |
| A second `update_plan` call earlier in the same assistant message | `One plan update per batch; this one was ignored.` |

### 4.4 The fold (one function, two twins)

The current plan is a pure function of the conversation: **fold every `update_plan` call whose result is `success`, in message order**. An `items` call resets the plan. A delta call applies to the plan before it.

* **Runtime twin**, `agents/builtin_tools/plan/fold.py`. The tool reads `tool_context.agent.messages` at call time, folds every earlier call, then validates and applies its own. Nothing is held on the agent instance, which matters because one session can be served by more than one agent (CLAUDE.md). History is the only state. The fold:
  * pairs each `toolUse` with its `toolResult` by `toolUseId`;
  * skips calls whose result is an error;
  * treats an earlier call in the *same* assistant message as a conflict (§4.3's last row), so two plan calls run concurrently in one batch resolve the same way however they interleave.

  This depends on two Strands facts, checked against the installed wheel. `ToolContext.agent` exposes the running agent. The event loop appends the assistant message holding the `toolUse` blocks (`_handle_model_execution` → `agent._append_messages`) *before* `_handle_tool_execution` runs them, so a call can see its own message and any sibling calls. PR-1 pins both with a test, so a Strands bump that reorders them fails loudly.
* **SPA twin**, `session/services/plan/plan-fold.ts`. It folds the same calls from the message list, applying a call only once its success result has arrived. The tool does no I/O, so its result lands within milliseconds of its input, and the card never shows a delta that then turns out to be an error.
* **Parity is pinned** by `plan-fold.fixture.json` beside the SPA twin. `backend/tests/.../test_plan_fold_fixture.py` reads it, the same pattern as `handoff-prompt.fixture.json`. A case must cover each of:
  * reset;
  * each delta form;
  * each error, including both restraint refusals;
  * a new plan vs. a re-plan vs. a delta on an earlier turn's plan, with the turn boundary placed after a steer;
  * same-batch conflict;
  * a delta whose base was compacted away.

Rewind and compaction need no special handling. Rewind cuts the Runtime's live list and hides the same messages in the SPA, so both twins fold the same calls. Compaction can drop the base from the Runtime's view while the SPA still shows it. The delta then errors, the model resends `items`, and because both twins skip error results, they agree again.

## 5. Restraint: only long tool work gets a plan

This tool's main risk is over-use, not under-use. Models like checklists. A three-item plan for "what's 2+2" is noise for the user. A plan call alone in a batch costs a whole extra model call (§6). The model's judgement is one layer of defence, and the weakest one: `ask-user-question.md` § Trigger rate showed a system-prompt sentence can make a model call a tool *more*, and nothing yet shows the same lever makes it call one *less*. So there are four layers, strongest first. Only the third depends on the model.

### 5.1 The obvious control is not available

We can't offer the tool only on turns that look long. The tool spec sits in `toolConfig`, which comes before the system prompt and the whole restored history in the cached prefix. A per-turn decision would rewrite that prefix every time it flipped, which the prompt-cache contract rules out. For a given configuration (Agent, role grants, model) the tool is either always present or never present. Every control below works within that rule.

### 5.2 Layer 1: grant it to long-running Agents, not to general chat

Most simple questions land in general chat, so the biggest lever is to keep the tool out of it. The rollout binds `update_plan` to the Agents whose work is long and tool-heavy, such as award intake, grading passes, `document_extract` batches and `run_code` sessions, through the Agent's tool binding. On dev, it is also granted to the roles that test them. General chat does not get it until production data (§5.5) shows the over-use rate is low enough on Agent turns to risk it on open-ended ones.

This costs nothing extra: an Agent's tool list is already part of its configuration, so binding the tool rewrites that Agent's prefix once, and no other conversation's.

### 5.3 Layer 2: the tool refuses a plan that is not next to real work

Two deterministic checks at call time. Both are cheap because the tool already reads its own assistant message for the fold (§4.4).

1. **Every `items` call has 3–12 steps.** This is the schema's `minItems`, and the tool also checks it. Two-step work does not need a checklist.
2. **A new plan must share its batch with a real tool call.** "Real" means a call to any tool other than `update_plan`. "New" means either no plan is in view, or the plan in view was last changed before this turn began. A turn begins at the last user message that carries no `toolResult` block; a mid-turn steer rides a tool-result message, so it does not start one.
   * Deltas are exempt. So is re-planning an active plan from this same turn: refusing a mid-task correction would cost more than it saves.
   * A "continue" turn that advances an earlier turn's plan uses a delta, so it passes.

The refusal is an ordinary error result (§4.3), so both fold twins skip it and **a refused plan never renders**. A wrong call costs one model round trip with an error the model can act on. It never puts a checklist in front of the user.

Rule 2 has three useful effects:

* A question with no tool work cannot produce a plan at all.
* A pure writing task (a five-section report with no tools) never gets one. That is right: the streaming text already shows progress there, and the plan exists to show progress across tool calls.
* It turns §6's "never call it alone" from a request into a rule for the opening call, which is the one most likely to go out alone.

**Risk to watch:** the 3-step minimum may tempt a model to pad a two-step task with a filler third step. The eval reads plans for steps that match no tool work (§7).

### 5.4 Layer 3: guidance

The first candidate text, appended through `_system_prompt_for` exactly as `SYSTEM_PROMPT_GUIDANCE` is, and only for turns whose post-filter tool list contains the tool:

> For a task that will take three or more distinct steps using tools, call update_plan once at the start, in the same batch as the first tool call, then advance it in the same batch as each tool call that starts the next step. Never call it alone. Do not use it for questions, writing without tools, single-step tasks, or follow-ups to a finished task.

The description carries the schema rules. The prompt carries the when-and-how. The eval decides the final wording and where it goes, measuring both placements, because this tool's aim is restraint, not reach. The chosen text gets the same do-not-reword warning and byte-equality test as `SYSTEM_PROMPT_GUIDANCE`.

### 5.5 Layer 4: watch it in production

At the end of each turn that called `update_plan`, `AgentStatusHook` emits two EMF metrics:

* `PlanTurn` for every such turn;
* `TrivialPlanTurn` when the turn made fewer than three real tool calls.

The hook already sees every call, and emitting runs after the stream, so nothing is added before the first token. The metrics carry ids and counts only. The **trivial-plan rate** is `TrivialPlanTurn / PlanTurn`, broken down by Agent and model. The bar is under 10%. Above it, the guidance is failing on that Agent or model, and the tool comes off that binding until it is fixed.

PR-4 adds the rate to the dev readout. Whether it also becomes a release-ledger counter is decided there, after seeing whether the EMF series is enough.

## 6. Cost and latency

* **Tool spec.** Constant in `toolConfig` for granted users, estimated at 300–400 tokens and to be measured with `scripts/measure_tool_spec_tokens.py` on dev. It is digest-pinned. Granting the tool to a role rewrites that role's users' prefix once. Nothing changes per turn.
* **Guidance.** About 60 tokens, constant per configuration, inside the system segment, and only for turns that have the tool.
* **History.** Each call adds its input and the one-line result, both appended at the end of history. History stays append-only, so the cached prefix never moves.
* **Time to first token: zero.** No `Before*` hook is added and nothing runs before the first token. The tool runs only when the model calls it.
* **The real cost is a solo plan call.** When a batch contains *only* `update_plan`, the event loop pays a full extra model call to continue. That call reads the whole cached prefix (0.1× input) and adds its own time to first token. This is the number the eval must hold down (§7). For the opening call, §5.3's rule 2 refuses it. For later updates, it is guidance only, because refusing a mid-task update would cost the same extra call it is meant to prevent.
* **Side channels it must not trigger.** `AgentStatusHook` skips the plan tool by name (a `NON_WORK_TOOLS` constant). Without the skip:
  * a plan call would flash "Using update_plan" in the loader;
  * it would add a line to the batch the Nova Micro summariser sees;
  * a batch holding only a plan call would cost a whole summariser call to say "Updated the plan".

  With the skip, such a batch is never queued.

## 7. Evaluation: what must be true before it goes on in dev

Run the cases with `backend/scripts/eval_harness.py` on a production-shaped tool set, at n ≥ 16 per cell. `ask-user-question.md` found baseline noise of 17–44%, so anything closer than about 25 points is not a result.

The easy ends are not where over-use shows up. The middle rows are the ones that decide whether §5's layers work.

"Plan attempted" counts every `items` call, refused or not. A refusal costs a wasted round trip even though nothing renders.

| Case class | Example | Pass bar |
|---|---|---|
| Question, no tools | "What does F&A stand for?" | plan attempted in ≤ 1/16 |
| Single-tool task | "Find the syllabus assignment in BIO 101" | plan attempted in ≤ 1/16 |
| Two-step task | look one thing up, then write one row | plan attempted in ≤ 2/16; no padded plans |
| Sounds big, is small | "Tell me everything about this award" when one document read answers it | plan attempted in ≤ 2/16 |
| Long writing, no tools | "Draft a five-section report from what I pasted" | 0 rendered by construction (§5.3); plan attempted in ≤ 2/16 |
| Follow-up to a finished plan turn | "Thanks!" or "Can you shorten the email?" | no new plan and no update in ≥ 15/16 |
| Multi-step task | intake an award PDF into the tracker, grade five submissions | plan created in ≥ 14/16 |
| Same, plan quality | — | every step completed or explicitly dropped by the end; no step that matches no tool work (padding) |

Measured per model: Haiku 4.5, Haiku 5.5, Sonnet 5.5, Opus 5.5.

| Metric | Bar |
|---|---|
| Solo plan-call cycles, excluding the opening call | ≤ 10% of plan calls |
| Plan-call error rate on multi-step tasks | ≤ 5%; every error fixed on the next call |
| Trivial-plan rate across all classes (§5.5's definition) | ≤ 10% |
| Output tokens per multi-step turn vs. tool absent | stated in the PR, not barred |
| Task completion vs. tool absent (LLM judge) | no regression; the hoped-for gain is fewer dropped steps on 15+ call turns |

Run each class twice: with the tool bound as an Agent's tool (§5.2), and with it in a general-chat tool set. The gap between the two is the evidence for whether general chat ever gets it.

Two of these settle open questions: delta vs. full list (§4.1) and result echo (§4.2). Report the eval's own cost per model and in total, estimate vs. actual.

## 8. SPA

No SPA flag. The renderer keys on the tool name, which appears only when the backend put it there. A conversation recorded while the flag was on must still render correctly after a deployment turns it off, and a frontend flag would break that.

* **The rail ignores plan calls.** They are removed before rail grouping (`assistant-message.component.ts`, where `tool_use` blocks become `ToolCallDisplay`), so a plan call between two real calls does not split the rail.
* **Plan card.** One per assistant run, placed at the run's first plan call, updated in place from the fold:

  ```
  ┌─────────────────────────────────────────────┐
  │ ✓ Read the award notice                     │
  │ ◐ Extract the budget table                  │
  │ ○ Fill the tracker row                      │
  │ ○ Draft the summary email                   │
  └─────────────────────────────────────────────┘
  ```

  When the turn ends the card collapses to one line ("Completed 4 of 4 steps", or "Stopped at step 2 of 4" after a Stop) and expands on click. Steps are text only, with no links or actions.
* **Loader line.** While the turn streams, the loader shows the `in_progress` step ("Step 2 of 4 · Extract the budget table") in place of the `agent_status` tool line. The tool line returns when no plan is active.
* **Shared view and share export** use the same renderer, so a shared conversation shows the card read-only.
* **The terminal client** sees `update_plan` as an ordinary tool call, which is harmless. Rendering a checklist there is a parity follow-up (`TUI_WEB_PARITY_SPEC.md`), not part of this work.

## 9. Interactions

* **Mid-turn steering.** A steer lands at a tool boundary. The model re-plans with `items` if the steer changes the work. Nothing special is needed.
* **Interrupts / resume.** Plan calls before a pause are in the stored history. The resumed turn folds them like any others.
* **Fork / handoff brief.** Both copy history, so plan calls come along and fold the same way.
* **Release ledger.** `#TOOL#update_plan` rows count plan calls, and plan errors count toward the tool-error KPI. A readout for a release that turns this on should name that mix shift rather than read it as a regression.
* **Workflow runs (later).** A step row could show its target conversation's progress ("3 of 5") by folding that conversation's plan calls. That needs a read of the target's messages on `GET /messages`, so it is a separate decision.

## 10. Flag

`PLAN_TOOL_ENABLED` / `CDK_PLAN_TOOL_ENABLED`. It is in development, so only `"true"` enables it.

* It applies to the AgentCore Runtime only and is written to the Runtime only while on, so the default spends none of the V2 environment budget.
* It is wired through `feature_flags.py`, `infrastructure/lib/config.ts` and its test, `platform.yml`, the docs-site feature-flags row, and the CLAUDE.md in-development list.
* While off, the tool is never registered and the catalog row grants nothing.
* Once finished it is a **feature switch**, not a rollout switch. It adds a tool to `toolConfig` and changes what a turn looks like, which is a choice a deployment may reasonably not want.

## 11. PR plan

| PR | Contents | Done when |
|---|---|---|
| **PR-1** backend | flag + CDK + workflow; `fold.py` and the tool, with §5.3's two refusals; registry and catalog row; `PARALLEL_SAFE_TOOLS`; `AgentStatusHook` skip plus the `PlanTurn` / `TrivialPlanTurn` metrics; guidance constant behind `_system_prompt_for`; spec digest test; fold fixture (Python side); measured spec tokens on dev | hermetic suites green; tool callable on a local stack |
| **PR-2** eval | the §7 cases, middle rows included, in both the Agent-bound and general-chat tool sets; run per model; guidance wording, placement and delta/echo decisions recorded here | every §7 bar met or the gap stated and accepted |
| **PR-3** SPA | `plan-fold.ts` against the shared fixture; rail exclusion; plan card; loader line; shared view | checked in the in-app browser on dev: live, reload, Stop, rewind, shared view |
| **PR-4** dev | flag on in the `development` environment; tool bound to the long-running dev Agents only; smoke turn-matrix row for a plan turn; readout from the release ledger plus the trivial-plan rate | one week of dev use with no unexplained cache misses on plan turns and a trivial-plan rate under 10% |

PR-2 comes before the SPA on purpose. Its result can change the contract (full list instead of deltas), and the SPA's fold should be written once against the final one.

## 12. Open questions

1. **Deltas or full list?** Recommend deltas with full-list reset (§4.1). §7 decides.
2. **Echo the whole plan in the result?** Recommend the one-line echo; measure the full echo against it on 15+ call turns, where drift shows.
3. **Guidance in the prompt or the description?** Recommend the prompt, given `ask_user_question`'s finding. Measure both (§5.4), since this tool's goal is restraint, not reach.
4. **Does a plan stay visible across turns?** Recommend no sticky element: each turn's card shows the plan as that turn left it, and the next turn's card picks up from there. Revisit if users "continue" long tasks across turns often.
5. **Does general chat ever get it?** Settled for now by §5.2: long-running Agents only. Reopen once production has a trivial-plan rate on Agent turns, and the §7 general-chat runs say whether open-ended conversations can hold the same bar.
6. **Is three steps the right minimum?** Three matches common practice in coding agents. If the eval finds padded plans (§5.3's risk), the fix is guidance first; lowering the minimum to two would weaken the refusal that keeps two-step work plan-free.
