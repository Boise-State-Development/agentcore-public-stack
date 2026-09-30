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
- **Sizing, observability, and tuning knobs** — the authoritative list with
  defaults lives in
  [`infrastructure/lib/config.ts`](https://github.com/Boise-State-Development/agentcore-public-stack/blob/main/infrastructure/lib/config.ts).

## Local development

For local backend/frontend runs, values come from `backend/src/.env` (see
`backend/src/.env.example`) and the frontend `environment.ts`, not from CDK. See
[Local Development](/agentcore-public-stack/local-development/).
