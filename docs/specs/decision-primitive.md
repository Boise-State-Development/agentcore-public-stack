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
**Revised 2026-11-15** after reading the AWS ML blog on decision models in AgentCore
Evaluations and an architecture article pairing AgentCore with Jev (§2.1a). Five changes:
a self-hosted Strands Decider as a later backend (§5.5), an ordered `score` question type
(§4.1), the quality signal moved to AgentCore Evaluations (§10.3), the three-labeler
validation method (§16), and `outcome` / `policyVersion` on decision records (§7).
**Revised again 2026-11-15** after reading the Strands decision-model docs and three
Decider posts (§2.1b): reasons kept out of traces (§5.1, §9), D7's reason corrected, the
engine checking its own answers (§5.1), Decider facts and a three-way rollout switch
(§5.5), deterministic rendering and per-backend caps (§6.2), per-choice criteria (§4.1),
and no zero-config models (D5, §9). Then designed for a managed, serverless decision
model that does not exist yet (§5.6, D16).
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

### 2.1a Outside the wheel (read 2026-11-15)

* **AWS ML blog, 2026-11-04**, on using decision models with AgentCore Evaluations. Read
  up to its validation section; any results reported after that are unread.
  * It names two backends. **Jev** is hosted by TypeSafe. **Strands Decider** is described
    as an open-source, 2-billion-parameter decision model from the Strands team, run as a
    self-hosted server, locally or on EC2 inside a VPC.
  * Both return a probability distribution and a confidence. Question types: boolean
    (returning P(true)), `choice` (up to 255 options) and `score` (2–10 ordered levels).
  * Decisions run as judges inside a **code-based evaluator** Lambda that receives the
    turn's spans: on demand, in batch over stored sessions, or online over sampled
    traffic. Never in the agent's request path.
  * The authors position decision models as an addition to LLM judges for narrow,
    well-defined checks, not a replacement.
* **The 1.65.0 wheel has no client for a self-hosted Decider.** The name does not appear
  in it, and `DecisionsAPIDecisionModel` points at the hosted `decisionapi.net`. Neither
  source establishes whether that service runs the same model.
* **An architecture article (2026-10-24)** pairing AgentCore with Jev argues the split
  this spec already makes: the decision model evaluates, the generative model explains,
  policy decides. It lists what to audit per decision: the question, the model version,
  the policy version and the final outcome. §7 adopts the last two.
* **A Jev developer site** (`jevtypesafeai.com`) states in its footer that it is not
  affiliated with TypeSafe. Its endpoint and claims are not used here.

### 2.1b The Strands docs and the Decider (read 2026-11-15)

This spec was first written from the installed 1.65.0 source. The decision-models guide
(Overview, Questions and answers, Decisions in an agent, Models, LLM decision model,
Observability) and three Strands blog posts were read afterwards. The docs list Python
SDK 1.64.0 as the latest release, so where the docs and the 1.65.0 code disagree, the
code is taken.

* **A custom backend is a `DecisionModel` subclass implementing `_ask`.** That is the
  documented route for "a classifier you host". **The base class does not check the
  answers `_ask` returns.**
* **The `decision` span records every answer, including an `Uncertain` answer's reason**,
  as `gen_ai.decision.answers`. The docs call it an event; the 1.65.0 code sets it as an
  attribute. The question text and the input are not recorded. Our inference image keeps
  conversation content out of traces with the variables
  `tests/security/test_otel_content_capture.py` pins. Those gate message content, not
  this attribute.
* **`invocation_context`.** Given one, a decision's tokens add to the request's reported
  usage and count toward its token limits, though not as a turn. Our code reads neither:
  no module uses `invocation_context`, and no invocation limits are set.
* **`LLMDecisionModel()` with no model** builds a zero-config `BedrockModel()`:
  * the model is `global.anthropic.claude-sonnet-4-6` (`DEFAULT_BEDROCK_MODEL_ID`);
  * the region is the session's, else `AWS_REGION`, else `us-west-2`;
  * the credentials are whatever the process has.

  The vended HITL handler's documented example constructs it exactly that way.
