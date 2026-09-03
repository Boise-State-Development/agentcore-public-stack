---
inclusion: fileMatch
fileMatchPattern: ["infrastructure/lib/constructs/observability/*", "infrastructure/test/observability-*"]
---

# Observability

Every CloudWatch alarm in `PlatformStack` publishes to one SNS topic. This doc
covers the rules that keep that true, the gotchas that silently break alarms, and
what to do when one fires.

## 0. The failure this system was built to prevent

Before this work the stack had 13 alarms and **not one of them notified anybody**.
Three separate constructs carried a comment saying so. Two of those alarms were
worse than silent: they watched metric names that exist in no CloudWatch
namespace at all, so they sat in `INSUFFICIENT_DATA` from the day they were
created — which an operator reads as *healthy*.

Both failures share a shape: **nothing errored.** The alarm deployed, evaluated,
and turned green. Every rule below exists because this domain fails quietly, and
quiet failure has to be caught by structure or by a test, never by remembering.

## 1. Never call `new cloudwatch.Alarm()` — use `AlarmFactory`

```typescript
const alarms = new AlarmFactory(this, config, props.alarmTopic);

alarms.alarm('MyAlarmLogicalId', {
  name: 'my-service-errors',          // NOT alarmName; prefix is applied for you
  alarmDescription: 'What broke, and what the first response should be',
  metric: someMetric,
  threshold: config.observability.someThreshold,
  evaluationPeriods: 2,
  comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
  treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
});
```

The factory attaches both `AlarmActions` and `OKActions` as a consequence of
being used at all. `new cloudwatch.Alarm()` produces a console-only alarm that
looks completely finished, which is why the rule is enforced by a source-level
test rather than convention:

- `observability-alarm-routing.test.ts` fails if any file under `lib/` calls the
  constructor directly (except the factory itself), and fails if any alarm in the
  synthesized template lacks actions.

Use `expressionAlarm()` for metric math so the routing guarantee survives.

## 2. No `config.production` in observability code

This repo is forked by many institutions. A fork with one environment should not
have to reason about a `production` boolean, and a fork with three should not be
limited to two. Every tunable is a **single scalar** in `ObservabilityConfig`,
and per-environment differences live in the forker's own deployment config
(GitHub Variables scoped to a GitHub Environment) — not in a ternary here.

Enforced: `observability-alarm-routing.test.ts` fails on any `config.production`
under `lib/constructs/observability/`.

Adding a tunable means touching five places. Miss the third and the flag is
accepted then silently ignored:

1. `OBSERVABILITY_DEFAULT_*` constant in `config.ts`, with the reasoning inline
2. field on `ObservabilityConfig`
3. loader entry using the full precedence chain (see §3)
4. `scripts/common/load-env.sh` → `build_cdk_context_params()`
5. job-level `env:` in `.github/workflows/platform.yml`

Defaults are **cost-conscious**: they are what a fork inherits when it configures
nothing, so they are the cheapest setting that still leaves alerting useful.
Diagnostic depth is opt-in. The X-Ray sampling default is the clearest case — it
was `1.0` for any fork that never set `production`, meaning a recorded trace for
every single agent invocation at $5/million.

## 3. The flat dotted context key (this has bitten the repo three times)

`--context observability.logRetentionDays=90` sets the **flat** key
`context['observability.logRetentionDays']`. It does **not** build a nested
object. Reading only the nested form accepts the operator's flag and ignores it.

```typescript
logRetentionDays:
  parseIntEnv(process.env.CDK_OBSERVABILITY_LOG_RETENTION_DAYS)
  ?? parseIntEnv(scope.node.tryGetContext('observability.logRetentionDays'))  // ← flat
  ?? scope.node.tryGetContext('observability')?.logRetentionDays              // ← nested
  ?? OBSERVABILITY_DEFAULT_LOG_RETENTION_DAYS,
```

Use `parseFloatEnv` for fractional values. `parseIntEnv('0.05')` is `0`, which
would switch X-Ray sampling off entirely rather than setting it to 5%.

## 4. Gotchas that produce silently-broken alarms

