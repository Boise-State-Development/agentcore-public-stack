# Decisions: one closed-question primitive, many surfaces

**Status:** Draft. Nothing here is implemented. Written before the code, so §16 of
`structured-extraction.md` is the precedent for what happens next: the decisions below
are hypotheses until a PR meets them, and a section recording where this spec was
wrong is expected, not a failure.
**Author:** Phil Merrell (with Claude Code)
**Date:** 2026-11-14
**Targets:** `develop`
**Motivating change:** `strands-agents==1.65.0` ships a decision-model primitive
(`strands.decisions`) and a model router that consumes it (`strands.models.routing`).
This spec decides how the platform uses the first, and why it mostly does not use the
second.
**Related:** `docs/specs/structured-extraction.md` (the constrained side call this
generalizes, and the measurements it already paid for), `docs/specs/field-set-authoring.md`
(the authoring surface question sets reuse), `docs/specs/scheduled-agent-runs.md` (the
first background consumer), `docs/specs/source-provenance-verification.md` (why a
verdict needs evidence), `docs/specs/turn-path-ttft.md` (why none of this runs before the
first token), `docs/specs/release-ledger.md` (`callKind` accounting),
`docs/specs/model-fallback-profiles.md` (where router-based failover belongs instead).

---

## 0. Summary

A **decision** is a closed question about one item, answered with one of its allowed
choices or with **`Uncertain`**. Many questions over many items is the common case:
"for each of these 140 award notices, is there a cost-share requirement, and which
sponsor type is it?"

The platform already makes this kind of call. `document_extract` runs a typed field set
over N documents on a dedicated constrained request, with bounded concurrency, metering,
a reload store and a backend-written table. Its `boolean` and `enum` fields are
decisions in all but name. What it cannot do is take an item that is not a document,
run outside a turn, say "I won't commit", or leave a result that outlives the
conversation.

This spec lifts the general parts into `apis/shared/decisions/` and adds two consumers
chosen to be unlike each other:

1. **`classify_items`**, an agent tool: a question set (authored or ad hoc) over
   documents or spreadsheet rows, interactive, many items.
2. **Notify-if on scheduled runs**: one boolean question over a finished run's answer,
   in a Lambda, with no turn at all.

Approvals, project file labels, mail triage and a release-ledger quality signal follow
as adapters plus a surface each (§10.3). `document_extract` keeps working as it is and
moves onto the shared engine later, on its own schedule.

---

## 1. Problem

Five ideas from the 2026-11-14 brainstorm need the same capability:

| Idea | Items | Where it runs | What `Uncertain` should do |
|---|---|---|---|
| Triage a set of documents or rows | many | a turn's tool call | go to the user's review list |
| Notify me only if a run found X | one | the scheduled-runs worker Lambda | follow the user's preference |
| Ask before risky tool calls only | one per call | a `BeforeToolCall` hook | ask the user |
| Label project files as they arrive | one per file | folder sync, off the turn path | go to the project's review filter |
| Triage my mail | many | a turn or a schedule | go to the review list |

Built one at a time, each would carry its own copy of: a model id and its catalog row,
a constrained request with its strict-mode fallback, a concurrency limit, input
bounding, metering, a results shape, and a rule for what an unclear answer means. Five
copies of the metering alone is five chances to ship an unmetered call. Three of the five
also need the same authoring surface, because the questions are the part a department
tunes.

So the deliverable is the primitive and its first two consumers, not five features.

---

## 2. What already exists (verified in code, 2026-11-14)

### 2.1 Strands 1.65.0

Read from the installed wheel; the SDK cites an internal design note
(`team/designs/0020-decision-models.md`) that is not published with it.

* **`DecisionModel`** (`strands/decisions/decision_model.py`) is an ABC. The public
  `ask(state, questions, *, invocation_context=None)` validates the question map, opens
  a `decision` OpenTelemetry span with the question ids and serialized answers, calls the
  subclass's `_ask`, and records usage. Given an `invocation_context`, it **adds the
  decision's usage to that request** (see D7).
* **`Question`** is `{question: str, choices: "boolean" | list[str], uncertainty?: bool |
  {min_confidence}}`. `"uncertain"` is reserved and may not be a choice. Uncertainty is
  permitted unless explicitly `False`.
* **`DecisionResult.answers`** maps each question id to `bool`, one of the choices, or
  `Uncertain(reason)`. `Uncertain` is truthy, so callers must narrow with `isinstance`.