* **Strands Decider:**
  * Open source. The code, adapters and readout heads are Apache 2.0; the base weights
    carry their own terms. The weights are on Hugging Face, with the training data.
  * Models: a 2B on Qwen3.5-2B, then E2B, E4B, 12B and 26B-A4B on Gemma 4. Each is a
    pre-trained model with its text-generating head replaced by a pointer head that scores
    the options, fine-tuned with LoRA.
  * Question types `noul`, `choice` and `score`, in a **4,096-token window**.
  * On JevBench v1 (231 public tasks), accuracy runs from 77.9% (E2B, Brier 0.313) to
    89.6% (26B-A4B, Brier 0.162). H100 median latencies are about 100–250 ms.
  * Strands' own caveats: it can return the same answer when only the question changes;
    it struggles with long, multi-step documents; and its calibration was fitted on short
    classification tasks, so thresholds must be tested on your own traffic.
* **A worked deployment (Strands blog, 2026-11-09).** Two Amazon engineers replaced Sonnet
  4.6 with Decider E4B for 20-label ticket classification.
  * The setup: an SQS-triggered Lambda calling a network-isolated SageMaker
    `ml.g5.xlarge` endpoint, with weights copied in from S3.
  * They report about 98% lower cost, and better accuracy than Sonnet on their own
    tickets after tuning the confidence threshold.
  * Three practices carry over whatever the backend: a three-way `BEDROCK` / `DUAL` /
    `DECIDER` switch with every disagreement on real traffic reviewed; **sorted JSON
    keys**, because key order alone moved probabilities by up to 0.19; and each label
    rewritten as a one- or two-sentence criterion.

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
* **AgentCore Evaluations** works end to end on dev
  (`agentcore-evaluations-spike-findings.md`, 2026-08-12): 16 built-in evaluators,
  custom `llmAsAJudge` and `codeBased` (Lambda) evaluators, content delivered as OTLP log
  records rather than span events, and a ~180 s ingestion delay on the on-demand runner.
  The spike left one decision to make deliberately: an evaluator receives the full system
  prompt and every user message, so pointing one at production conversations is a
  choice, never a side effect.

---

## 3. The model of a decision

### 3.1 Three outcomes, kept apart

Every item-question pair ends in exactly one of:

| Outcome | Meaning | Who produced it |
|---|---|---|
| **Answer** | `true`/`false`, one of the allowed choices, or one of a score's levels | the model, committing |
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

### 3.3 Confidence is not a field in v1

The Bedrock engine reports no confidence, so no v1 surface shows a percentage, and
"Uncertain" is the whole signal.

A backend that does report it (§5.5) adds `confidence` and a per-question distribution to
the result. A caller may then set `min_confidence`: an answer below it becomes `Uncertain`
with reason `low_confidence`. A threshold the caller chose then replaces the model's own
judgement about when to decline, which is the better policy for approvals. Until a
backend's calibration has been measured on our fixtures (§5.5), no surface shows a
percentage or sets a threshold.

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
| budget_completeness | Budget justification | score | how fully the budget justification explains each cost category | missing, partial, adequate, complete | yes |
```

* It lives at `references/question-set.<name>.md` in a skill's reference bundle, so it
  inherits the skill's visibility, RBAC grant, Agent binding and project-harness binding,
  exactly as field sets do (`structured-extraction.md` D2). The flat
  `question-set.<name>.md` form follows §16.1 there: bundle filenames cannot contain a
  path separator.
* The parser, serializer, `FieldSetLayout` round-trip and authoring page are the
  field-set ones. A `kind: questions` file rejects any type outside `boolean`, `enum` and
  `score` by name.
* `uncertain` defaults to `yes`. `no` means the question must commit, as Strands'
  `uncertainty: False` does.
* **`type: score` is an ordered scale.** `values` lists 2–10 levels from lowest to
  highest. The answer is one level's label, and counts and tables sort by level. It is for
  rubric questions ("how complete is the budget justification?"), which a boolean
  flattens and an unordered `choice` loses the order of. It matches the `score` type
  AgentCore Evaluations uses (§2.1a).
  * Strands' `Question` has no score type, so the engine passes the levels as `choices` and
    keeps the order itself.
  * A backend that reports a distribution (§5.5) can also return the expected level.
  * `score` exists only in question sets. Field sets refuse it, because a level is a
    judgement, not a value quoted from the document.
* **A choice can carry a criterion.** An optional `## Choices` table after the questions
  gives each choice a one- or two-sentence criterion:

  ```markdown
  ## Choices

  | question | choice | criterion |
  |---|---|---|
  | sponsor_type | federal | funded by a US federal agency, directly or as a pass-through |
  | sponsor_type | foundation | funded by a private or community foundation |
  ```

  * The criterion travels to the backend with its choice. The label stays what tables and
    counts show.
  * It is a separate table rather than text inside `values`, because a criterion contains
    commas and `values` is comma-separated.
  * Strands reports that a Decider can return the same answer when only the question
    changes and the options stay fixed. The Amazon team rewrote each label as a short
    criterion for that reason (§2.1b).
  * It helps the Bedrock engine as well: the options are where the distinction is drawn.
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
* **Its own answer checks.** The base class does not check what `_ask` returns (§2.1b),
  so the engine does: every question answered, and every answer one of its choices or
  levels or `Uncertain`. Anything else is `Failed`. The Bedrock engine gets this from its
  Pydantic schema; a future backend must run the same check rather than trust its
  provider.
