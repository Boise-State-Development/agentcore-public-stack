---
title: Environment Variables
description: Where configuration lives, and how a value reaches a running container.
sidebar:
  order: 1
---

Configuration reaches a running service through a fixed chain:

```
GitHub Actions variable  →  platform.yml workflow  →  scripts/common/load-env.sh  →
cdk synth/deploy (infrastructure/lib/config.ts)  →  container env var  →  backend reader
```

Almost every operator-facing knob is a `CDK_*` GitHub Actions variable (or the
matching `cdk.context.json` key). `config.ts` reads it at synth time, validates
it, and writes the plain env var (e.g. `SKILLS_ENABLED`, `CORS_ORIGINS`) onto the
Fargate task / AgentCore Runtime.

## Feature flags

Every on/off feature flag — what it does, its default on a fresh fork, and
whether it costs money — has its own page:

**→ [Feature Flags](/agentcore-public-stack/configuration/feature-flags/)**

## Other configuration

- **CORS origins** — see [CORS](/agentcore-public-stack/configuration/cors/).
- **Authentication** (Cognito, federated IdPs, `SKIP_AUTH` local-dev bypass) —
  see [Authentication](/agentcore-public-stack/configuration/authentication/).
- **AgentCore services** (Memory, Gateway, Code Interpreter, Browser) — see
  [AgentCore Services](/agentcore-public-stack/configuration/agentcore-services/).
- **Project memory content check** (`CDK_MEMORY_LINT_MODE`,
  `CDK_MEMORY_SENSITIVE_PATTERNS`) — see
  [Projects](/agentcore-public-stack/admin/projects/#memory-content-check).
- **Sizing, observability, and tuning knobs** — the authoritative list with
  defaults lives in
  [`infrastructure/lib/config.ts`](https://github.com/Boise-State-Development/agentcore-public-stack/blob/main/infrastructure/lib/config.ts).

## Derived resource names on the AgentCore Runtime

Most of the Runtime's variables used to be resource names that CDK builds from
the stack prefix (`{prefix}-sessions-metadata`, `{prefix}-rag-documents-{account}`).
The inference-api now derives those itself at process start from
`PROJECT_PREFIX` (plus `AWS_ACCOUNT_ID` for the account-scoped buckets), so the
Runtime does not have to be sent them. The authoritative list is
[`backend/src/apis/shared/config/derived_environment.json`](https://github.com/Boise-State-Development/agentcore-public-stack/blob/main/backend/src/apis/shared/config/derived_environment.json);
a CDK test checks every entry against the synthesized template.

Rules:

- **An explicit variable always wins.** Derivation only fills in what is
  absent, so a local `.env` or a deployment that keeps sending a name is
  unaffected.
- **Drift is logged, never guessed at.** When a variable is set *and* differs
  from the derived name, startup logs a `WARNING` naming it and keeps the
  explicit value.
- **app-api keeps every variable.** ECS has no payload limit; app-api only runs
  the comparison.

This is what lets a deployment fit the V2 Runtime's 2,560-byte environment
limit — see [AgentCore Runtime V2](/agentcore-public-stack/deployment/agentcore-runtime-v2/).

## Local development

For local backend/frontend runs, values come from `backend/src/.env` (see
`backend/src/.env.example`) and the frontend `environment.ts`, not from CDK. See
[Local Development](/agentcore-public-stack/local-development/).
