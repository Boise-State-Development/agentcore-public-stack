# Online evaluation — quality and usage trends from sampled live traffic

**Status:** Draft, 2026-11-19. Nothing built. Proposed as a flag-gated, fork-neutral feature; the first deliverable is a dev-only trial (§10 PR-1). Tracking issue: #2172.
**Refs:** `docs/specs/agentcore-evaluations-spike-findings.md` (proved the managed judges work on dev; §4 item 3 named online configs as the unexamined follow-up), `docs/specs/eval-harness.md` (the synthetic golden set; this spec is the live-traffic complement it rules out of its own scope in §1), `docs/specs/response-feedback.md` §8–§9 (the governance and "no pooled score" rules this spec inherits), `backend/src/apis/shared/feedback_eval/sampler.py` (the "numbers leave, prose does not" rule in code), `.kiro/steering/observability.md` (alarm factory, resource and dashboard budgets), AWS docs: [online evaluation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/create-online-evaluations.html), [Insights](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/insights-how-it-works.html), [CloudWatch Logs data protection](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/mask-sensitive-log-data.html).

---

## 1. Problem

Cost on this platform is measured per model call. Quality on live traffic is not measured at all. The eval harness measures 57 synthetic cases nightly and refuses, by design, to read a real conversation. The thumbs-down judge reads real turns but is starved: prod produces about one thumb a day. Nothing answers two questions leadership and the team both ask:

1. **Is the platform answering well, and is that getting better or worse?** Per release, per model swap, per prompt edit.
2. **What are people using it for?** Which kinds of work, in what proportions, changing how over time.

An earlier internal project answered both by sampling conversations, having a model name the topic, and having a second model grade the answer. It worked. Its dominant cost was PII redaction through Comprehend (about $1 per million characters) before anything else ran. This spec reaches the same outcome with AWS-managed parts, where redaction costs $0.12 per GB instead.

## 2. Who can read what

This table is the spec's load-bearing decision. Everything else exists to make it true.

| Output | Contents | Readable conversation text? | Who may read it | How that is enforced |
|---|---|---|---|---|
| **CloudWatch metrics** (`<prefix>/OnlineEvaluation` namespace) | Evaluator name, topic bucket, score, count, timestamp | **No.** Numbers only. | Anyone with CloudWatch read; the admin dashboard | Nothing to enforce: a metric cannot carry text |
| **Runtime log group** (`/aws/bedrock-agentcore/runtimes/<runtime>-<id>-DEFAULT`) | Every turn's messages, system prompt, tool I/O, as OTel log records | **Yes, masked.** PII is masked on every read for principals without `logs:Unmask`. The unmasked original is stored until retention deletes it. | The evaluation execution role (masked only). A break-glass admin role for debugging. | Data protection policy on the group; `logs:Unmask` granted to no role this stack creates; retention 7 days while the flag is on (D4); IAM read scoped to the prefix |
| **Results log group** (dedicated, created by this feature) | Per-session scores **and the judge's explanation**, which quotes the (masked) conversation | **Yes, masked.** | Dev team only, to debug a dip | Own data protection policy; own retention; IAM read limited to the platform operator role |
| **Agent Insight Store** (only when `insightsEnabled`) | Generated failure-pattern names, descriptions, root causes, sample session ids | **Yes.** Prose that paraphrases real chats. | Dev team only | Separate flag, default off (D6). Store encrypted with the stack KMS key. No product surface reads it. |
| **Admin dashboard** (`/admin/evaluations`, phase 3) | Trend charts drawn from metrics | **No.** | Anyone with `admin.costs` | The route reads CloudWatch metrics only. It never calls `GetAgentInsight` or reads either log group. A test pins this (§9). |
| **The judge itself** (AWS-managed built-in evaluators) | Reads the masked session for the seconds it takes to score | Transient | AWS service account, not ours | Nothing to enforce from our side. It is a policy decision each environment signs when it sets the flag (D1). Note the cross-region point in §11. |