* **Reasons kept out of traces.** Strands' `decision` span records the answers, and an
  `Uncertain` answer's reason with them (§2.1b).
  * So the engine's `_ask` returns `Uncertain()` with no reason in `answers`.
  * It carries the reasons on its own result type, `PlatformDecisionResult`, a
    `DecisionResult` subclass.
  * The span still records which answers were uncertain. It never holds text derived from
    the item.

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
3. **It has no hook for our `C#` rows.** Its usage reaches the agent's request only
   through `invocation_context`, which we don't pass (D7).

Its system prompt's untrusted-content rules are good and are kept verbatim in ours.

### 5.3 The schema

Per question, an object `{answer: <enum>, reason: <string>}`, both required:

* `answer` is a string enum of the choices, plus `"uncertain"` when the question permits
  it. A boolean question's enum is `["true", "false"]` (plus `"uncertain"`), parsed back
  to `bool`. A score's enum is its levels in authored order, so the order survives into
  the schema. **No unions anywhere**, so the 16-union ceiling never applies.
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

### 5.5 Backends

The engine is one `DecisionModel`. A backend is another, chosen per deployment behind the
same interface (D1), so callers never change when it does.

| Backend | Where content goes | Confidence | Status |
|---|---|---|---|
| `PlatformDecisionModel` on Bedrock | Bedrock in the deployment's account | none | v1 |
| Self-hosted Strands Decider | a server in the deployment's VPC | distribution and confidence | later, on the criteria below |
| A managed, serverless decision model | an AWS service, in the deployment's account and region | expected | anticipated; adopted on §5.6's criteria once it exists |
| Hosted Jev, `decisionapi.net` | the provider's API, outside the account | distribution and confidence | excluded (D12) |

**Adopting a self-hosted Decider** is its own spec and PR. It happens only when all of
these hold, measured on our own fixtures (§16) rather than taken from a provider's claims:

1. **Agreement** at least matches the Bedrock engine's on the same fixtures, per question
   type.
2. **Calibration** is measured: across the fixtures, answers given at confidence *p* are
   right about *p* of the time. Until it is, no surface shows a percentage or sets
   `min_confidence` (§3.3).
3. **Cost per decision, including hosting,** beats Bedrock at the deployment's actual
   volume. The one public figure: a network-isolated SageMaker `ml.g5.xlarge` endpoint
   serving E4B at $1.408 an hour (us-east-1 list price), handling about 5,600 decisions
   an hour (§2.1b). That is about $1,000 a month per always-on instance. A few hundred
   decisions a day will not repay it, and it adds resources to a stack near its ceiling
   (`platform-stack-resource-budget.md`).
4. **Licence and provenance** are checked per model. The Decider's code, adapters and
   readout heads are Apache 2.0, but the base weights (Qwen3.5, Gemma 4) carry their own
   terms, and the training-data inventory records terms per source. The dependency is
   pinned exactly, like every other.
5. **A client exists.** Strands 1.65 ships none for a self-hosted server, so either
   Strands adds one or we write a `DecisionModel` subclass against the server's documented
   API.