* **`LLMDecisionModel(model)`** makes one forced structured-output call through
  `ModelProxy(model).structured_output`. Its answer schema is one object per question,
  `{answer: Literal[choices + "uncertain"], reason: Optional[str]}`. Its system prompt
  declares the content untrusted and forbids it from changing the options. It reports no
  confidence.
* **`TypeSafeDecisionModel` / `DecisionsAPIDecisionModel`** call hosted third-party APIs
  (`api.typesafe.ai`, `decisionapi.net`). They are the only implementations that return
  probabilities and per-question confidence.
* **`ModelRouter` + `ClassifierStrategy`** (`strands/models/routing/`) ask a decision
  model which candidate model serves an invocation, once, before its first model call.
  **`FallbackStrategy`** does ordered failover with no classifier. The router documents
  its own limitations: `agent.model` stays the first candidate (so compression sizes
  against the wrong window), `Agent.structured_output()` bypasses routing, and a failover
  after partial streaming emits the partial output and then the replacement's.
* The vended **human-in-the-loop** handler (`vended_interventions/hitl/`) accepts a
  `DecisionModel` as a risk classifier at `BeforeToolCallEvent`.

### 2.2 This repository

* **`apis/shared/extraction/`** (3,198 lines):
  * `field_sets.py` parses a field set from a skill reference file
    (`references/field-set.<name>.md`): frontmatter plus one markdown table, fields in
    authored order. Types include `boolean` and `enum` (`JUDGEMENT_TYPES`). The widest
    set Bedrock will compile in this schema shape is **14 fields**, measured.
  * `extractor.py` is the constrained call: a dedicated `BedrockModel(strict_tools=True)`
    carrying the document and the schema and nothing else. It retries unenforced on a
    narrow set of causes (`_should_fall_back`), marks the run `schemaEnforced: false`
    when it does, and refuses to fall back on a **capacity** failure (`_CAPACITY_MARKERS`:
    more than 16 union-typed parameters, complexity pre-checks, the grammar size
    ceiling), because that is an authoring error that would otherwise downgrade every run
    forever.
  * `batch.py` runs N items under a semaphore (`DOCUMENT_EXTRACT_CONCURRENCY`, default 4,
    clamped 1..16) and runs the first item alone while the grammar is cold. Measured: a
    cold schema cost 15.3–16.0 s per call against a 2.95 s warm median.
  * `extraction_broker.py`, `extraction_store.py` and `render.py` carry the rows to the
    SSE stream, persist them off the message (`EXTR#` rows on the existing
    `SessionLookupIndex`), and write the reviewable table and `.xlsx` in the backend.
* **Metering:** `document_extract_tool._meter` writes a `C#` row per extraction call
  against the session, priced from the catalog row of `DOCUMENT_EXTRACT_MODEL_ID`, and
  emits `UnmeteredModelCall` when a call reports no usage. Project-scoped background work
  meters differently: `MemoryMaintenanceRunner._meter` adds cost to the project's month
  through `projects.add_call_cost`. The release ledger buckets rows by `callKind`
  (`CALL_KINDS` in `ledger_rollups.py`, which already lists `document_extract`).
* **Scheduled runs:** `lambdas/scheduled_runs_worker/worker.py` calls
  `run_agent_headless` and gets a `RunResult` with `final_message`, `session_id` and
  `status`. **A completed run notifies nobody today**: it lands in the user's session
  list (or is shared to the project), and only pauses send a notification
  (`project_schedule_paused`). So notify-if adds a notification; it does not filter an
  existing one.
* **Notifications:** `apis/shared/notifications/`, in-app only, with a closed
  `NotificationKind` literal and 90-day retention.
* **Approvals:** `ToolApprovalHook` pauses MCP tools whose catalog entry sets
  `needs_approval`, plus the skill-authoring writes.

---

## 3. The model of a decision

### 3.1 Three outcomes, kept apart

Every item-question pair ends in exactly one of:

| Outcome | Meaning | Who produced it |
|---|---|---|
| **Answer** | `true`/`false`, or one of the allowed choices | the model, committing |
| **Uncertain** | the item says something relevant, and the model will not commit; carries a bounded reason | the model, declining |
| **Failed** | no answer exists: timeout, throttle, refused input, invalid output after retry | the engine |

`Uncertain` and `Failed` must never be merged. A batch where 9 of 140 are `Uncertain` is
a review list. A batch where 9 failed is an incomplete batch, and a judgement across the
set must say so (the rule `extraction/batch.py` already applies to failed documents).

