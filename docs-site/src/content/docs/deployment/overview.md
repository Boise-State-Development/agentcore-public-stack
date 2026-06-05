---
title: Deployment Overview
description: How code and infrastructure ship to AWS.
sidebar:
  order: 1
---

The platform deploys into **your own AWS account** through a small set of GitHub
Actions workflows. Infrastructure is provisioned once by a single CDK stack;
application code ships separately, and far more often. Understanding that split
is the key to everything else in this section.

## Single-stack architecture

- **One CDK stack** (`PlatformStack`) provisions every AWS resource — VPC, ALB,
  ECS, ~24 DynamoDB tables, 6 S3 buckets, Cognito, KMS, Secrets Manager,
  CloudFront, AgentCore (Memory, Code Interpreter, Browser, Gateway, Runtime),
  and the supporting Lambdas and IAM.
- **Application code ships out-of-band** via AWS APIs — not through CDK. A code
  change pushes a new container image (or Lambda zip) and rolls the live service
  over to it; CloudFormation is never touched.

Because the two are decoupled, infrastructure changes (rare) and code changes
(frequent) never block on each other. After the first deploy you'll run the
infrastructure workflow only when you actually change infrastructure.

## What you deploy

The platform stands up in four parts, each owned by its own workflow.

#### Infrastructure — `platform.yml`

| Component | Description |
|-----------|-------------|
| **VPC + ALB + ECS** | Networking, load balancer, and container orchestration |
| **DynamoDB** | ~24 tables for all application state |
| **S3** | 6 buckets — file uploads, RAG documents, RAG vectors, artifacts, MCP sandbox shell, fine-tuning data |
| **Cognito** | User pool and BFF app client for first-boot admin signup |
| **CloudFront** | SPA distribution, artifacts iframe origin, and MCP sandbox proxy |
| **AgentCore** | Memory, Code Interpreter, Browser, Gateway, Runtime |
| **SageMaker IAM** | Execution role for fine-tuning jobs |

#### Application code — `backend.yml`

| Service | Deploy method |
|---------|---------------|
| **App API** | ECR push → ECS service update |
| **Inference API** | ECR push → AgentCore Runtime update |
| **RAG Ingestion** | ECR push → Lambda update-function-code |
| **Artifact Render** | Zip → Lambda update-function-code |

#### Frontend — `frontend-deploy.yml`

| Component | Deploy method |
|-----------|---------------|
| **Angular SPA** | S3 sync + CloudFront invalidation |

#### Bootstrap data — `bootstrap-data-seeding.yml`

| Component | Description |
|-----------|-------------|
| **Seed data** | Default models, RBAC roles, quota tiers, and the tool catalog |

## The deployment workflows

| Workflow | Trigger | What it does |
|----------|---------|--------------|
| `platform.yml` | Infra changes / manual | `cdk deploy` — provisions or updates all AWS resources |
| `backend.yml` | Backend changes / manual | Builds service images (content-hash skip), pushes to ECR, rolls ECS / Lambda / Runtime over |
| `frontend-deploy.yml` | Frontend changes / manual | Builds the Angular SPA, syncs to S3, invalidates CloudFront |
| `bootstrap-data-seeding.yml` | Manual | Seeds DynamoDB with default config (first deploy only) |
| `teardown.yml` | Manual | Destroys the stack — for cleanup |
| `nightly-deploy-pipeline.yml` | Nightly / manual | Full end-to-end: platform → backend → frontend |

## Before you begin

A first deploy needs three things in hand:

- An **AWS account** with administrator access (or permission to create IAM
  roles, VPCs, ECS clusters, CloudFront distributions, Route 53 zones, ACM
  certificates, Lambda functions, and DynamoDB tables).
- A **GitHub account** with a fork of the repository.
- A **domain name** you control, with the ability to update its nameservers.

No external identity provider is required to start — Cognito handles the first
admin signup, and federated login (Entra ID, Okta, Google) can be added later
from the admin dashboard. The AWS resources the platform needs first — an auth
method, a Route 53 hosted zone, and two ACM certificates — are covered on the
[Platform (CDK)](/agentcore-public-stack/deployment/platform-cdk/) page.

:::note
Most of a first deploy is spent waiting for AWS to provision resources. The
hands-on work — wiring credentials and variables into GitHub — is
straightforward.
:::

## First-time deploy order

Run the workflows in this order on a fresh account. Each depends on resources or
images created by the one before it.

1. **`platform.yml`** — provisions all AWS infrastructure (~15–20 min the first time).
2. **`backend.yml`** — builds and deploys every service image (~5 min).
3. **`frontend-deploy.yml`** — builds and publishes the Angular SPA (~2 min).
4. **`bootstrap-data-seeding.yml`** — seeds the default config data (~1 min).

After this, `platform.yml` only runs when infrastructure changes. Day-to-day
pushes trigger `backend.yml` and/or `frontend-deploy.yml` automatically.

## Day-to-day: when to re-run

| You changed | Re-run |
|-------------|--------|
| `infrastructure/lib/**` (CDK constructs, stack composition) | `platform.yml` |
| `backend/src/apis/app_api/**` | `backend.yml` (only the app-api jobs run) |
| `backend/src/apis/inference_api/**` or `backend/src/agents/**` | `backend.yml` (only the inference-api jobs run) |
| `backend/src/lambdas/rag_ingestion/**` | `backend.yml` (only the rag-ingestion jobs run) |
| `backend/src/lambdas/artifact_render/**` | `backend.yml` (only the artifact-render job runs) |
| `frontend/ai.client/**` | `frontend-deploy.yml` |
| Default tools / models / roles | `bootstrap-data-seeding.yml` |

You don't need to gate `backend.yml` or `frontend-deploy.yml` behind
`platform.yml` for routine changes — they only depend on infrastructure that
already exists.

## Content-hash builds

`backend.yml` and `frontend-deploy.yml` use **content-hash tagging**. Each image
or bundle is tagged with a SHA-256 of its source inputs — the Dockerfile, the
tracked source tree, and the dependency manifests. If the registry already has
that tag, the build is skipped entirely. In practice:

- A frontend-only change rebuilds no Docker images.
- A change to one service rebuilds only that service's image.
- Unchanged services deploy in seconds — the workflow just verifies the tag exists.

## Verify your deployment

Once all four workflows are green, open your frontend URL and confirm:

- The page loads and shows the **first-boot setup** (fresh deploy) or a login page.
- You can **create the admin account** and log in to the chat interface.
- A test message gets a **streaming response** from the agent.
- The **admin section** is visible and lets you manage models, tools, and roles.

If anything is off, the [Troubleshooting](/agentcore-public-stack/deployment/troubleshooting/)
page is organized by deployment phase.

## Where to go next

- [Platform (CDK)](/agentcore-public-stack/deployment/platform-cdk/) — AWS prerequisites, GitHub configuration, and what `platform.yml` provisions.
- [Backend Images](/agentcore-public-stack/deployment/backend-images/) — how service code ships without touching CloudFormation.
- [Frontend Deploy](/agentcore-public-stack/deployment/frontend/) — publishing the SPA to S3 and CloudFront.
- [Bootstrap Data Seeding](/agentcore-public-stack/deployment/bootstrap-data/) — the one-time default-config seed.
- [Environments](/agentcore-public-stack/deployment/environments/) — dev vs prod, per-environment overrides, and the full configuration reference.
- [Upgrading from Multi-Stack](/agentcore-public-stack/deployment/upgrade/) — migrating an older multi-stack deployment.