Two facts a reader should not have to rediscover:

- **Masking is a read control, not a storage control.** CloudWatch detects PII at ingest and masks it "at all egress points" for anyone without `logs:Unmask`. The raw log event is retained. There is no AWS setting that stores only the masked form. Storing only masked text would need PII detection in our own telemetry path on every turn, which is the Comprehend-shaped cost this design exists to avoid, or a regex pass that misses names. Neither is proposed.
- **This is a second copy, not a new class of data.** Every conversation is already stored in plain text in the sessions messages table; that is how history works. What this feature adds is a second set of readers (anyone with CloudWatch read on the runtime log group), and the controls above are about that set.

## 3. Thesis

> **Sample the deployed agent's real traffic, score it with managed judges, and keep only numbers.** The existing harness proves a change did not break the golden set. This proves the platform is doing its job for the people actually using it, and says what that job is. Both are needed; neither replaces the other.

Three design commitments follow, and §4 makes each one a decision:

1. The turn path does not change. Nothing here runs inside a request.
2. Redaction is CloudWatch's job, not ours. Judges read masked text.
3. No prose leaves the log groups. Metrics are the product; explanations and Insights are debugging aids behind IAM.

## 4. Decisions

**D1 — Opt-in per environment, default off, and the flag is the privacy decision.** `onlineEvaluation.enabled` follows the `feedbackEvalSampling` pattern in `infrastructure/lib/config.ts`: only the literal `"true"` enables it, so a workflow forwarding an unset variable cannot turn it on. When off, the construct produces zero resources and the Runtime keeps its redacting defaults. Turning it on is the statement "this environment has decided masked conversation text may be written to CloudWatch and read by an AWS-managed judge". The spike's §2 asked for that decision to be explicit; this flag is where it is made.

**D2 — Content capture is unlocked on the Runtime, not in the image.** `Dockerfile.inference-api` keeps `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_unredacted_attributes=` (redact everything). When the flag is on, the inference construct sets `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_unredacted_attributes=gen_ai.*` in the Runtime's `environmentVariables`, which wins over the image ENV. `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` stays `false`: the spike found the content the judge needs arrives as `strands.telemetry.tracer` log records, and the botocore scope would only duplicate it. The Runtime environment has a 2,560-byte budget and a 50-variable cap (`runtime-environment-payload-guard.ts`, `test/runtime-env-var-limit.test.ts`); the worst-case config test must include this variable.

**D3 — PII masking is a CloudWatch data protection policy, applied to both log groups.** Managed identifiers: `Name`, `Address`, `EmailAddress`, `PhoneNumber`, `Ssn`, `DateOfBirth`, `DriversLicense`, `CreditCardNumber`, `BankAccountNumber`, `IpAddress`, plus the credentials set. Cost is $0.12 per GB scanned. The policy is created only when the flag is on, on the runtime log group by prefix (`runtimeLogGroupPrefix()` already exists for the retention sweep) and on the results log group by name. The `LogEventsWithFindings` metric it emits is free and goes on the dashboard, because a sudden drop in findings means masking stopped working.

**D4 — While the flag is on, the runtime log group retention is 7 days.** The judge scores a session about 15 minutes after it goes quiet; nothing needs the text after that. `runtime-log-retention-sweep-construct.ts` already stamps retention on these groups daily; this feature passes it 7 when enabled and the configured `observability.logRetentionDays` otherwise. 7 is a constant, not a tunable, because a longer window has no consumer.

**D5 — The evaluation execution role can read, and cannot unmask.** One IAM role, created by the construct: `logs:StartQuery`/`GetQueryResults`/`FilterLogEvents` on the runtime log group prefix, `logs:PutLogEvents` and `cloudwatch:PutMetricData` on the outputs, `bedrock:InvokeModel` on the custom evaluator's judge model. An explicit `Deny` on `logs:Unmask`. No role this stack creates is granted `logs:Unmask`; a human who needs the original text uses the account's break-glass path, and that is an audit event, not a feature.