### The SNS topic must use a customer-managed KMS key

CloudWatch **cannot** publish to a topic encrypted with the AWS-managed
`alias/aws/sns` key: the publish is made by the `cloudwatch.amazonaws.com`
service principal, and an AWS-managed key's policy cannot be edited to grant it
`kms:GenerateDataKey*`. The alarm goes to ALARM, the console shows it firing, and
the notification is dropped.

`kms:Decrypt` alone is **not enough** — SNS envelope encryption has the
*publisher* generate the data key, so `GenerateDataKey*` is required too.

### Dimensions must come from real resources

A `CPUUtilization` alarm with no dimensions is a valid CloudWatch alarm that
silently averages every ECS service in the account. Same for ALB metrics. Always
derive dimensions from the CDK resource (`service.metricCpuUtilization()`,
`targetGroup.metrics.*`, `table.metric()`), never from a name string.

### Metric-math alarms cap at 10 metrics

CloudWatch rejects an alarm whose math expression contains more than 10
individual metrics. CDK's `table.metricSystemErrorsForOperations()` defaults to
**all 14** DynamoDB operations and throws `TooManyMetricsInMathExpression` at
synth. Pass an explicit operations list.

Also: CDK deprecates `metricThrottledRequests()` and `metricSystemErrors()` as
returning invalid metrics. Use `table.metric('ReadThrottleEvents')` etc.

### Don't pass `label` to a metric used in an alarm

It forces CDK to render the alarm as a `Metrics[]` array instead of flat
`Namespace`/`MetricName`/`ExtendedStatistic` properties. CloudWatch labels
percentile series adequately on its own.

### Units differ between services

| Metric | Unit | Threshold handling |
|---|---|---|
| AgentCore `Latency` | **Milliseconds** | use `agentCoreLatencyMs` directly |
| ALB `TargetResponseTime` | **Seconds** | divide by 1000 |

Both verified with `get-metric-statistics`. Getting this wrong is a 1000x error
in either direction, and neither direction fails loudly.

## 5. Streaming makes latency a weak signal

The chat path is SSE. The ALB does not consider a request complete until the
stream closes, so `TargetResponseTime` and AgentCore `Latency` are legitimately
tens of seconds for a healthy turn — and a sudden *drop* can mean turns are
failing early.

Measured over 14 days in dev: average turn **3.0–4.5s**, daily maxima **16.7s,
16.9s, 24.4s**. The original alarm threshold was 30s, i.e. *below* the observed
maximum, so a healthy long turn could trip it. Latency floors default to 120s.

Reliable signals on this path are the discrete ones: 5xx counts, unhealthy hosts,
rejected connections, throttles.

## 6. `treatMissingData` is a per-metric decision

Never defaulted by the factory, because both answers are correct somewhere:

- **`NOT_BREACHING`** for error and throttle counts. A service that is not
  failing publishes nothing, so absent data is the healthy state.
- **`BREACHING`** for `UnHealthyHostCount` and `RunningTaskCount`. These stop
  being published when the service is at zero or deleted. `NOT_BREACHING` would
  leave the alarm silent during a total outage — the exact case it exists for.

## 7. Verify metrics exist before alarming on them

Documentation is not sufficient evidence that a metric is published, and an alarm
on a non-existent metric is indistinguishable from a healthy one.

```bash
aws cloudwatch list-metrics --namespace "AWS/Bedrock-AgentCore" \
  | jq -r '.Metrics | group_by(.MetricName) | .[] |
      "\(.[0].MetricName) dims=\(map(.Dimensions|map(.Name)|sort)|unique|tostring)"'
```

Known facts from that sweep, all pinned by tests:

- Runtime metrics live in `AWS/Bedrock-AgentCore` (hyphenated), dimensioned
  `Resource` + `Operation=InvokeAgentRuntime` + `Name={runtimeName}::DEFAULT`.
  The lowercase `bedrock-agentcore` namespace is real but holds only the
  OpenTelemetry/Strands *application* metrics.
- **Memory and Gateway publish `Resource` as a full ARN; Code Interpreter
  publishes a bare ID.** Passing an ARN for Code Interpreter yields an alarm that
  matches nothing.
