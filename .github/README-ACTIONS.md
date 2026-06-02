# Deploy AgentCore Public Stack with GitHub Actions

Deploy a production-ready multi-agent AI platform to your AWS account in about 45 minutes. This guide walks you through every step.

> **TL;DR — Ready to begin?**
>
> ### 👉 [Start here — Step 1: Prerequisites](./docs/deploy/step-01-prerequisites.md)

## Architecture Overview

The platform uses a **single-stack architecture**:

- **One CDK stack** (`PlatformStack`) provisions all AWS infrastructure — VPC, ALB, DynamoDB, S3, Cognito, CloudFront, AgentCore, ECS, Lambdas.
- **Application code ships out-of-band** via AWS APIs — not via CDK deploys. This means infrastructure changes (rare) and code changes (frequent) are completely decoupled.

## What You'll Deploy

### Infrastructure (via `platform.yml`)

| Component | Description |
|-----------|-------------|
| **VPC + ALB + ECS** | Networking, load balancer, and container orchestration |
| **DynamoDB** | ~24 tables for all application state |
| **S3** | 6 buckets (file uploads, RAG, artifacts, SPA, MCP sandbox, fine-tuning) |
| **Cognito** | User pool, identity providers, BFF app client |
| **CloudFront** | SPA distribution + artifacts iframe origin + MCP sandbox proxy |
| **AgentCore** | Memory, Code Interpreter, Browser, Gateway, Runtime |
| **SageMaker IAM** | Execution role for fine-tuning jobs |

### Application Code (via `backend.yml`)

| Service | Deploy Method |
|---------|--------------|
| **App API** | ECR push → ECS service update |
| **Inference API** | ECR push → AgentCore Runtime update |
| **RAG Ingestion** | ECR push → Lambda update-function-code |
| **Artifact Render** | Zip → Lambda update-function-code |

### Frontend (via `frontend-deploy.yml`)

| Component | Deploy Method |
|-----------|--------------|
| **Angular SPA** | S3 sync + CloudFront invalidation |

### Bootstrap Data (via `bootstrap-data-seeding.yml`)

| Component | Description |
|-----------|-------------|
| **Seed data** | Auth provider config, default models, roles, and tools |

---

## Workflows

| Workflow | Trigger | What it does |
|---------|---------|--------------|
| `platform.yml` | Infra code changes / manual | `cdk deploy` — provisions or updates all AWS resources |
| `backend.yml` | Backend code changes / manual | Builds Docker images (content-hash skip), pushes to ECR, updates ECS/Lambda/Runtime |
| `frontend-deploy.yml` | Frontend code changes / manual | Builds Angular SPA, syncs to S3, invalidates CloudFront |
| `bootstrap-data-seeding.yml` | Manual | Seeds DynamoDB with default config (first deploy only) |
| `teardown.yml` | Manual | Destroys all CDK stacks (for cleanup) |
| `nightly-deploy-pipeline.yml` | Nightly / manual | Full end-to-end: platform → backend → frontend |

---

## Deployment Steps

Follow each step in order. Click a step to open its guide.

| | Step | Time | Difficulty |
|---|------|------|------------|
| **1** | [Prerequisites](./docs/deploy/step-01-prerequisites.md) | ~5 min | Easy |
| **2** | [AWS Setup](./docs/deploy/step-02-aws-setup.md) | ~15 min | Moderate |
| **3** | [Configure GitHub](./docs/deploy/step-03-github-config.md) | ~10 min | Moderate |
| **4** | [Deploy Workflows](./docs/deploy/step-04-deploy.md) | ~20 min | Easy |
| **5** | [Verify Deployment](./docs/deploy/step-05-verify.md) | ~5 min | Easy |

> [!TIP]
> Most of the time is spent waiting for AWS resources to provision. The actual hands-on work is straightforward.

---

## Deploy Order (First Time)

```
1. platform.yml          → provisions all AWS infrastructure (~15 min)
2. backend.yml           → builds + deploys all container images (~5 min)
3. frontend-deploy.yml   → builds + deploys the Angular SPA (~2 min)
4. bootstrap-data-seeding.yml → seeds default config data (~1 min)
```

After the first deploy, `platform.yml` only needs to run when infrastructure changes. Day-to-day pushes trigger `backend.yml` and/or `frontend-deploy.yml` automatically.

---

## Content-Hash Docker Builds

The `backend.yml` workflow uses **content-hash tagging** — each image is tagged with a SHA-256 hash of its source inputs (Dockerfile + source tree + dependency manifests). If ECR already has an image with that tag, the build is skipped entirely. This means:

- Pushing a frontend-only change doesn't rebuild any Docker images
- Pushing a change to one service only rebuilds that service's image
- Unchanged services deploy in seconds (just verifies the tag exists)

---

## Quick Links

- [Troubleshooting](./docs/deploy/troubleshooting.md) — common issues and fixes
- [Full Configuration Reference](./ACTIONS-REFERENCE.md) — every available variable and secret
