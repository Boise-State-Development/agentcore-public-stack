# AgentCore Evaluations — spike findings

**Date:** August 12, 2026 · **Status:** spike complete, recommendation below
**Question:** before we write the quality-veto eval harness by hand, does the
`bedrock_agentcore.evaluation` package in our existing pin do the job?

**Verdict: adopt it for judging and trajectory scoring; build our own arm
runner and experimental design. It removes roughly a third of the build and
cannot satisfy one requirement at all (blinding) — which is worth knowing
before someone designs around it.**

*Context:* the harness is the shared asset described by
`compaction-over-threshold-cache-spiral.md` §4.3, `compaction-v2-versioned-prefix.md`
§8, and `document-offload-evaluation.md` §2. All three predate the
`bedrock-agentcore` 1.21.0 bump (#857, merged 2026-08-11) and none mentions
this package. It has in fact been in the SDK since at least 1.9.1.

---

## 1. What was verified, and how

Everything below was run against **dev-ai** (`us-west-2`), not read from docs.

| claim | evidence |
|---|---|
| The APIs exist on our pinned boto3 | boto3/botocore **1.43.68** expose `Evaluate`, `StartBatchEvaluation`, and the full evaluator CRUD on `bedrock-agentcore` / `bedrock-agentcore-control` |
| The service is authorized for us — not preview-gated | `list_evaluators()` returns **16 built-in evaluators**, all `ACTIVE` |
| The whole pipeline works end-to-end **today, unmodified** | Ran `EvaluationClient.run()` against a real dev conversation: 272 spans collected, 4 scored results returned, explanations quoting the actual conversation text |
| Session correlation is already deterministic | `runtime_session_id_for()` (`apis/shared/harness/runner.py:63`) is `sid-<sha256(session_id)>` — every conversation the existing headless harness drives maps to its spans with no new plumbing |

### The built-in evaluators

- **TRACE, response quality (8):** Correctness, Faithfulness, Helpfulness,
  ResponseRelevance, Conciseness, Coherence, InstructionFollowing, Refusal
- **TRACE, safety (2):** Harmfulness, Stereotyping
- **SESSION (4):** GoalSuccessRate, TrajectoryExactOrderMatch,
  TrajectoryInOrderMatch, TrajectoryAnyOrderMatch
- **TOOL_CALL (2):** ToolSelectionAccuracy, ToolParameterAccuracy

Custom evaluators come in two shapes, per the `CreateEvaluator` input model:
`llmAsAJudge` (instructions + rating scale + model config — fully managed, no
infra) and `codeBased` (a Lambda ARN, wrapped by the SDK's
`@custom_code_based_evaluator()` decorator).

### Where the conversation content actually lives — read this before extending anything

This is the part a documentation-only read gets wrong, and it nearly derailed
this spike.

`aws/spans` in dev-ai holds 117 MB of spans, and **not one of them carries
message content.** The `strands.telemetry.tracer` spans there have token counts
and model ids and nothing else; a query for any span with a non-empty `events`
array across seven days returns **zero rows**. Read only that, and the obvious
conclusion is "content capture is off, this needs observability work first."

That conclusion is wrong. Content arrives as **OTLP log records**, not span
events — emitted by `strands.telemetry.tracer` into the *runtime* log group with
`eventName = "strands.telemetry.tracer"` and a `body` of:

```
{"input":  {"messages": [{"role": "system"|"user"|"tool", "content": …}, …]},
 "output": {"messages": [{"role": "assistant", "content": …}]}}
```

They carry `traceId` / `spanId` / `scope`, so they duck-type past the
collector's `_is_valid_adot_document` check and get swept up alongside real
spans. In the session sampled: 21 content-bearing log records among 359
documents. `CloudWatchAgentSpanCollector` queries **both** `aws/spans` and the
runtime log group and unions them, which is why the end-to-end call works —
the content comes entirely from the second query.

Two consequences worth writing down:

1. **Don't "fix" content capture.** It isn't broken. Setting
   `OTEL_SEMCONV_STABILITY_OPT_IN` tokens to chase span events would change the
   emission shape out from under a path that already works.
2. The runtime log group name is the AWS-suffixed one
   (`…_agentcore_runtime-Z6D3HsHKs6-DEFAULT`), not the prefix-derived name.
   Querying the wrong one returns zero rows rather than an error — the same
   trap that hid the broken dashboard widgets fixed in #843.

### What a real result looks like

`Builtin.Correctness` on a live dev conversation returned `value: 1.0`, label
`"Perfectly Correct"`, `tokenUsage` of 1,728 tokens, and a paragraph of
explanation that correctly identified that the agent's tool calls had 502'd,
that it declined to fabricate results, and that it had accurately reported the
failure. That is a usable judge, not a rubber stamp.

---

## 2. What it does not give us

The harness specs ask for a specific experimental design. The SDK gives a
runner, not that design.

- **Paired A/B arms.** `OnDemandEvaluationDatasetRunner` executes one dataset
  against one invoker. Arms, k=3 replicates at production temperature,
  order-swapped pairwise judging, per-family reporting, McNemar — all ours.
- **Blinding — and this one is structural, not a gap.** The offload eval
  (§2.4) requires stripping transcripts to *final answer text only* before
  judging, because arm C transcripts contain `document_read` calls and
  `<document-digest>` blocks that give the arm away. AgentCore Evaluations
  works by feeding the evaluator the whole span set — content-in-spans **is**
  the mechanism. There is no scrubbing seam. **The blinded pairwise holistic
  judge therefore cannot run through this service**; it needs our own judge
  over scrubbed text. Everything else can.
- **Programmatic ground-truth scoring.** `ReferenceInputs` carries
  `assertions` / `expected_response` / `expected_trajectory`, but exact-match
  scoring against planted facts, table cells, and remapped page numbers is a
  local function. Routing it through a Lambda-backed `codeBased` evaluator is
  infrastructure weight for something a pure function does better.
- **Cost and token metrics.** We already read these from the `C#` rows, and
  `partial_miss` (#838) is a better instrument than anything in the spans.
- **The corpus.** Planted-fact document generation, the BSU public scrape, the
  chart-only pages that make the dual-encoding question answerable — all ours,
  and that was always where the actual thinking lived.

### Operational notes

- The on-demand runner sleeps `evaluation_delay_seconds` (**default 180s**) for
  span ingestion, then the collector polls on top of that. Batch the arms;
  don't put this in a tight loop.
- Each `evaluate` call is billed LLM usage (~1.7k tokens for one TRACE-level
  Correctness call). At 120 tasks × 3 replicates × 2 arms this is real but
  modest — budget it, and prefer the free trajectory matchers where they answer
  the question.
- Evaluator level drives batching: SESSION sends one request, TRACE fans out
  over trace ids, TOOL_CALL over tool span ids, capped at 10 targets per
  request.

### One scoping decision to make deliberately

The `body` payload carries the **full system prompt and every user message**
into an AWS-managed evaluator. For the synthetic corpus the harness is designed
around, that is a non-issue — the corpus is ours and contains no user content.
It becomes a real decision only if someone later points this at recorded
production conversations. Decide it then, explicitly; don't let it happen as a
side effect of reusing the same script.

---

## 3. Recommendation

**Hybrid.** Concretely:

**Use AgentCore Evaluations for**
- the tool-trajectory families — `TrajectoryInOrderMatch` and
  `ToolSelectionAccuracy` directly answer "did the model call `document_read`
  when it needed to, with the right arguments", which is the
  `document_read` health band the offload eval (§3.1) already wants
- `GoalSuccessRate` and `InstructionFollowing` for the long-session
  constraint-retention families in #833 §4.3
- a **secondary, non-blinded** quality signal — useful as the second judge the
  eval design calls for on a 20% sample

**Build ourselves**
- the arm runner, extending `experiment_agent_cache_arms.py` (already drives
  real multi-turn dev-ai sessions through the runtime gateway; don't rebuild
  it — and salt priming text per arm, per the G1 confound)
- the corpus and planted-fact generation
- programmatic lookup / citation / page-identity scoring
- the **blinded** pairwise holistic judge plus the scrubber, which the managed
  path cannot do
- the statistics and the stopping rule

**Net effect:** this removes roughly a third of the build — the judging
infrastructure, tool-trajectory scoring, and result plumbing — and leaves the
experimental design, which is the part that was always going to need care. It
also converts one open spec question ("who writes the judges?") into a
configuration choice.

**What it does not change:** the harness is still a prerequisite for #833 PR-2,
#833 PR-4, #835 v2, and #836 PRs 1–4, and it still needs an owner. It is now a
smaller job than the specs assumed.

---

## 4. Follow-ups this spike opens

1. **The three eval spec sections should be amended** to name the hybrid split
   — otherwise the next reader re-derives it, or worse, designs the blinded
   judge on top of a service that cannot blind.
2. **`Builtin.Faithfulness` is worth a second look for the offload work
   specifically.** It scores whether a response is supported by provided
   context — which is close to the exact question "did the digest lose
   something the document had". Not in any spec today; may be a better primary
   than a hand-rolled rubric for the holistic family.
3. **Online evaluation configs** (`CreateOnlineEvaluationConfig`) went
   unexamined here. If they sample live traffic continuously, that is a
   candidate for the "standing verification in prod" the spiral spec asks for
   in §4.4 — but it points a managed evaluator at real user conversations, so
   read §2's scoping note first.