- `AWS/Cognito` on the **ESSENTIALS** feature plan publishes *only* success
  metrics. There is no sign-in failure or throttle metric to alarm on — failure
  and threat metrics require the **Plus** plan. The auth-path failure signal is
  the token-enrichment Lambda's `Errors` metric instead.
- AgentCore Browser has zero metric streams (provisioned, unused).

A metric absent from `list-metrics` may simply never have fired. Alarming on it
is still correct — `NOT_BREACHING` keeps it quiet until the first occurrence.

## 8. Resource budget

CloudFormation caps a stack at **500 resources**, and this is a deliberate
single-stack architecture with nowhere to spill. Alarms are the largest
discretionary consumer.

Measured: **308** resources before this work, **~370** after. A guard in
`observability-dynamodb-alarms.test.ts` fails the build above 460, so the ceiling
surfaces while there is still room to react rather than as a failed deploy.

When budget matters, decide with data rather than by covering every documented
metric. The DynamoDB allocation was cut from 78 alarms to 27 because a live sweep
showed `ReadThrottleEvents`, `WriteThrottleEvents` and `SystemErrors` had **zero
streams** — all tables are on-demand, so throttling had never occurred — while
account-level `UserErrors` had real data nobody was watching.

Dashboards: the first **3 are free**, then $3/month each. The stack is at exactly
3, which is why the platform dashboard links to the other two instead of
restating their widgets.

## 9. Log retention

One value, `observability.logRetentionDays`, applied through
`logRetentionFor(config)`. A source guard fails the build if any construct
hardcodes `retention: logs.RetentionDays.*`.

The AgentCore Runtime's log group is created by the **AgentCore service**, not
CloudFormation, so a CDK `LogGroup` cannot set its retention — declaring one
would collide on create or manage a second, empty group. An `AwsCustomResource`
calls `logs:PutRetentionPolicy` instead. That API is idempotent *and* creates the
group if absent, which matters on a first deploy when the runtime exists but has
never been invoked. There is deliberately **no `onDelete`**: removing the
retention policy on teardown would revert the group to "keep forever", which is
the cost problem it fixes.

## 10. Subscriptions are not infrastructure-as-code

The topic is created by CDK; **subscribers are not**. Several teams need to hear
about failures and their membership changes far more often than the
infrastructure does. Requiring a PR, a review, and a CloudFormation deploy to add
one address is how a notification list goes stale and stops being trusted.

```bash
TOPIC=$(aws ssm get-parameter --name "/${PREFIX}/observability/alarm-topic-arn" \
  --query Parameter.Value --output text)
aws sns subscribe --topic-arn "$TOPIC" \
  --protocol email --notification-endpoint team@example.edu
```

A test asserts zero `AWS::SNS::Subscription` resources exist, so this decision
cannot be quietly reversed.

## 11. Runbook — first response by alarm