6. **It rolls out through a three-way switch per surface:**
   * `BEDROCK`, today's behaviour;
   * `DUAL`, where both backends answer, the Bedrock answer is used, and every
     disagreement is recorded for review;
   * `DECIDER`.

   Going back is a configuration change. This is the Amazon ticketing team's method
   (§2.1b), and disagreements on real traffic are better evidence than a fixture alone.

The volume that could justify it is project file labels and batch work over a lakehouse,
not v1's interactive surfaces.

**What a Decider cannot take.** Its window is 4,096 tokens, and Strands reports it
struggles with long documents that need several reasoning steps. So input caps are per
backend (§6.2). Rows, mail, tool calls and run answers fit. A whole document does not,
and a surface that sends documents stays on Bedrock unless it narrows the input first.

### 5.6 Designed for a backend that does not exist yet

**The assumption.** A managed, serverless decision model will arrive, in Bedrock,
AgentCore or SageMaker's serverless inference. It will answer boolean, choice and score
questions with probabilities, authenticate with IAM, run in the deployment's account and
region, and price per token or per request. Its API is unknown.

**The goal.** Adopting it should take a backend class, a catalog row, a calibration
measurement and a shadow run. Nothing else changes: not the surfaces, records, question
sets, SPA or Runtime environment. Six seams make that true, and each is cheap to build
now and expensive to retrofit.

**1. A catalog row chooses the backend, not code.**
* `DECISION_MODEL_ID` names a model catalog row, as today (D5). The row's
  `decisionBackend` (`bedrock_llm` now; `decider_http` or a managed service later)
  picks the `DecisionModel` class, and the row carries that backend's settings (endpoint
  ARN or URL, region).
* Decision rows carry a `DECISION` output modality. An `is_decision_model` predicate
  keeps them out of every chat surface, exactly as `is_speech_model` keeps Nova 2 Sonic
  out (`apis/shared/models/modalities.py`).
* No backend adds a Runtime environment variable, which matters against the V2 budget of
  2,560 bytes.

**2. Each backend declares what it can do, and the engine reads that instead of
constants.**
* The declaration covers: question types, questions per call, choices per question, input
  window in tokens, content types, native batching, whether it reports confidence, and
  `calibrated`.
* `calibrated` is set by us after measurement (§5.5 criterion 2), never by the provider.
* Adapters size their caps from the window (§6.2).
* A question set the backend cannot take is either mapped by the backend (a score as an
  ordered choice, a criterion folded into the option text) or refused by name before any
  call. It never degrades silently.

**3. One internal question model, mapped per backend.** A question set becomes a
`DecisionQuestion`: boolean, choice or score, with criteria, uncertainty and
`min_confidence`. Each backend maps that to its own wire form. Strands' `Question` is one
wire form, not the internal model, because it has no score type and no criteria. If
Strands adds them, the mapping shrinks and nothing else moves.

**4. Confidence fields exist from day one, empty.**
* Records (§7) and the `decision_run` event carry nullable `confidence` and
  `distribution`, and a question set may declare `min_confidence` per question.
* While the backend is not `calibrated`, the engine ignores thresholds, stores nothing
  in those fields, and the SPA shows nothing.
* Turning on a calibrated backend then needs no schema migration and no SPA release just
  to start storing confidence. Showing it is a separate, deliberate SPA change.

**5. The row names its pricing basis.**
* Today's rows price per token. A serverless backend may price per request or per
  decision, and a hosted one costs per instance-hour whatever the call count.
* The row carries `pricingBasis`: `per_token`, `per_decision` (with
  `pricePerThousandDecisions`) or `hosted`. The metering helper (§5.4) prices from it.
* `hosted` writes `C#` rows with tokens and a zero call price on purpose, does not fire
  `UnmeteredModelCall`, and reports the instance cost as platform cost instead.
* The precedent is the speech prices added to the row for voice. Only `per_token` is
  accepted until a second basis is needed.

**6. Routing per surface is admin configuration.**
* Each surface has `{primary, shadow?, shadowSample}`, read through the existing catalog
  cache, so a change takes effect on the next cache fill. There's no deploy, which is
  the fast rollback the Amazon team relied on (§2.1b).
* Setting `shadow` is `DUAL`. The shadow call runs after the primary, never on its
  critical path, under its own timeout, and is metered. Answers are compared, and
  disagreements are recorded, answers only.
