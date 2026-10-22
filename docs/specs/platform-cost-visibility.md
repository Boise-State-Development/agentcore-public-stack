# Platform cost visibility

Brings the AWS infrastructure bill into the admin cost dashboard, alongside the
per-user inference ledger it already had.

## Why

The dashboard measured Bedrock token spend, and measured it well. Against
prod's September 1–21 bill our ledger came within **0.50%** of Cost Explorer's
model SKUs ($1,076.03 vs $1,081.44). What it could not see was anything that
is not a token:

| Prod, Sept 1–21 2026 | Amount | Share |
|---|---|---|
| Inference (Bedrock model SKUs) | $1,081.44 | 61.2% |
| **Infrastructure** | **$685.96** | **38.8%** |
| **Total** | **$1,767.41** | |

`Amazon Bedrock AgentCore` alone was **$201.54/month** — the second-largest
line item in the account, on no screen anywhere. (It is also the line that
would have surfaced the idle-reaper regression, #827.)

With 1,807 active users that month:

| Basis | Per user / month |
|---|---|
| Inference only (what the card used to show) | $0.60 |
| All-in | $0.98 |

The old "Avg Cost/User" understated true unit cost by **1.64×**.

Dev is the mirror image: August was **$736.32** of which only **$13.56** was
inference. Dev is ~98% fixed infrastructure, a standing cost unrelated to
usage.

## How it works

```
EventBridge (daily 07:10 UTC)
  └─> platform-cost-sync Lambda      infrastructure/lambda-assets/platform-cost-sync/
        ├─ 1 Cost Explorer call per period (current month + previous)
        └─ writes system-cost-rollup:  PLATFORM#MONTHLY          / SK=<YYYY-MM>
                                       PLATFORM#SERVICE#<YYYY-MM> / SK=<service>
                                                │
  GET /admin/costs/platform  ────────────────────┘  (app-api, reads DynamoDB only)
        └─> "Platform" tab + the all-in Total Cost / Avg Cost/User cards
```

**Scheduled, never live.** Cost Explorer bills **$0.01 per request**. A screen
that queried it on load would cost ~$10/month per thousand page loads to
display costs. `ce:GetCostAndUsage` is granted to the Lambda role and
deliberately **not** to app-api's, so no request path can spend billing
dollars however often it is called. Steady-state cost is ~$0.02/day.

**No new infra.** Platform rows use their own `PLATFORM#*` PK namespace in the
existing `system-cost-rollup` table — no new table, no new GSI, no migration.

**Two sources, never added twice.** `inferenceCost` comes from our ledger
(per-user and per-session, where Cost Explorer is per-account only);
`platformCost` comes from Cost Explorer. CE's own inference figure rides along
as `ceInferenceCost` **only** to reconcile the two — adding it to a total
would count every token twice. The Platform tab surfaces that comparison as a
**pricing regression test**: a widening delta means the rates in
`curated-models.ts` have drifted from what AWS actually charged, which
CLAUDE.md names as a live risk.

## Enabling it

Opt-in, against this repo's usual default-on-with-a-kill-switch posture —
this one reads the account's billing data, needs an IAM action an SCP may
deny, against a Cost Explorer that may not be enabled, and costs money per
call. Only the literal `true` enables; an unset workflow variable (which
arrives as an empty string) leaves it off.

```bash
CDK_PLATFORM_COSTS_ENABLED=true   # platform.yml (CDK) deploy
```

While off, the construct produces zero resources and the dashboard reports
inference cost only — the Platform tab explains that rather than rendering a
`$0.00` that would read as "the infrastructure is free".

To backfill history after enabling, invoke the function directly:

```bash
aws lambda invoke --function-name <prefix>-platform-cost-sync \
  --payload '{"periods":["2026-08","2026-07","2026-06"]}' /dev/stdout
```

## Attribution: this deployment, not the account

**An account is not an application.** This stack is open source, so a deployer
may well share an account with other workloads — and ours does. dev-ai hosts
**five** deployments of this stack (`beta-boisestateai-dev`,
`boisestateai-v2-dev`, `bsu-agentcore`, `dev-boisestateai-v2`, `nightly-mv`)
plus unrelated apps, and both accounts run a `bsu-*-backend` Aurora cluster we
do not provision. prod-ai happens to hold exactly one deployment today, but
that is luck, not design, and no fork should rely on it.

So the sync asks for **this deployment's own resources first**, filtering on
the `Project` tag `applyStandardTags` already writes. Its value is the stack's
`projectPrefix`, so there is nothing to configure and a fork inherits it.

`scope` on the summary records which answer the deployer actually got:

| `scope` | Meaning |
|---|---|
| `deployment` | The tag filter returned data. Figures cover **this stack**. |
| `account` | It did not, so this is the **whole account** — a ceiling, not an attribution. The UI says so, prominently. |

### Why the fallback exists

Cost Explorer will not group or filter by a cost allocation tag until that tag
is **activated in the payer account**:

- Activation happens in the payer account. An Organizations member account
  (dev-ai) gets `AccessDeniedException` merely *listing* the tags.
- It is **not retroactive** — months before activation stay account-wide.
- An inactive tag is **not an error**. Verified against prod: a filtered query
  returns `HTTP 200`, zero groups, `$0.00`. That empty result is exactly what
  `resolve_scoped_costs()` probes for — we deliberately do *not* call
  `ListCostAllocationTags`, since that is the call a linked account is denied,
  and a linked account is the topology most likely to need the fallback.

A fork that has not activated the tag — or cannot — still gets a working,
clearly-labelled dashboard instead of a blank one.

> ⚠️ **ECS Fargate bills per TASK, and tasks do not inherit a service's tags**
> without `propagateTags: SERVICE`. The live dev service reported `NONE`, which
> would have dropped **~17% of prod's infrastructure** (~$118/month) out of the
> tagged scope while everything still looked healthy. Set in
> `app-api-service-construct.ts`. If a tagged total looks low, check task tags
> before suspecting the filter.

### Activation checklist

1. Billing → **Cost allocation tags** in the **payer** account.
2. Activate `Project`. (Standalone accounts self-serve; an org member needs the
   management account.)
3. Wait up to 24h for the first data, then let the nightly sync run. `scope`
   flips to `deployment` on its own — no redeploy.

Tag coverage was audited across 1,519 dev resources: ECR/ELB/Cognito/SQS 100%,
DynamoDB 98%, KMS 96%, CloudWatch 91% — and the two largest lines specifically
confirmed (our NAT gateway and our AgentCore runtime both carry
`Project=dev-boisestateai-v2`, while other stacks' carry theirs or none).

### Service buckets

Within whichever scope applies, services are bucketed three ways:

| Bucket | Contents |
|---|---|
| `inference` | `… (Amazon Bedrock Edition)` model SKUs. Reconciliation only. |
| `excluded` | Another team's resources (RDS), plus account-level charges no single application causes: Support (a % of spend), the Control Tower baseline (Config, CloudTrail, Security Hub, GuardDuty), and Cost Explorer itself. |
| `platform` | Everything else. The number the dashboard adds. |

`excluded` rows are **persisted and displayed**, not silently dropped: an
operator can only trust a total if they can see what was held out of it. Under
`deployment` scope most exclusions simply never appear — another team's
database was never ours — and what remains is account-level charges the tag
cannot reach.

## Gotchas worth keeping

- **`PLATFORM_COST_EXCLUDED_SERVICES` overrides, and an empty value is
  ignored.** The default list lives in the handler; the env var is not set by
  CDK at all. An unset variable silently emptying a load-bearing list is how
  the browser URL blocklist broke, and here it would bill another team's
  database to our users with nothing but a slightly high total to show for it.
- **A month keeps settling after it ends**, so the sync always re-reads the
  previous month too. Syncing only the current month would freeze every
  December at whatever it looked like on the 1st.
- **CE rejects a future end date.** An in-progress month ends "tomorrow" and
  is flagged `partialMonth`, which the UI renders as a *Month to date* badge.
- **Costs persist as `Decimal`.** DynamoDB rejects floats, and that failure
  only shows up in cloud.
- **An empty `PLATFORM_COST_PROJECT_TAG` disables scoping; it never filters on
  `Project=''`.** Filtering on an empty value matches nothing, and the fallback
  would mask it — reporting the account total as though it were scoped.
- **Rows synced before scoping existed carry no `scope`, and default to
  `account`.** The pessimistic default is deliberate: defaulting to
  `deployment` would relabel an account-wide figure as this app's cost with
  nothing to reveal the error.
