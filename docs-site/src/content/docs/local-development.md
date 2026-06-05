---
title: Local Development
description: Run the backend and SPA on your machine against a deployed environment.
---

Local development means running the services on your machine — the App API on
`:8000`, the Inference API on `:8001`, and the Angular SPA on `:4200` — while
they talk to **real AWS resources** in a deployed environment. There is no
offline mode.

:::caution[A deployed environment is a prerequisite]
You cannot run this stack in isolation. The backend depends on DynamoDB,
Cognito, Bedrock, AgentCore (Memory, Code Interpreter), S3, KMS, and Secrets
Manager. Its required configuration — table names, Cognito IDs, KMS and Secrets
Manager ARNs, the AgentCore Memory ID — only exists once a `PlatformStack` is
deployed. Stand one up first (see
[Deployment](/agentcore-public-stack/deployment/overview/)), or get credentials
for a shared development environment, before continuing.
:::

## What runs where

| Service | Command | Port | Talks to |
|---------|---------|------|----------|
| **App API** | `uv run python main.py` | `:8000` | Deployed DynamoDB, Cognito, S3, Secrets Manager |
| **Inference API** | `uv run python main.py` | `:8001` | Deployed Bedrock, AgentCore Memory + tools |
| **Angular SPA** | `npm start` | `:4200` | The local App API at `:8000` |

You need Python with [uv](https://docs.astral.sh/uv/), Node.js, and AWS
credentials for an account where a stack is deployed (with read access to its SSM
parameters).

## Backend

Install dependencies and copy the environment template:

```bash
cd backend
uv sync --extra agentcore --extra dev
cp src/.env.example src/.env
```

Then fill in `src/.env`. The required values come from your deployed stack's SSM
parameters — pull them with the CLI rather than hand-copying from the console:

```bash
aws ssm get-parameters-by-path --path "/<your-prefix>/" --recursive \
  --query 'Parameters[].[Name,Value]' --output text
```

Each variable in `src/.env.example` documents where its value comes from.
`backend/README.md` and the
[Configuration](/agentcore-public-stack/configuration/environment-variables/)
section cover the full catalog. Make sure your AWS credentials resolve (via
`AWS_PROFILE` or the default chain) and that the account has **Bedrock model
access** enabled for the models in your seed data.

Run each API in its own terminal:

```bash
cd src/apis/app_api      && uv run python main.py   # http://localhost:8000
cd src/apis/inference_api && uv run python main.py   # http://localhost:8001
```

Run the backend test suite:

```bash
cd backend && uv run python -m pytest tests/ -v
```

## Authentication for local dev

The SPA authenticates through Cognito, which means a round-trip you may not want
on every local run. Two options:

- **Full Cognito (BFF).** Set the `COGNITO_*` and `BFF_*` variables in `src/.env`
  from the deployed stack, with `BFF_AUTH_CALLBACK_URL=http://localhost:8000/auth/callback`.
  The deployed Cognito app client must allow that callback URL. This exercises
  the real login flow.
- **Auth bypass (`SKIP_AUTH=true`).** Returns a fake admin user and skips Cognito
  entirely — handy when you have no IdP access or are driving the app
  unattended.

:::danger[SKIP_AUTH is local-only by design]
`SKIP_AUTH=true` is gated hard. The App API **refuses to boot** unless every
entry in `CORS_ORIGINS` is a localhost URL, a CI guard rejects any PR that puts
it into a Dockerfile, CDK, script, or workflow, and the Inference API is never
bypassed. Never set it in a deployed environment.
:::

## Frontend

```bash
cd frontend/ai.client
npm install
npm start          # ng serve → http://localhost:4200
npm test           # ng test
```

The SPA reads its API base URL from `src/environments/environment.ts`, which
points at the local App API (`http://localhost:8000`). For browser calls to
succeed, the App API's `CORS_ORIGINS` must include `http://localhost:4200` — the
`.env.example` default already does. If you instead point the SPA at a *deployed*
backend, that environment must allow `http://localhost:4200` in its
`CDK_CORS_ORIGINS` (and, for MCP Apps or artifact iframes, its
`*_EXTRA_FRAME_ANCESTORS`); see
[Environments](/agentcore-public-stack/deployment/environments/).

## Working on the infrastructure

To iterate on the CDK constructs themselves, the tooling runs locally even though
deploying happens in CI:

```bash
cd infrastructure
npm ci
npx tsc --noEmit
npx jest
npx cdk synth
```

Synth and tests run on your machine; the actual `cdk deploy` is owned by the
`platform.yml` workflow — see
[Platform (CDK)](/agentcore-public-stack/deployment/platform-cdk/).

## See also

- [Deployment Overview](/agentcore-public-stack/deployment/overview/) — stand up the environment you'll develop against.
- [Configuration](/agentcore-public-stack/configuration/environment-variables/) — the full environment-variable reference.