* A backend may override `ask_many` with a native batch endpoint; the default is the
  semaphore fan-out (§6.3).
* Every backend maps its errors into the engine's own reasons (`throttled`, `too_large`,
  `timeout`, `invalid_output`, `unavailable`), so a surface's mapping for `Failed` (§8)
  never depends on the provider. Strands' System One adapter already raises
  `ModelThrottledException` and `ContextWindowOverflowException` for the first two.

**Criteria for a serverless backend.** §5.5's, minus the hosting term:
* agreement and calibration measured on our fixtures;
* cost per decision against Bedrock;
* data handling: content stays in the account and region, or is covered by AWS service
  terms that exclude training on it;
* a client;
* a `DUAL` run on real traffic.

D12 still applies to any provider that takes content outside AWS.

**What is not built now.** No backend class beyond `bedrock_llm`, no hosting, no
`per_decision` or `hosted` pricing, and no shadow execution: the `shadow` field is
refused until a second backend exists. Its shape is fixed now so the config, the records
and the SPA never need it changed.

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

Every adapter declares a character cap **per backend**, and the engine enforces the one
for the backend in use.
* Over the cap, the text is cut to head and tail around an omission marker, the shape
  Strands' `_truncate_text` uses, because the request is usually at the start and the
  conclusion at the end.
* Initial caps for the Bedrock engine, to be measured: 24,000 characters for a document
  (narrowed with `pages`, as extraction allows), 2,000 for a row, 8,000 for a run answer.
* A backend with a smaller window (§5.5) declares smaller caps. An item that cannot fit
  even cut down is `Failed` with reason `too_large`, never silently reduced to nothing.

**Rendering is deterministic.** An adapter renders the same item to the same text every
time:
* a row's columns in the sheet's own order;
* a mail message's fields and a tool call's arguments in a fixed order;
* any JSON with sorted keys.

The Amazon ticketing team measured JSON key order alone moving a Decider's probabilities
by up to 0.19 (§2.1b). Without fixed rendering, two runs of the same set over the same
items are not comparable, and neither are two backends answering side by side in `DUAL`.

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
  outcome:      what the surface did          # notified | not_notified | card_skipped | asked | labelled | sent_to_review
  policyVersion                               # version of the surface's §8 mapping that chose the outcome
  confidence?, distribution?                  # empty until a calibrated backend answers (§5.6)
  backend, modelId, schemaEnforced, createdAt, expiresAt
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
  (§16) is computed from them, and it is how an office finds a badly worded question.
* **The outcome is recorded, not just the answer.** The question, the set version and the
  model say what was asked and what answered. `outcome` says what the surface did with the
  answer, and `policyVersion` says which version of its mapping (§8) chose that.
  * When approvals move from "skip on a confident not-risky" to a confidence threshold, a
    card skipped last month stays attributable to the rule in force at the time.
  * It turns D10's visible reason into a fact that can be counted. For example: how many
    cards did classify mode skip this week, and how many of those did a user later
    dispute?
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
  what happened to the user must be explainable after the fact, from the record's
  `outcome` and `policyVersion` (§7).

---

## 9. Invariants