### 3.2 Decisions have no `null`

Extraction uses `null` for "the document does not state this value", which is a fact
about the document. A decision does not need it: "does this mention cost share?" over a
document that never mentions it is answered `false`. Where an author wants "not stated"
as an answer, it is a choice they write (`[required, not required, not stated]`), so it
appears in the schema, the table and the counts like any other choice.

### 3.3 Confidence is not a field

The only implementations that report confidence send content to third parties (D12).
Ours reports none, so no surface shows a percentage. "Uncertain" is the whole signal.

---

## 4. Question sets

### 4.1 One grammar, a distinct file

A question set is a field-set file restricted to judgement types, with one added column:

```markdown
---
name: award-notice-triage
title: Award notice triage
version: 3
kind: questions
---

| name | title | type | find | values | uncertain |
|---|---|---|---|---|---|
| has_cost_share | Cost share | boolean | whether the sponsor requires institutional cost sharing of any amount | | yes |
| sponsor_type | Sponsor type | enum | who is funding the award | federal, state, foundation, industry | yes |
| needs_pi_signature | PI signature | boolean | whether the notice asks the PI to sign or accept terms | | no |
```

* It lives at `references/question-set.<name>.md` in a skill's reference bundle, so it
  inherits the skill's visibility, RBAC grant, Agent binding and project-harness binding,
  exactly as field sets do (`structured-extraction.md` D2). The flat
  `question-set.<name>.md` form follows §16.1 there: bundle filenames cannot contain a
  path separator.
* The parser, serializer, `FieldSetLayout` round-trip and authoring page are the
  field-set ones. A `kind: questions` file rejects any type outside `boolean` and `enum`
  by name.
* `uncertain` defaults to `yes`. `no` means the question must commit, as Strands'
  `uncertainty: False` does.
* `find` becomes the question text the model sees. It is the field an office tunes, and
  the reason a question set beats a bare `question` string.
* Order is authored, never derived, for the reason extraction gives: the schema's
  property order follows the list, and a reordered list is a different grammar.
* `version` is part of the cache key and of every stored record (§7), so a changed set
  re-labels on purpose and old labels stay attributable to the set that produced them.

### 4.2 Ad-hoc questions

A user who has authored nothing can still say "classify these 300 comments by theme".
The tool accepts up to **8** questions in its arguments (`{name, question, choices}`),
the same bound Strands' `DecisionsAPIDecisionModel` enforces and comfortably inside the
measured union ceiling (§5.3). An ad-hoc set runs **non-strict** (D4), is never stored as
a skill, and the surface offers "Save as a question set" afterwards, which writes the file
through the existing `my_skill_*` path under that path's own approval card.

---

## 5. The engine

### 5.1 `PlatformDecisionModel(DecisionModel)`