**D6 — Insights is a separate flag, default off.** `onlineEvaluation.insightsEnabled` adds `Builtin.FailureAnalysis` to the evaluator list and enables the Agent Insight Store. It is split from scoring because its output is generated prose about real sessions (§2 row 4), which is a different comfort level from a number. Scoring and topic trends work without it. Nothing in the product reads Insights; it is a dev-team tool read through the console or CLI.

**D7 — Topic is a categorical dimension of one custom evaluator, not a second judge call.** A custom LLM-as-a-judge evaluator with two dimensions: `topic` (categorical, from the deployment's bucket list) and `answer_quality` (numerical 1–5, so it stays usable for A/B significance, which categorical scales cannot do). One judge call per sampled session returns both, so quality can be split by topic at no extra cost. The bucket list and rubric live in the repo as data (§6) and are overridable per deployment; AWS retired its own intent analysis on 2026-10-16, so there is no built-in alternative. A `other` bucket is mandatory and its share is a tracked metric: a growing `other` means the list is stale.

**D8 — No pooled score. Every chart carries its `n`.** Inherited from `response-feedback.md` §9 and `eval-harness.md` §1. The metrics namespace has per-evaluator, per-topic series. No construct, dashboard widget or admin endpoint computes a single platform-wide number, and a review that adds one is rejected on that ground. Small deployments produce noisy series; the docs say to raise the sampling rate rather than read a thin one.

**D9 — Dev first; prod is a separate yes.** The first PR enables this on dev only. Prod enablement is its own PR with its own reviewer, after the dev trial has produced a week of charts someone has looked at and the §2 table has been shown to whoever owns the privacy call. The flag makes this cheap; the spec makes it required.

**D10 — Fork-neutral.** Nothing names our deployment. Bucket list, sampling rate, retention and the Insights flag are config. The docs page is written for a deployer who has never seen our traffic. The default bucket list is generic (§6) and a fork is expected to replace it.

**D11 — Nothing here touches the turn path.** The only runtime effect is one environment variable that changes what telemetry already being emitted contains. TTFT, the cacheable prefix and `toolConfig` are unaffected by construction. Stated because the review checklist asks.

## 5. What AWS provides, verified

Read from the AWS docs on 2026-11-19 and the spike's live run on 2026-08-12.

- **Online evaluation config** (`AWS::BedrockAgentCore::OnlineEvaluationConfig`): watches CloudWatch log groups by exact name or by 1–5 name prefixes, identifies the agent by `serviceName` (`<runtime-name>.<endpoint>`), samples 0.01–100% of sessions (default 10%), treats a session as ended after 1–60 idle minutes (default 15), runs up to 25 evaluators, and writes to a results log group and a metrics namespace via `OutputConfig`. `ExecutionStatus` pauses and resumes it without deleting it.
- **Built-in evaluators** (16 verified `ACTIVE` in the spike): quality (Helpfulness, Correctness, Faithfulness, GoalSuccessRate, ResponseRelevance, Conciseness, Coherence, InstructionFollowing, Refusal), safety (Harmfulness, Stereotyping), trajectory and tool matchers, and `Builtin.FailureAnalysis` for Insights. Model cost is included in the evaluator price.
- **Custom evaluators**: `llmAsAJudge` with instructions, a numerical or categorical rating scale, a model config, and (new) multiple named dimensions per call. Locked while any enabled config uses them; clone to change.
- **Insights**: currently produces only `type: FAILURE`. Intent and trajectory insights existed in the retired recurring-report feature and are gone. Free during preview.
- **Pricing** ([AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)): built-in evaluators $2.40 per 1M input tokens and $12.00 per 1M output tokens; custom evaluators $1.50 per 1,000 evaluations plus the judge model's own Bedrock usage in our account; batch evaluation at a 25% discount. CloudWatch data protection $0.12 per GB scanned.
- **What it does not give**: topic discovery (D7), a scrubbing seam before the judge (hence D3), storage of only masked text (§2), and any guarantee about where a built-in evaluator runs (§11).

## 6. Architecture

```
infrastructure/
  lib/config.ts                              OnlineEvaluationConfig: enabled, samplingPercentage,
                                             insightsEnabled, topicBucketsPath (D1, D6, D7)
  lib/constructs/evaluation/
    online-evaluation-construct.ts           the config, execution role, results log group,
                                             metrics namespace, both data protection policies (D3, D5)
    topic-quality-evaluator-construct.ts     the custom multi-dimension evaluator (D7)
    default-topic-buckets.json               the generic bucket list a fork replaces (D10)
    topic-quality-rubric.md                  the judge instructions, loaded at synth
  lib/constructs/inference-api/
    inference-agentcore-construct.ts         +1 Runtime env var when enabled (D2)
    runtime-log-retention-sweep-construct.ts retention 7 when enabled (D4)
  lib/constructs/observability/
    platform-dashboard-construct.ts          a section, not a 4th dashboard (§7)
  test/online-evaluation.test.ts             §9
docs/
  specs/online-evaluation.md                 this file
  <feature-flags page>                       the deployer-facing entry
```

Default bucket list, deliberately generic:

```
writing_and_editing · research_and_explanation · data_and_code · document_qa ·
scheduling_and_admin · brainstorming · translation_and_language · platform_help · other
```

The rubric asks for exactly one bucket, names `other` as the answer when none fits, and scores `answer_quality` on whether the final response addressed what the user asked, with 1 for "did not" and 5 for "fully, with nothing wrong". It does not ask the judge to assess truth; built-in Faithfulness exists for that and is not in the default set because it needs reference context most sessions lack.

Default evaluator set when enabled: `Builtin.Helpfulness`, `Builtin.GoalSuccessRate`, the custom `topic_quality` evaluator. With `insightsEnabled`: plus `Builtin.FailureAnalysis`.

## 7. Dashboard and alarms

The stack holds exactly three CloudWatch dashboards, the free allowance (`observability.md` §8). This feature adds a **section to the platform dashboard**, not a fourth dashboard:

- `Builtin.Helpfulness` and `Builtin.GoalSuccessRate` averages per day, with sample count on the same axis.
- `answer_quality` per topic bucket, stacked.
- Topic bucket share per week, stacked to 100%, with `other` drawn first so its growth is visible.
- `LogEventsWithFindings` on the runtime log group (D3).

Alarms go through `AlarmFactory` like every other alarm. Two in phase 1: evaluation errors (the results log group's error count, `NOT_BREACHING` on missing data) and `LogEventsWithFindings` dropping to zero for 24 hours while sessions continue (masking stopped; `BREACHING` on missing data, because silence is the failure). No alarm on a score, because a judge score moving is a question to look at, not a page.

Phase 3 adds the same charts to `/admin/evaluations` next to the harness results, reading `GetMetricData` only (§2 row 5).

## 8. Cost

Per sampled session, order of magnitude:

| Item | Basis | Per session |
|---|---|---|
| Two built-in judges | ~5k–15k input tokens each at $2.40/1M, ~300 output at $12/1M | $0.03–$0.08 |
| Custom topic/quality evaluator | $1.50/1,000 + one Haiku-class call in our account | ~$0.01 |
| Data protection | every session's text, sampled or not, at $0.12/GB; a 15k-token session is ~60 KB | ~$0.00001 |

Round figure: **about $0.05–$0.10 per sampled session.** At 10% sampling, 1,000 sessions a day costs roughly $5–$10 a day. Masking is a rounding error; the judges are the bill. The sampling rate is the cost knob and is config. The run spends nothing until the flag is on.

## 9. Tests

- **Construct test**: with the flag off, synth contains no `OnlineEvaluationConfig`, no evaluator, no data protection policy and the Runtime environment carries no `OTEL_SEMCONV_STABILITY_OPT_IN`. With it on, all of them exist, the execution role's policy contains an explicit `Deny` on `logs:Unmask`, and both log groups carry a data protection policy. Verified by reading the synthesized template, not by compiling (the `cliAppClient` lesson in the TUI steering).
- **Ceilings**: `platform-ceilings.test.ts` already synthesizes with every opt-in flag on; this adds roughly ten resources and must stay under its margin.
- **Runtime payload**: `runtime-environment-payload-guard.test.ts` worst case includes the new variable.
- **No pooled score**: a source-level test under `infrastructure/test/` fails if any file under `lib/constructs/evaluation/` or the dashboard section defines a metric math expression that averages across evaluators or across buckets.
- **Phase 3 route**: an architecture test asserts the admin evaluations route imports no CloudWatch Logs client and no `bedrock-agentcore` Insights call.
- **Backend**: `tests/security/test_otel_content_capture.py` already pins both redaction switches to the library code; it gains a case that the Runtime override string is the one Strands' redaction parses as "exempt `gen_ai.*`".

## 10. Phasing

Each PR is independently shippable and each earns the next.

- **PR-1 — Spec, config, construct, dev trial.** `config.ts` block (five places: constant, interface, loader with the flat-dotted-context chain, `load-env.sh`, `platform.yml`), `online-evaluation-construct.ts` with the two built-in evaluators only, data protection policies, retention override, Runtime env override, construct tests. Enabled on dev at 10%. Deliverable: a week of metrics on dev and a screenshot of the masked log group in the spec's "Verified" section.
- **PR-2 — Topic and quality evaluator.** The custom multi-dimension evaluator, default bucket list, rubric, dashboard section with the stacked charts, the no-pooled-score test. Deliverable: the `other` share for dev, which decides whether the bucket list needs work before anyone else sees it.
- **PR-3 — Admin page and docs.** Charts on `/admin/evaluations`, the feature-flags docs entry, the "who can read what" table reproduced in deployer docs. Deliverable: a fork can turn it on from the docs alone.
- **PR-4 — Insights, opt-in.** `insightsEnabled`, `Builtin.FailureAnalysis`, the Insight Store with the stack KMS key, a short runbook for reading an insight. Deliverable: one real pattern found on dev that led to a fix.
- **Prod** is not a numbered PR. It is a one-line config change with the §2 table attached to the PR description and a named approver (D9).

## 11. Open questions

1. **`UNIFIED_TRACES_DESTINATION_ENABLED`.** The AWS docs say an online config matching log groups by prefix needs the Runtime to write spans to its own log group, not the shared `aws/spans`. The spike found our content records already land in the runtime group and spans in `aws/spans`. Whether the online evaluator needs both in one place is the first thing PR-1 finds out on dev. If it does, that is one more Runtime variable against the byte budget.
2. **Cross-region processing.** The Insights docs note that built-in evaluator requests "can be processed in another Region" depending on the source Region. For a university this is a data-residency question, not a technical one. Confirm the behaviour for `us-west-2` before prod, and record it in the §2 table's last row.
3. **Masking quality on JSON bodies.** The content records are JSON with messages as string values. Some managed identifiers depend on nearby keywords; a bare name inside a JSON string may be missed where the same name in prose is caught. PR-1 inspects a sample of masked records and reports the miss rate rather than assuming.
4. **Session boundaries.** The evaluator's "session" is the Runtime session, closed by the idle timeout. A conversation someone returns to over days will be scored once per quiet gap. That is probably right (each sitting is a unit of help) but changes how counts read; decide and document.
5. **Service name.** `<agent-runtime-name>.DEFAULT`, where the runtime name is `getResourceName(config, 'agentcore_runtime')` with hyphens replaced by underscores. Verify against a real span's `service.name` before hardcoding; the dashboard widgets fixed in #843 were broken by exactly this kind of name guess.
6. **Judge model for the custom evaluator.** Haiku-class by default for cost; the eval harness chose Sonnet as its judge and warns on self-judging. Whether a categorical topic label needs Sonnet is an empirical question PR-2 answers with a 50-session spot check.