| Invariant | Enforced by |
|---|---|
| No decision runs before the first token: not in route phases, `BeforeInvocationEvent` or `BeforeModelCallEvent` | an architecture test allowlisting the modules that may import `apis.shared.decisions.engine`, in the style of `test_attachment_kb_retrieve_boundary.py` |
| Questions never reach the turn's system prompt or `toolConfig`; the agent-facing tool spec is one constant per deployment | a digest-pinned spec test, as `document_extract` has |
| No content is persisted: records hold answers and bounded reasons only | a store test that rejects a record carrying item text |
| The model-facing tool result carries labels and answers only: never a reason, item text or cell value, and a row's label is its position | a result-builder test whose fixture plants a sentinel string in every reason, cell and document, and asserts it appears nowhere in the result at any size, below and above the cap |
| No reason reaches a trace: the `decision` span's answers carry `Uncertain()` with no reason, and reasons travel on the engine's own result | a case in `tests/security/test_otel_content_capture.py` that plants a sentinel in a reason and asserts it is absent from the exported `decision` span |
| Nothing is zero-config: no `LLMDecisionModel()`, `BedrockModel()`, `ClassifierStrategy` or `HumanInTheLoop` is built without an explicit model that has a catalog row | an architecture test over `decisions/` and every module that imports it |
| The engine checks every answer it returns: each question answered with one of its choices or levels, `Uncertain`, or `Failed` | an engine test whose backend stub returns an out-of-set value and omits a question |
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
* **Release-ledger quality signal, through AgentCore Evaluations.** A code-based
  evaluator Lambda wraps the engine and asks narrow questions over a session's spans:
  "did the user's next message correct or redo the previous answer?", or a `score` for
  how complete an answer was. It runs online over sampled traffic.
  * Nothing is added to the Runtime, not even work after the turn. The service already
    supplies sampling, span collection and result plumbing (§2.2).
  * Results arrive minutes after the turn, which suits a per-release signal.
  * **Precondition:** the evaluator receives the full system prompt and every user
    message. Running it over production conversations is decided explicitly first, with
    its own privacy review, as the evaluations spike asked. Until then it runs over the
    eval harness's synthetic sessions only.
  * Answers are counted per release; no content is stored.
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
read the file. Anywhere else, the record keeps the answer and drops the reason. Traces
count as anywhere else: no reason reaches a span (§5.1).

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
decisions on a model nobody chose. Missing id or row fails loudly at the surface. The
same reasoning rules out zero-config construction anywhere (§9). `LLMDecisionModel()`
with no model runs on `global.anthropic.claude-sonnet-4-6` with whatever credentials the
process has (§2.1b). That id may have no catalog row in a deployment, which would leave
every such call unmetered, and it is a large model for a closed question. The id names a catalog row, and the row
chooses the backend (§5.6), so moving to another backend changes the row or this one
value, never code.

**D6. Decisions never run before the first token,** enforced by an import-boundary test
rather than by review.

**D7. Never pass `invocation_context` to `ask`.** Decisions are metered on their own `C#`
rows at their own model's price.
* Passing the context would add the same tokens to the request's reported usage too, so
  that figure would stop agreeing with our rows.
* If a deployment ever sets invocation token limits, a turn's decisions would spend its
  budget.
* Our code reads neither today (§2.1b). This rule prevents a future disagreement; it does
  not fix a current mispricing, which an earlier draft wrongly claimed.

**D8. Records hold answers; reasons only where §11.2 allows; content never.**

**D9. The engine has no policy for `Uncertain` or `Failed`.** Each surface states its
mapping (§8), because "ask the user" is right for approvals and wrong for a 500-row
batch.

**D10. Every suppression leaves a visible reason** (§8), and the record keeps what the
surface did and under which version of its mapping (§7).

**D11. Build the primitive from two unlike consumers, then migrate `document_extract`.**
An abstraction designed from one consumer fits only that consumer; `document_extract`
works today and gains nothing from being rewritten first.

**D12. No hosted third-party decision providers.** Jev and `decisionapi.net` send content
outside the account. They are no longer the only source of confidence: a self-hosted
Strands Decider would report it from inside the VPC, and is a later backend under §5.5's
criteria. v1 shows no confidence.

**D13. Model routing is out of scope.** `ClassifierStrategy` picks a model before every
invocation: mid-conversation that re-writes the cached prefix on each switch, and before
the first token it adds a model round-trip. Failover with `FallbackStrategy` is a
different question and belongs with `model-fallback-profiles.md`.

**D14. `score` is a question type.** Ordered rubric questions are common in the first use
cases and lose information as booleans or unordered choices. On the Bedrock engine it
costs one enum in the schema.

**D15. The ledger quality signal runs in AgentCore Evaluations, not the Runtime.** The
service already samples, collects spans and stores results; an evaluator Lambda is the
only new piece, and it adds nothing to any turn. It waits on the production-content
decision (§10.3).

**D16. Built for a backend that does not exist yet.** A managed, serverless decision model
is likely, and its arrival should be a configuration change, not a redesign (§5.6). The
six seams are built in PR-1 because each is cheap now and expensive later:
* nullable confidence fields avoid a record migration;
* a catalog row avoids an environment variable per backend;
* a capability declaration avoids constants scattered through the adapters;
* one internal question model avoids tying question sets to Strands' wire form;
* a pricing basis avoids a metering rewrite;
* a fixed routing shape avoids a config migration.

