---
title: Environments
description: Run more than one deployment from a single fork.
sidebar:
  order: 6
---

A single fork can deploy more than one environment — typically a development
stack and a production stack — by binding each to a **GitHub Environment** with
its own set of variables and secrets. The workflows are identical; only the
values they read differ.

## GitHub Environments

Each deployable environment maps to a GitHub Environment under **Settings →
Environments**. Variables and secrets defined on an environment override the
repository-level defaults when a workflow runs against it, so the same
`CDK_PROJECT_PREFIX`, `CDK_DOMAIN_NAME`, and certificate ARNs resolve to
different values per environment.

:::note
When a deploy workflow is triggered manually, it defaults to the **production**
environment. Select the target environment explicitly when running against a
non-production stack.
:::

## Dev vs prod domains

Keep each environment on its own domain so they never share state or DNS. The
reference deployment runs two:

| Environment | Branch | Domain |
|-------------|--------|--------|
| **Development** | `develop` | `dev.boisestate.ai` |
| **Production** | `main` | `boisestate.ai` |

Development is a **subdomain**, so the TLS wildcard-depth rule applies: a
`*.boisestate.ai` cert does **not** cover `artifacts.dev.boisestate.ai`, so the
dev environment needs a `us-east-1` cert covering its own `dev.boisestate.ai`
**and** `*.dev.boisestate.ai` (which covers `artifacts.dev.boisestate.ai` and
`mcp-sandbox.dev.boisestate.ai`).

Production serves at the **apex** `boisestate.ai`. The SPA sits on the bare
apex, which a wildcard does **not** match, while the `artifacts.` and
`mcp-sandbox.` origins are single-label subdomains that a wildcard does cover —
so the prod cert needs **both** `boisestate.ai` and `*.boisestate.ai` as SANs.
See the
[wildcard-depth note](/agentcore-public-stack/deployment/platform-cdk/#acm-certificates)
on the Platform page.

## Cross-account hosted zones

CDK normally creates the Route53 ALIAS/A records for the SPA, ALB, artifacts,
and mcp-sandbox origins. It does this with `HostedZone.fromLookup`, which runs
under the **deploy account's** credentials — so it only works when the hosted
zone lives in the same account as the deployment.

When the zone for an environment's domain lives in a **different AWS account**
(as the production `boisestate.ai` zone does), set `CDK_MANAGE_DNS_RECORDS=false`
on that GitHub Environment. The stack then:

- still attaches the custom domain + ACM cert to every CloudFront origin and the
  ALB listener, so each origin serves its domain over TLS, and
- skips the in-account zone lookup + record creation that would otherwise fail
  cross-account, and
- emits CfnOutputs with the record name and alias target for each origin so you
  can create the records by hand in the zone's account:

| Origin | Record-name output | Alias-target output |
|--------|--------------------|---------------------|
| SPA | `{prefix}-frontend-dns-record-name` | `{prefix}-frontend-dns-alias-target` |
| ALB | `{prefix}-alb-dns-record-name` | `{prefix}-alb-dns-alias-target` |
| Artifacts | `{prefix}-artifacts-dns-record-name` | `{prefix}-artifacts-dns-alias-target` |
| MCP sandbox | `{prefix}-mcp-sandbox-dns-record-name` | `{prefix}-mcp-sandbox-dns-alias-target` |

In each pair, create an ALIAS (or CNAME) at the record name pointing to the
alias target. The CloudFront origins target a `*.cloudfront.net` domain; the ALB
record targets the ALB DNS name.

:::note[Certs still live in the deploy account]
ACM certificates are referenced by ARN and must exist in the **deploy** account
(CloudFront certs in `us-east-1`, the ALB cert in the deploy region) regardless
of where the zone lives. Validate them with DNS validation records you add by
hand in the zone's account.
:::

Leave `CDK_MANAGE_DNS_RECORDS` unset (defaults to `true`) for any environment
whose hosted zone is in the deploy account — e.g. the dev stack — and CDK manages
the records automatically.

## Per-environment overrides

Beyond the required variables, most tuning knobs are optional and naturally
differ between a dev and a prod stack:

| Concern | Variables |
|---------|-----------|
| **ECS / Runtime sizing** | `CDK_APP_API_CPU`, `CDK_APP_API_MEMORY`, `CDK_APP_API_DESIRED_COUNT`, `CDK_APP_API_MAX_CAPACITY`, and the matching `CDK_INFERENCE_API_*` |
| **CloudFront** | `CDK_FRONTEND_CLOUDFRONT_PRICE_CLASS` (`PriceClass_100` / `200` / `All`) |
| **CORS** | `CDK_CORS_ORIGINS` and per-module overrides — add `http://localhost:4200` to point a local SPA at a deployed environment |
| **Frame ancestors** | `CDK_ARTIFACTS_EXTRA_FRAME_ANCESTORS`, `CDK_MCP_SANDBOX_EXTRA_FRAME_ANCESTORS` — leave unset in production |
| **Networking** | `CDK_VPC_CIDR` |
| **Retention** | `CDK_RETAIN_DATA_ON_DELETE`, `CDK_ARTIFACTS_RETENTION_DAYS` |

:::caution[Extra frame ancestors are a real loosening]
`CDK_ARTIFACTS_EXTRA_FRAME_ANCESTORS` and `CDK_MCP_SANDBOX_EXTRA_FRAME_ANCESTORS`
let additional origins embed your users' artifacts and MCP Apps. They're handy
for pointing a local SPA at a shared dev stack, but every listed origin can frame
that content — leave them unset on production.
:::

## Data retention on teardown

`CDK_RETAIN_DATA_ON_DELETE` controls what happens to stateful resources when the
stack is deleted. With it set to `true`, CloudFormation **retains** DynamoDB
tables, S3 buckets, Cognito, secrets, and KMS keys instead of deleting them —
which protects production data but means a later redeploy must reconcile those
retained resources (see
[Upgrading from Multi-Stack](/agentcore-public-stack/deployment/upgrade/)). On a
disposable dev stack, leaving it `false` lets a teardown clean up completely.

## Full configuration reference

The variables above are a curated subset. For every available variable and
secret — type, default, and the subsystem it tunes — see the
[GitHub Actions Configuration Reference](https://github.com/Boise-State-Development/agentcore-public-stack/blob/main/.github/ACTIONS-REFERENCE.md),
and the [Configuration](/agentcore-public-stack/configuration/environment-variables/)
section for how these map onto runtime behavior.
