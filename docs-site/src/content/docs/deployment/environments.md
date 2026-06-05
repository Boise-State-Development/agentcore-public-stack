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
| **Development** | `develop` | `alpha.boisestate.ai` |
| **Production** | `main` | `beta.boisestate.ai` |

Both are **subdomains**, which means the TLS wildcard-depth rule applies: a
`*.boisestate.ai` cert does **not** cover `artifacts.alpha.boisestate.ai`, so
each environment needs a `us-east-1` cert that covers its own
`artifacts.{domain}` and `mcp-sandbox.{domain}` origins. See the
[wildcard-depth note](/agentcore-public-stack/deployment/platform-cdk/#acm-certificates)
on the Platform page.

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
