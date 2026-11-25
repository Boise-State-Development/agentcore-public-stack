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

## Native-app sign-in

| GitHub variable | Container variable | Default | What it does |
|---|---|---|---|
| `CDK_NATIVE_AUTH_REDIRECT_URIS` | `NATIVE_AUTH_REDIRECT_URIS` (app-api) | empty | Comma-separated redirect URIs your own build of the iOS companion app ends its sign-in on, e.g. `edu.example.agentcore://auth/callback` (the app's bundle id as its scheme). Each is matched **exactly**: no wildcards, no normalisation, and synth refuses an entry that is not an absolute `scheme://…` URI. Empty means no native sign-in: `GET /auth/login?client=native` and `POST /auth/native/exchange` answer 404. There is no separate on/off flag; the list is the switch. |

These are not Cognito callback URLs. Cognito still returns to app-api's own
`/auth/callback`, which then hands the app a single-use, 120-second handoff
code on the listed URI; the app trades it for a session at
`/auth/native/exchange` with the PKCE verifier only it holds. A native session
does not slide on idle: it lasts 30 days from sign-in
(`BFF_SESSION_ABSOLUTE_LIFETIME_SECONDS`) unless the user signs out, which also
revokes its Cognito refresh token. Design:
[`docs/specs/native-app-auth.md`](https://github.com/Boise-State-Development/agentcore-public-stack/blob/main/docs/specs/native-app-auth.md).

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
