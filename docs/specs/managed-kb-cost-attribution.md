# Managed Knowledge Base cost attribution

How to find what Managed Knowledge Base actually costs, and the two ways of asking
that quietly return the wrong number.

Requirement 22.7 of `.kiro/specs/managed-kb-migration`. Figures are from
`docs/specs/bedrock-managed-kb-evaluation.md` §2 and §11.3, measured against
dev-ai — not estimates.

---

## The two wrong queries

**Keying on `AmazonBedrock` returns nothing at all.** Managed KB bills under the
service code **`AmazonBedrockAgentCore`**. Searching the Bedrock service code for
knowledge base SKUs returns an empty result, so a cost query filtered on
`AmazonBedrock` reports `$0.00` — and reports it *successfully*, which is worse
than an error. Nobody investigates a zero they asked for.

**Keying on the service code alone blends it into Runtime memory.** AgentCore's
bill is dominated by the Runtime memory line — the 2026-08 AICC report put it at
**73%** of the AgentCore total. A query grouped by service code therefore shows a
large, slowly-growing AgentCore number in which a knowledge base storage curve of
any plausible size is invisible.

**So: filter on `usagetype`.**

---

## The three SKUs

Exactly three per region, all `Consumption-based`, all `beginRange=0 →
endRange=Inf` — there is no tier-0 minimum block.

| usagetype (us-west-2) | rate |
|---|---|
| `USW2-Knowledge-Base:Consumption-based:Storage` | $5.00 / GB-month |
| `USW2-Knowledge-Base:Consumption-based:Retrieval` | $0.001 / query |
| `USW2-Knowledge-Base:Consumption-based:AgenticRetrieval` | $0.004 / query, **stacks on** Retrieval |

`AgenticRetrieval` should not appear on our bill: agentic retrieval is not enabled
on any path (Requirement 3.5), and its 60 RPM *account-wide* quota is why. A
non-zero value on that line means something turned it on.

Storage at $5.00/GB-month is roughly **35×** the current S3 Vectors cost of about
$0.15/GB-month. That ratio, not the absolute number, is what the per-owner byte cap
exists to bound.

## There is no idle floor

Of 6,467 AgentCore usagetypes, 6,124 contain `Hours` — Runtime carries
`Instance-based:<type>:Management-Hours`. **Zero of the 21 Knowledge-Base
usagetypes do.** AWS models hourly floors in this exact service code and
deliberately did not for knowledge bases. Three empty probe knowledge bases left
running for a month billed **$0.00000203** in total.

This is why lazy provisioning is safe and why an idle knowledge base is a storage
problem rather than a fixed cost.

---

## The query

```bash
aws ce get-cost-and-usage \
  --region us-east-1 \
  --time-period Start=2026-08-01,End=2026-09-01 \
  --granularity DAILY \
  --metrics UnblendedCost UsageQuantity \
  --filter '{"And":[
      {"Dimensions":{"Key":"SERVICE","Values":["Amazon Bedrock AgentCore"]}},
      {"Dimensions":{"Key":"USAGE_TYPE_GROUP","Values":[]}}
    ]}' \
  --group-by Type=DIMENSION,Key=USAGE_TYPE
```

Notes that cost time if you skip them:

- **Cost Explorer is `us-east-1` only.** The call fails elsewhere regardless of
  where the knowledge bases live.
- **Group by `USAGE_TYPE`, then read the `Knowledge-Base:` rows.** Grouping by
  `SERVICE` is the blended-into-Runtime-memory mistake above.
- **The usagetype carries a region prefix** (`USW2-`, `USE1-`). A filter written
  against one region silently returns nothing for another, and KB SKUs exist in
  seven regions only: USW2, USE1, EU, EUC1, EUW2, APN1, APS2.
- **Cost Explorer lags by up to 24 hours.** For "did we just spend something
  alarming", read the CloudWatch metrics below instead.

---

## What to watch instead of the bill

Cost Explorer answers "what did we spend". These answer "what are we about to
spend", which is the question worth alarming on. Both sets live in
`{projectPrefix}/ManagedKb`.

| Metric | Reads as |
|---|---|
| `KbStorageGB` | The storage curve. Multiply by $5.00 for a monthly run rate |
| `KbCount` | Progress toward the adjustable 10,000-knowledge-base account cap |
| `KbIdleGB` | Bytes nothing has needed for `KB_IDLE_THRESHOLD_DAYS`. **Baseline only in this phase** — nothing reclaims yet, and the follow-up spec's eviction threshold has to be chosen from this distribution, which cannot be backfilled |
| `KbByteCapRejected` | Whether the 100 MB default cap is actually workable before it hardens into policy |

`KbReclaimedGBPerDay` is deliberately **not** emitted. Nothing reclaims in this
phase, so it would be structurally zero — and a permanently-zero metric on a
dashboard teaches people to stop reading the dashboard.

### Reading Bedrock's own metrics

Per-knowledge-base `Invocations` lives in Bedrock's `AWS/Bedrock/KnowledgeBases`
namespace and is a cheaper idleness signal than anything we can compute. That
namespace is a **read source only**: CloudWatch reserves every namespace beginning
with `AWS` and rejects writes to them, so the platform's own metrics go to
`{projectPrefix}/ManagedKb` (Requirement 20.10) and reading Bedrock's requires
`cloudwatch:GetMetricData` / `GetMetricStatistics` (Requirement 20.13).

Conflating those two directions once produced a `PutMetricData` grant scoped to
`AWS/Bedrock/KnowledgeBases`, which would have deployed cleanly and published
nothing, forever.

---

## Tags are not enforcement

Knowledge bases are tagged at creation with `prefix`, `env`, `appKbId` and
`ownerUserId` (Requirement 20.11), and those tags are what make a tag-filtered
`ListKnowledgeBases` — and therefore the reconciler and teardown — possible at all.

They are **not** the byte cap. Cost-allocation tags take up to 24 hours to appear
and are not queryable synchronously, so nothing that has to refuse an upload can
depend on them (Requirement 12.8). `ownerUserId` is also deliberately an opaque
identifier and never an email address: anyone with `bedrock:ListKnowledgeBases` can
read a tag.