What would be speculative, a second backend class or shadow execution, waits for a
backend to exist.

---

## 15. PR sequence

1. **PR-1, engine (dark).** `apis/shared/decisions/` with the engine, the schema builder,
   the batch runner and the `document`, `sheet_row` and `run_answer` adapters; §5.6's
   seams (the backend chosen by catalog row with the `DECISION` modality and
   `is_decision_model`, the capability declaration, the internal question model, empty
   confidence fields, `pricingBasis` accepting only `per_token`, and per-surface routing
   accepting only `primary`); the shared
   call code moved out of `extraction/` with extraction importing it unchanged in
   behaviour; metering helpers; `CALL_KINDS` gains `decision`; the §9 tests. Plus a
   measurement script run on dev: the widest question set and the longest choice list
   strict mode compiles in §5.3's shape, cold and warm latency, tokens per item per question, and agreement on a
   labeled fixture for two candidate models. Numbers go in this spec.
2. **PR-2, notify-if backend.** `ScheduledPrompt` fields, the worker's call and run-row
   result, the notification kind, worker IAM and environment in CDK, the flag and its
   `platform.yml` variable.
3. **PR-3, notify-if SPA.** The condition field on the schedule form, the run-history
   line, the notification's rendering.
4. **PR-4, question sets.** `kind: questions`, the `uncertain` column and the `score` type
   in the parser and serializer, the resolver, and the authoring page's "Question sets" section.
5. **PR-5, `classify_items` backend.** The tool, its digest-pinned spec, the size and cost
   pause, the broker and `decision_run` event, session records, the artifact and
   workbook, metering. Adds `decision_run` to CLAUDE.md's SSE table.
6. **PR-6, `classify_items` SPA.** The verdict table, the shared `Uncertain` treatment,
   the review list, "Save as a question set".
7. **PR-7, evals.** Tool choice between `document_extract` and `classify_items`;
   agreement and `Uncertain` rate on labeled fixtures (§16); a notify-if false-negative
   set of seeded runs.
8. **Later, the ledger quality evaluator** (§10.3), once §17's production-content
   question is decided.
9. **Later, a second backend**: a managed serverless model or a hosted Decider, whichever
   clears its criteria first (§5.5, §5.6). Its own PR adds the backend class, its catalog
   row, its pricing basis if new, and shadow execution, then runs `DUAL` before any
   surface makes it primary.

---

## 16. How we'll know

* **Agreement, measured the way the AWS post validates a decision-model evaluator.**
  * Three people label each fixture item independently, and the majority label is the
    reference. An item where no two agree is set aside as ambiguous, never forced.
  * Report per question: agreement of committed answers with the majority label, and the
    `Uncertain` rate. Report both again split into **easy** items (all three labelers
    agreed) and **hard** ones (two of three).
  * The split is the point. A decider that is right on the easy items and declines the
    hard ones is behaving well. One that commits on the hard ones and gets them wrong is
    not, though the overall figure can look the same.
  * Report agreement among the labelers too, because it is the ceiling. A question three
    people can't agree on is worded badly, not answered badly.
  * Proposed bar for shipping a surface: at least 90% agreement on committed easy items,
    `Uncertain` under 15% overall, and hard-item agreement no lower than the labelers' own.
    The bar is a proposal until PR-1's numbers exist.
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
7. **Production conversations in AgentCore Evaluations** (§10.3): whether, under what
   review, and for which deployments.
8. **A self-hosted Decider** (§5.5): who would own the hosting, and whether the volume
   from project labels or lakehouse batches ever clears the cost criterion.
9. **Where per-surface routing lives** (§5.6): on the decision model's catalog row, or in
   a separate admin settings record read through the same cache. The row is simpler, but
   one row would then hold the routing for every surface.

---

## 18. Non-goals

* **Model routing** (D13).
* **Confidence scores in v1** (§3.3, §5.5, D12).
* **Acting on a verdict.** No surface sends, deletes, grants or denies anything because of
  a decision alone.
* **Generative side calls.** Titles and tool summaries stay as they are; a decision is a
  closed question, never prose.
* **Running inside `Before*` hooks or route phases** (D6).
* **The lakehouse pipeline** (§10.3).