`apis/shared/decisions/engine.py`. Subclasses Strands' ABC, so it gets the `decision`
span for free and can be handed to anything in Strands that takes a `DecisionModel`
(the router's `ClassifierStrategy`, the HITL classifier) without an adapter.

```python
engine = PlatformDecisionModel(model_id=decision_model_id(), strict=True)
result = await engine.ask(state, questions, purpose="classify_items")
```

It owns:

* **The model.** `DECISION_MODEL_ID`, which must have a catalog row (D5). A missing id
  or row raises before any call, naming the variable.
* **The call.** The extractor's constrained-call machinery, generalized: a dedicated
  `BedrockModel(strict_tools=...)` instance, the timeout, the narrow fallback
  classification, and the refusal to fall back on a capacity failure. Shared code, not a
  copy: `extractor._run_once`, `_should_fall_back` and `_capacity_failure` move to
  `decisions/` and extraction imports them.
* **The schema** (§5.3).
* **Input bounding** (§6.2), applied before the call, so no adapter can forget it.
* **Usage capture** into a `DecisionUsage` the caller meters (§5.4).
* **No content in logs.** Log lines carry the purpose, question ids, item ids and error
  types, never the state or a reason. Strands' `ClassifierStrategy._warn_declined` takes
  the same line, for the same reason.

It does not own any policy about what `Uncertain` or `Failed` means (§8).

### 5.2 Why not `LLMDecisionModel`

Three reasons, all from code we already measured:

1. **Its schema collides with our ceiling.** Each question carries
   `reason: Optional[str]`, a union-typed parameter. Bedrock refuses a strict schema with
   more than 16 of them (`_CAPACITY_MARKERS`), so a strict `LLMDecisionModel` caps a set at
   16 questions before any other limit applies, and our wider schema shape already stops
   at 14 fields.
2. **It has no strict fallback.** Our fallback classification exists because "retry
   unenforced on any `ValidationException`" was measured to silently downgrade an
   over-wide set on every run.
3. **It meters into the turn** when given an `invocation_context`, which is the wrong
   price (D7), and has no hook for our `C#` rows.

Its system prompt's untrusted-content rules are good and are kept verbatim in ours.

### 5.3 The schema

Per question, an object `{answer: <enum>, reason: <string>}`, both required:

* `answer` is a string enum of the choices, plus `"uncertain"` when the question permits
  it. A boolean question's enum is `["true", "false"]` (plus `"uncertain"`), parsed back
  to `bool`. **No unions anywhere**, so the 16-union ceiling never applies.
* `reason` is a required string the prompt asks to leave empty unless the answer is
  `"uncertain"`. Required rather than optional because optional costs a union; bounded at
  200 characters after return.

How many questions per call strict mode will compile in this shape is **measured in PR-1,
not assumed** (§15). Until then the authoring limit is the field-set one, 14.

### 5.4 Usage and metering

`ask` returns usage on the result and the caller meters it through
`apis/shared/decisions/metering.py`, which has one function per scope:

| Scope | Where the cost goes | Used by |
|---|---|---|
| A session | a `C#` row with `callKind: "decision"` and `decisionPurpose` | `classify_items`, notify-if (the run's session), approvals |
| A project | `projects.add_call_cost`, as `MemoryMaintenanceRunner` does | project file labels |

A call that reports no usage emits `UnmeteredModelCall` with the purpose as its surface,
as `document_extract` does. `CALL_KINDS` gains `"decision"`, so the ledger counts these
calls and never as turns.

---

## 6. Items

### 6.1 Adapters

An adapter turns one kind of thing into a bounded `DecisionInput` plus a stable item id
and a display label. `apis/shared/decisions/items/`:

| Adapter | Item id | Content | First consumer |
|---|---|---|---|
| `document` | upload id | the prepared text layer, as extraction reads it | `classify_items` |
| `sheet_row` | `upload:sheet:row` | header plus one row, rendered as `column: value` lines | `classify_items` |
| `run_answer` | run id | the run's `final_message` | notify-if |
| `tool_call` | `toolUseId` | tool name, server, arguments | approvals (later) |
| `mail_message` | message id | subject, sender, latest body with quotes and signature stripped | mail triage (later) |

A new use case is an adapter and a surface. Neither touches the engine.

### 6.2 Bounding

Every adapter declares a character cap; the engine enforces it. Over the cap, the text is
cut to head and tail around an omission marker, the shape Strands' `_truncate_text` uses,
because the request is usually at the start and the conclusion at the end. Initial caps,
to be measured: 24,000 characters for a document (narrowed with `pages`, as extraction
allows), 2,000 for a row, 8,000 for a run answer.

### 6.3 The batch

`run_bounded` and the warm-first shape move from `extraction/batch.py` to
`decisions/batch.py`, with the concurrency variable renamed per surface
(`DECISIONS_CONCURRENCY`, same default and clamp). Warm-first applies only to strict
calls; a non-strict ad-hoc set has no grammar to warm.

---

## 7. Decision records

Where a result must outlive the turn (labels, corrections, run history), it is stored as
a record, not a message.

```
DecisionRecord {
  scope:        session | project            # who may read it
  scopeId:      sessionId | projectId
  ownerId:      userId                        # never matched on scopeId alone
  purpose:      classify_items | schedule_notify | project_label | ...
  subjectId:    item id from the adapter
  setName, setVersion
  answers:      {questionName: value | "uncertain" | "failed"}
  reasons:      {questionName: str}           # only where §11.2 allows
  modelId, schemaEnforced, createdAt, expiresAt
  correctedBy?, corrections?: {questionName: value}
}
```

* **Session scope** reuses the `TSUM#` / `EXTR#` pattern: rows in sessions-metadata on the
  existing `SessionLookupIndex`, replayed on `GET /messages`, no new infrastructure.
  A run over hundreds of rows writes one summary row plus answer pages kept under the
  400 KB item limit; the artifact holds the full table regardless.
* **Project scope** (labels, later) needs a store keyed on the project. Its table design
  is that surface's PR, not this one.
* **Corrections** are first-class: when a user changes a label, the record keeps both the
  model's answer and the correction. The agreement rate per question per set version
  (§14) is computed from them, and it is how an office finds a badly worded question.
* Records hold answers, not content (§11).

---

## 8. What callers decide

The engine returns outcomes. Each surface maps them, and its spec section states the
mapping in this table's form:

| Surface | Answer | Uncertain | Failed |
|---|---|---|---|
| `classify_items` | table cell | review list | named in the result, batch marked incomplete |
| Notify-if | notify on `true`; record "not met" on `false` | user's choice, default notify | notify, saying the condition could not be checked |
| Approvals (later) | skip the card only on a confident "not risky" | ask | ask |
| Project labels (later) | label | the project's Uncertain filter | retry on next sync, then Uncertain |

Two rules hold everywhere:

* **Fail safe in the direction of the human.** Where a wrong answer hides something from
  the user (a skipped card, a missed notification), `Uncertain` and `Failed` resolve to
  showing it.
* **Every suppression leaves a visible reason.** A tool that ran without a card says so on
  its row. A run that did not notify says so in its run history. A decision that changed
  what happened to the user must be explainable after the fact.

---

## 9. Invariants

| Invariant | Enforced by |
|---|---|
| No decision runs before the first token: not in route phases, `BeforeInvocationEvent` or `BeforeModelCallEvent` | an architecture test allowlisting the modules that may import `apis.shared.decisions.engine`, in the style of `test_attachment_kb_retrieve_boundary.py` |
| Questions never reach the turn's system prompt or `toolConfig`; the agent-facing tool spec is one constant per deployment | a digest-pinned spec test, as `document_extract` has |
| No content is persisted: records hold answers and bounded reasons only | a store test that rejects a record carrying item text |
| The model-facing tool result carries labels and answers only: never a reason, item text or cell value, and a row's label is its position | a result-builder test whose fixture plants a sentinel string in every reason, cell and document, and asserts it appears nowhere in the result at any size, below and above the cap |
| Records and in-process state are keyed by owner as well as scope id | the store's key builder; a test that two users on one session id never read each other's records |
| Every call is metered against a catalog row, or names itself unmetered | the metering tests; `UnmeteredModelCall` |
| `strict_tools=True` is set only on a dedicated side-call instance, never on a chat agent | `tests/architecture/test_strict_tools_single_call_site.py`: its `_ALLOWED` set moves from `extraction/extractor.py` to the module that builds the model once that code moves into `decisions/` (PR-1), so there is still exactly one site |

---

## 10. Surfaces

### 10.1 `classify_items` (first consumer)

**What the model calls:**

```
classify_items(
  question_set?: str,                 # an authored set among the turn's skills
  questions?: [{name, question, choices}],   # ad hoc, at most 8; exactly one of the two
  upload_ids?: [str],                 # documents
  sheet?: {upload_id, sheet?, range?},       # spreadsheet rows
  from_run?: {run_id, where},         # read back an earlier run's answers; no model call
)
```

**What goes back to the model.** The tool result is the Converse payload, so it becomes
part of every later turn's cached prefix. It carries the least that lets the model answer
the normal follow-up ("which ones need cost share?") without a re-run. `document_extract`
learned that withholding values forces a document-sized re-run to answer exactly that
(`structured-extraction.md` §5.1), and the same holds here.

1. **Per-item answers, while they fit.** One line per item: its label and its answers,
   in the set's authored order, as the choice values themselves plus `uncertain` and
   `failed`:

   ```
   award-114.pdf: has_cost_share=true sponsor_type=federal needs_pi_signature=false
   award-131.pdf: has_cost_share=uncertain sponsor_type=foundation needs_pi_signature=true
   ```

   Up to 2,000 characters for the whole result.
2. **Past the cap, counts.** Per question, the count of each answer, then the labels of
   every `Uncertain` and `Failed` item (capped in turn, with "and N more"), then the
   artifact reference. The model can answer "how many" from the counts and says the table
   holds the rest.
3. **Reading answers back costs no model call.** `from_run` with a `where` such as
   `has_cost_share=true` returns the matching items' labels from the session's records
   (§7), under the same cap. A follow-up over a 500-row run is a record read, not 500
   decisions.

**What never reaches the model, whatever fits:**

* **Reasons.** A reason is model text written about the item and can quote it. Reasons
  travel on the `decision_run` event, the session records and the artifact only.
* **Item content.** No document text, quote or cell value.
* **Content inside labels.** A document's label is its filename, which the conversation
  already shows on the attachment. A spreadsheet row's label is its position
  (`Responses!14`), never a cell value, so classifying 500 survey answers puts none of
  their text in the conversation. A mail message's label (later) is its sender and
  received time, not its subject.

**What the model has anyway.** The tool does not remove what the conversation already
holds. Ad-hoc questions are written by the chat model, so they are in history by
definition. A document the user attached is already in the context inline, as a digest or
on demand, and classifying it adds nothing and hides nothing. What stays off the chat
model is content it never read: rows of a sheet, files in a folder, an inbox. Acting on a
result brings the acted-on items in: drafting replies to the ten messages that need one
means reading those ten, and never the other 190.

**What goes to the user:** a `decision_run` SSE event drained in `stream_coordinator`
beside `document_extraction`, the session records (§7) for reload, and a backend-written
artifact and `.xlsx` with an Uncertain column. The SPA renders a verdict table with the
`Uncertain` items first; `Uncertain` gets one shared visual treatment, reused by every
later surface.

**Gate.** RBAC catalog entry `classify_items`, `enabledByDefault: false`, plus the flag
(§13). Constant per user, so it changes `toolConfig` once when granted, never between
turns. It is **not** gated on the turn having a document or a question set: a gate that
flips on a per-turn fact rewrites the cached prefix (the lesson of the document-read tool
gate).

**Size and cost before running.** Initial caps of 50 documents or 500 rows per call. Past
a threshold (initially 100 items), the tool computes an estimate from the adapter caps
and the catalog row and pauses on `ask_user_question` with the estimate before spending
it.

**One tool or a mode of `document_extract`?** Same machinery, different arguments
(rows, ad-hoc questions) and a different model-facing verb. Proposed: a separate tool,
because `document_extract`'s spec is digest-pinned and its arguments do not fit rows.
**Settled by eval, not taste** (PR-7): measure how often the model picks the right tool
for a prompt set with both present.

### 10.2 Notify-if on scheduled runs (second consumer)

**What the user sets:** on a schedule, an optional condition in plain language, at most
500 characters ("only if a sponsor deadline moved or a new one appeared"), and "notify
me when it can't tell" (default on).

**What happens:** after `run_agent_headless` returns `completed`, the worker asks one
boolean question, the condition, over the `run_answer` item. Mapping per §8. The result
and its bounded reason go on the run's history row, so "why didn't I hear about this?"
has an answer.

**What is new:**

* `ScheduledPrompt` gains `notify_condition: Optional[str]` and
  `notify_on_uncertain: bool`.
* `NotificationKind` gains `schedule_condition_met`, payload `{scheduleId, label,
  sessionId, reason}`. The reason is model text about the user's own run, bounded.
* The worker Lambda needs `bedrock:InvokeModel` on the decision model's profile, and
  `DECISION_MODEL_ID` in its environment. It does not call Bedrock today.
* Metering writes a `C#` row on the run's own session, so the call shows up where the run
  does.

A schedule with no condition behaves exactly as today: no notification on completion.

### 10.3 Later surfaces (sketched; each gets its own section when built)

* **Risk-classified approvals.** A catalog value `approvalMode: always | classify |
  never` replacing the boolean, with `needs_approval: true` read as `always`. On
  `classify`, `ToolApprovalHook` asks the engine over the `tool_call` item; a skipped
  card is recorded on the tool row. Users may choose stricter than the admin, never
  looser.
* **Project file labels.** A project binds question sets; folder sync and uploads label
  each new file off the turn path; labels become Files-panel columns and filters, with
  `Uncertain` as a filter; the agent reads labels instead of reopening files.
* **Mail triage.** The `mail_message` adapter behind `classify_items` and notify-if,
  once a mail connector is in the deployment. Labels and suggestions only; any write to
  the mailbox goes through approvals.
* **Release-ledger quality signal.** After a turn, fire-and-forget: "did the user's next
  message correct or redo the previous answer?". Answers only, counted per release.
* **Batch library outside the platform.** The engine is plain Python over Bedrock; a
  lakehouse job may import it. That job, its data and its governance are out of scope
  here.

---

## 11. Privacy and decisions about people

### 11.1 Where content goes

Content goes to Bedrock in the deployment's own account, on the decision model's
profile, and nowhere else. No third-party decision provider (D12).

### 11.2 Where reasons may be stored

A reason is model text derived from the item and can quote it ("mentions her medical
leave"). A reason is stored **only where the item itself is visible to the same
readers, for the same lifetime**: in the session that already holds the item, on the run
history of the user's own schedule, on a project label readable by the members who can
read the file. Anywhere else, the record keeps the answer and drops the reason.

### 11.3 Decisions about people

Admissions, grading, hiring, misconduct and student-success uses are where a wrong or
biased verdict does real harm, and where FERPA applies to the content. The primitive
does not forbid them, because triage with a human decision behind it is a legitimate
use. Every surface built on it must:

* present verdicts as triage for a reviewer, never as the decision;
* keep `Uncertain` enabled on any question about a person;
* never trigger an action from a verdict alone.

A question set may declare `subject: person` in its frontmatter; surfaces then show the
triage wording and refuse `uncertain: no` on its questions. Whether such sets need their
own RBAC grant is open (§17).

---

## 12. Cost and TTFT

**Nothing before the first token.** `classify_items` runs when the model calls it.
Notify-if runs in a Lambda after the run. The architecture test in §9 keeps it that way.

**Against the prompt-cache contract:**

* `toolConfig` gains one constant `classify_items` spec for granted users, once.
  `scripts/measure_tool_spec_tokens.py` is re-run against dev and the growth is stated in
  the PR as one deliberate prefix rewrite.
* Questions travel in the tool's arguments or the skill bundle, never in the spec or the
  system prompt.
* The model-facing result is bounded (§10.1), so a large batch does not grow history.

**Per call:** one bounded request per item, whose input is the item plus the questions
and whose output is about 10–60 tokens per question. Priced from `DECISION_MODEL_ID`'s
catalog row, which should be a small model; the choice is measured against agreement in
PR-1, not assumed.

---

## 13. Flags

The engine has no flag: it is a library, and nothing calls it until a surface does. Each
surface has its own, in development, so off by default and only `"true"` enables:

| Surface | Backend | Frontend | Where |
|---|---|---|---|
| `classify_items` | `CLASSIFY_ITEMS_ENABLED` / `CDK_CLASSIFY_ITEMS_ENABLED` | `features.classifyItems` | AgentCore Runtime |
| Notify-if | `SCHEDULE_NOTIFY_CONDITION_ENABLED` / `CDK_SCHEDULE_NOTIFY_CONDITION_ENABLED` | `features.scheduleNotifyCondition` | app-api and the scheduled-runs worker |

`DECISION_MODEL_ID` (and `DECISIONS_CONCURRENCY` when set) is written to the Runtime only
while `CLASSIFY_ITEMS_ENABLED` is on, so the default spends none of the V2 environment
budget. Both are **feature switches** once finished: each costs money per item and widens
what users can automate.

---

## 14. Decisions, with reasons

**D1. Adopt Strands' `DecisionModel` as the contract; implement `_ask` ourselves.** The
interface gives us tracing and lets Strands consumers take our engine unchanged.
`LLMDecisionModel` is not used, for the three reasons in §5.2.

**D2. `Uncertain` is an enum member and a first-class outcome; `Failed` is separate; there
is no `null`.** Merging them would make an incomplete batch look like a review list
(§3).

**D3. Question sets reuse the field-set grammar in a distinct file.** One parser,
serializer and authoring page; a separate filename so the resolver can offer each tool
only the sets it runs, and so the model is never handed a field set to classify with.

**D4. Strict only for authored sets.** A strict schema pays a measured 15–16 s grammar
compile the first time Bedrock sees it, then stays warm for 24 hours. An authored set
amortizes that; an ad-hoc set made up in chat would pay it on its first item every time.
Ad-hoc sets run non-strict and are validated on return.

**D5. A separate `DECISION_MODEL_ID` with a required catalog row.** Not a fallback to
`DOCUMENT_EXTRACT_MODEL_ID`: a silent fallback is how a deployment ends up running
decisions on a model nobody chose. Missing id or row fails loudly at the surface.

**D6. Decisions never run before the first token,** enforced by an import-boundary test
rather than by review.

**D7. Never pass `invocation_context` to `ask`.** Strands would add the decision's tokens
to the turn's usage, where they would be priced at the turn model's rate. Decisions are
metered on their own row with their own model's price.

**D8. Records hold answers; reasons only where §11.2 allows; content never.**

**D9. The engine has no policy for `Uncertain` or `Failed`.** Each surface states its
mapping (§8), because "ask the user" is right for approvals and wrong for a 500-row
batch.

**D10. Every suppression leaves a visible reason** (§8).

**D11. Build the primitive from two unlike consumers, then migrate `document_extract`.**
An abstraction designed from one consumer fits only that consumer; `document_extract`
works today and gains nothing from being rewritten first.

**D12. No third-party decision providers.** They send content outside the account, and
they are the only source of confidence scores, so the platform shows none.

**D13. Model routing is out of scope.** `ClassifierStrategy` picks a model before every
invocation: mid-conversation that re-writes the cached prefix on each switch, and before
the first token it adds a model round-trip. Failover with `FallbackStrategy` is a
different question and belongs with `model-fallback-profiles.md`.

---

## 15. PR sequence

1. **PR-1, engine (dark).** `apis/shared/decisions/` with the engine, the schema builder,
   the batch runner and the `document`, `sheet_row` and `run_answer` adapters; the shared
   call code moved out of `extraction/` with extraction importing it unchanged in
   behaviour; metering helpers; `CALL_KINDS` gains `decision`; the §9 tests. Plus a
   measurement script run on dev: the widest question set strict mode compiles in §5.3's
   shape, cold and warm latency, tokens per item per question, and agreement on a
   labeled fixture for two candidate models. Numbers go in this spec.
2. **PR-2, notify-if backend.** `ScheduledPrompt` fields, the worker's call and run-row
   result, the notification kind, worker IAM and environment in CDK, the flag and its
   `platform.yml` variable.
3. **PR-3, notify-if SPA.** The condition field on the schedule form, the run-history
   line, the notification's rendering.
4. **PR-4, question sets.** `kind: questions` and the `uncertain` column in the parser and
   serializer, the resolver, and the authoring page's "Question sets" section.
5. **PR-5, `classify_items` backend.** The tool, its digest-pinned spec, the size and cost
   pause, the broker and `decision_run` event, session records, the artifact and
   workbook, metering. Adds `decision_run` to CLAUDE.md's SSE table.
6. **PR-6, `classify_items` SPA.** The verdict table, the shared `Uncertain` treatment,
   the review list, "Save as a question set".
7. **PR-7, evals.** Tool choice between `document_extract` and `classify_items`;
   agreement and `Uncertain` rate on labeled fixtures; a notify-if false-negative set of
   seeded runs.

---

## 16. How we'll know

* **Agreement:** on a hand-labeled fixture per first use case, the share of committed
  answers that match the label, and the `Uncertain` rate. Proposed bar for shipping a
  surface: at least 90% agreement on committed answers with `Uncertain` under 15%. The bar
  is a proposal until PR-1's numbers exist.
* **Corrections in use:** the per-question correction rate from §7's records, read per
  set version, so a reworded question shows up as a change.
* **Latency:** warm per-item latency and batch wall time at the default concurrency.
* **Cost:** tokens and dollars per item from the `callKind: "decision"` rows, against
  PR-1's estimate.
* **No regressions on the turn path:** `turn_prelude` marks unchanged with the flag on
  and off; the `toolConfig` digest changes once per granted user and never between turns.
* **Notify-if:** on the seeded set, no run that met its condition went un-notified.

---

## 17. Open questions

1. **One tool or two** for documents (§10.1). Settled by PR-7's eval.
2. **Quota for project-scoped calls.** `add_call_cost` charges the project's month; does
   that count against the requesting user's quota, the project's, or neither?
3. **Item caps.** 50 documents and 500 rows are guesses; PR-1's measurements replace them.
4. **Personal schedules first?** Notify-if on project schedules raises who gets notified
   (creator, owner, members). Proposed: creator only in v1.
5. **A grant for `subject: person` sets** (§11.3), or only the triage wording?
6. **Reasons on durable labels.** §11.2 allows them on project labels visible to the
   file's readers; confirm that matches how project files are actually shared.

---

## 18. Non-goals

* **Model routing** (D13).
* **Confidence scores** (D12).
* **Acting on a verdict.** No surface sends, deletes, grants or denies anything because of
  a decision alone.
* **Generative side calls.** Titles and tool summaries stay as they are; a decision is a
  closed question, never prose.
* **Running inside `Before*` hooks or route phases** (D6).
* **The lakehouse pipeline** (§10.3).