| Alarm | What it means | First action |
|---|---|---|
| `alb-unhealthy-hosts` | Targets failing health checks, **or none reporting** | `aws ecs describe-services` — are tasks running at all? Then app-api logs. |
| `alb-elb-5xx` | The load balancer itself could not serve | Almost always no healthy target. Check the alarm above first. |
| `alb-target-5xx` | App is reachable and erroring | app-api logs. This is application code. |
| `alb-rejected-connections` | ALB connection limit hit | Users were turned away *before* reaching the app, so nothing is in app logs. Check request volume. |
| `app-api-running-tasks-low` | Fewer tasks than desired | Task failing to start: check stopped-task reason, image pull, subnet IPs. |
| `app-api-memory-high` | Sustained memory pressure | Fargate **kills** a task that exhausts memory. Raise memory or find the leak. |
| `agentcore-system-errors` | AgentCore's fault | Escalate to AWS. Not application code. |
| `agentcore-high-error-rate` | `UserErrors` — our requests are malformed | Recent inference-api deploy? Check payload shape and IAM. |
| `agentcore-throttles` | At the TPS or session quota | Request a quota increase. Will not self-resolve. |
| `agentcore-high-latency` | p99 above 120s | Genuinely hung, not merely slow — 24s is a normal maximum here. |
| `bedrock-tpm-quota-usage` | **Leading** indicator | Request a quota increase *now*, before throttling starts. |
| `bedrock-invocation-throttles` | At a model's TPM/RPM quota | Users see chats that never respond. Quota increase. |
| `agentcore-memory-*` | Memory hot path failing | Users experience an agent that has forgotten the conversation. |
| `agentcore-gateway-*` | MCP calls failing at the gateway | Agents lose tool access. Check gateway targets. |
| `ddb-*-throttle` | Named table throttling | On-demand, so this is a hot partition or an account limit. Compare `ReadThrottleEvents` vs `WriteThrottleEvents` on that table. |
| `ddb-user-errors` | DynamoDB rejecting our requests (4xx) | Application code misusing the API. Account-wide, so use CloudTrail or app logs to find the caller. |
| `lambda-token-enrichment-errors` | **Silent** degradation | Handler is fail-open: logins still work, but MCP tools are losing user-identity claims. |
| `dlq-kb-ingestion-not-empty` | Work accepted then failed every retry | Will **not** self-clear. Inspect, fix, then replay or drain. |
| `prompt-cache-session-partial-miss` | One conversation re-writing its prefix every turn | Use the "Sessions by partial-miss waste" widget to find which session. |

Start at the **`{prefix}-platform-health`** dashboard: row 1 says whether traffic
is being served, row 2 says why, row 3 shows every alarm's current state.

## 12. Boise State's own profile

The committed defaults are what a **fork** should inherit. Boise State authors
this platform and does far more diagnostic work than any deployer of it, so our
values differ — and they live in GitHub Variables scoped to a GitHub Environment,
never in committed code. That separation is what lets both be right at once.

`scripts/observability/set-bsu-overrides.sh --env <development|production>`
applies them. It is **not run by CI**, makes no AWS changes, and requires
confirmation, because it mutates shared repository configuration. Add `--dry-run`
to print the plan (no `gh` auth needed).

| Field | OSS default | BSU dev | BSU prod |
|---|---|---|---|
| `xraySamplingRate` | `0.01` | `0.5` | `0.1` |
| `xraySamplingReservoir` | `1` | `5` | `2` |
| `xrayInsightsNotifications` | `false` | `true` | `true` |
| `agentCoreApplicationLogsEnabled` | `false` | `true` | **`false`** |
| `logRetentionDays` | `30` | `14` | `90` |
| `agentCoreErrorThreshold` | `10` | `5` | `5` |
| `lambdaErrorThreshold` | `5` | `1` | `3` |
| `albTarget5xxThreshold` | `10` | `5` | `5` |
| `dynamoThrottleThreshold` | `10` | `1` | default |
| `ecsCpuPercent` / `ecsMemoryPercent` | `80` / `85` | default | `75` / `80` |
| `promptCacheAvoidableMissThreshold` | `10` | `5` | default |
| `promptCacheWastedUsdThreshold` | `1` | `0.5` | default |

Two choices worth understanding rather than copying:

- **Dev traces at 50%, prod at 10%.** Not a mistake. X-Ray bills per trace
  recorded, and prod traffic is orders of magnitude larger, so 10% of prod is far
  more traces than 50% of dev. Dev is where we debug; prod is where we pay.
- **`agentCoreApplicationLogsEnabled` stays OFF in production.** Those records
  carry every user's prompt and the model's response verbatim — the
  highest-volume log source available and a genuine PII surface. Enable it
  temporarily for a specific investigation, then turn it back off.

Retention inverts between the two for the same reason latency floors are high:
dev keeps 14 days because dev noise is not worth a month, prod keeps 90 because a
real incident review reaches back weeks.

**Verifying a variable took effect.** The synth log prints a line beginning
`Observability:` with the *resolved* values. If it disagrees with the variable you
set, the value is not reaching `--context` — check the deploy job's **job-level**
`env:` block, since `vars.*` in a workflow-level `env:` silently resolves to an
empty string.
