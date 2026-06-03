# Step 4 of 5 — Deploy Workflows

✅ Step 1: Prerequisites<br>
✅ Step 2: AWS Setup<br>
✅ Step 3: Configure GitHub<br>
➡️ **Step 4: Deploy Workflows** ← You are here<br>
⬜ Step 5: Verify Deployment

⏱️ ~25–35 minutes · 🟢 Easy (just clicking buttons) · Requires: Steps 1–3 complete

---

The platform deploys in three workflows that you run in order: infrastructure, then backend code, then frontend. A fourth workflow seeds initial data once at the end.

## What you'll need

- Access to the **Actions** tab in your forked repository
- Patience — the first deploy takes the longest as AWS provisions new resources

---

## Single-stack architecture

All AWS infrastructure lives in **one CDK stack** (`PlatformStack`): VPC, ALB, ECS cluster, ~24 DynamoDB tables, 6 S3 buckets, Cognito, KMS, Secrets Manager, AgentCore (Memory + Code Interpreter + Browser + Gateway + Runtime), CloudFront distributions (SPA + artifacts + MCP sandbox), Route53 aliases, RAG ingestion + artifact render Lambdas, and the SageMaker fine-tuning IAM. There is no separate backend / inference / artifacts / sandbox / fine-tuning stack to deploy. Application **code** is shipped out-of-band by the backend workflow via AWS APIs (no CDK).

## How to run a workflow

1. Go to the **Actions** tab in your repository
2. Select the workflow from the left sidebar
3. Click **Run workflow** → select the `main` branch → click the green **Run workflow** button
4. Wait for it to complete (green checkmark) before starting the next one

> [!IMPORTANT]
> Run the deploy workflows **in this order on a fresh account**. Each one depends on resources or images created by the previous one.

> [!WARNING]
> The first deploy of `Platform Stack` takes the longest (~15–20 minutes) because AWS is provisioning everything from scratch. Subsequent runs only ship deltas.

---

## Deployment order

| # | Workflow | What it does |
|:-:|---------|--------------|
| 1 | **Platform Stack** (`platform.yml`) | `cdk deploy` — provisions every AWS resource. Synths and pushes the bootstrap container images (a tiny stub that responds to ALB/AgentCore health checks), then seeds the per-service `image-tag` SSM parameters with their bootstrap URIs so first-deploy CFN parameter resolution succeeds. After this, the ECS service and AgentCore Runtime are running the bootstrap stub. |
| 2 | **Deploy Backend** (`backend.yml`) | Builds and pushes real container images for app-api, inference-api, and rag-ingestion (parallel jobs, content-hash short-circuited so unchanged services don't rebuild). For each, calls the matching AWS API to roll the live service over to the new image: `aws ecs register-task-definition` + `update-service` for app-api, `aws bedrock-agentcore-control update-agent-runtime` for inference-api, `aws lambda update-function-code` for rag-ingestion, and a separate `update-function-code` for the artifact-render zip. |
| 3 | **Frontend Deploy** (`frontend-deploy.yml`) | Builds the Angular SPA (production) and `aws s3 sync`s `dist/ai.client/browser/` to the SPA bucket, then issues a CloudFront invalidation. |
| 4 | **Bootstrap Data Seeding** (`bootstrap-data-seeding.yml`) | One-time post-deploy step. Seeds default models, RBAC roles, quota tiers, and tool catalog entries into DynamoDB. Re-running is safe and idempotent — re-runs upsert. |

> [!TIP]
> After the first successful run of all four, day-to-day code changes only re-run **Deploy Backend** (and **Frontend Deploy** for SPA changes). You don't run `Platform Stack` again until you actually change infrastructure (new tables, new IAM grants, network changes, etc.) — which is rare.

### Status badges

| Workflow | Status |
|----------|--------|
| Platform Stack | [![1.](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/platform.yml/badge.svg)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/platform.yml) |
| Deploy Backend | [![2.](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/backend.yml/badge.svg)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/backend.yml) |
| Frontend Deploy | [![3.](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/frontend-deploy.yml/badge.svg)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/frontend-deploy.yml) |
| Bootstrap Data Seeding | [![4.](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/bootstrap-data-seeding.yml/badge.svg)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/bootstrap-data-seeding.yml) |

> [!NOTE]
> All workflows default to the **production** environment when triggered manually.

---

## What each workflow does

<details>
<summary>1. Platform Stack (CDK)</summary>

Runs `scripts/platform/deploy.sh`, which:

1. `cdk synth` — produces the CFN template under `cdk.out/`.
2. `cdk-assets publish` — pushes the bootstrap container images (small stdlib HTTP servers that respond to ALB/AgentCore health checks) to the cdk-assets ECR repo.
3. `scripts/stack-bootstrap/seed-image-tags.sh` — for each compute service, writes `/<prefix>/<service>/image-tag` to SSM with the bootstrap URI **only if the parameter doesn't already exist**. Subsequent runs skip — the build pipeline owns the parameter from then on.
4. `cdk deploy {prefix}-PlatformStack` — provisions everything.

What gets provisioned:

- **Networking**: VPC, public + private subnets across 2 AZs, ALB with HTTPS listeners, Route53 aliases.
- **Identity**: Cognito User Pool + App Client (first-boot admin signup), KMS keys for OAuth token encryption + BFF cookie signing, Secrets Manager for OAuth client secrets.
- **Data**: ~24 DynamoDB tables (sessions, users, roles, quotas, models, costs, OAuth, fine-tuning jobs, etc.), 6 S3 buckets (file uploads, RAG documents, RAG vectors, artifacts content, MCP sandbox shell, fine-tuning data), all encrypted at rest with public-access fully blocked.
- **AgentCore**: Memory, Code Interpreter, Browser, Gateway, and the AgentCore Runtime resource (initially pointed at the inference-api bootstrap image).
- **Compute**: App API ECS Fargate service + task definition (initially pointed at the app-api bootstrap image), RAG ingestion Lambda, artifact render Lambda.
- **Edge**: CloudFront distributions for the SPA (`{CDK_DOMAIN_NAME}`), artifacts subdomain (`artifacts.{CDK_DOMAIN_NAME}`), and MCP sandbox subdomain (`mcp-sandbox.{CDK_DOMAIN_NAME}`). Custom origins, OAC-only S3 reads, response-headers policies with strict CSP.
- **ML**: SageMaker execution IAM role + security group for fine-tuning training jobs.

Artifacts, MCP sandbox, and SageMaker fine-tuning are **always provisioned** — there are no `CDK_*_ENABLED` flags. If you don't use them, the resources sit idle at zero or near-zero cost.

</details>

<details>
<summary>2. Deploy Backend (AWS-API code deploy)</summary>

`backend.yml` ships application code without touching CFN. Each service has a build job followed by a deploy job:

| Service | Build | Deploy |
|---------|-------|--------|
| app-api | `scripts/build/build-one.sh app-api` (linux/amd64) | `aws ecs register-task-definition` + `update-service` + `wait services-stable` |
| inference-api | `scripts/build/build-one.sh inference-api` (linux/arm64, runs on AgentCore Runtime) | `aws bedrock-agentcore-control update-agent-runtime` (full-replacement payload), polled to `READY` |
| rag-ingestion | `scripts/build/build-one.sh rag-ingestion` (image Lambda) | `aws lambda update-function-code --image-uri` |
| artifact-render | (zips `backend/src/lambdas/artifact_render/` directly — no Docker) | `aws lambda update-function-code` |

Each build is content-hash short-circuited: it computes a SHA-256 of the Dockerfile + tracked source + dependency manifests, then only runs `docker build` + `docker push` if ECR doesn't already have an image with that tag. After every push, the build pipeline writes the new tag to `/<prefix>/<service>/image-tag` in SSM. The compute constructs in CDK read those SSM parameters at deploy time, so any subsequent CFN re-registration of the task def or Runtime picks up the latest live image, not the bootstrap stub.

</details>

<details>
<summary>3. Frontend Deploy</summary>

Builds the Angular SPA (`npm run build` in `frontend/ai.client/`), produces `dist/ai.client/browser/`, then `aws s3 sync`s that directory to the SPA bucket. The build is content-hashed too — if no source changes, the sync is a no-op except for a minimal CloudFront invalidation of `index.html` and `index.csr.html`.

</details>

<details>
<summary>4. Bootstrap Data Seeding</summary>

`scripts/stack-bootstrap/seed.sh` resolves table names from SSM (managed-models, app-roles, user-quotas, tools) and runs `backend/scripts/seed_bootstrap_data.py` to upsert the default catalog. Idempotent — safe to re-run after the data has changed.

> [!NOTE]
> Authentication is handled by Cognito's first-boot flow — no auth provider seeding is needed. The first person to access the application creates the admin account directly.

</details>

---

## When to re-run each workflow

| You changed | Re-run |
|-------------|--------|
| `infrastructure/lib/**` (CDK constructs, stack composition) | Platform Stack |
| `backend/src/apis/app_api/**` | Deploy Backend (only `build-app-api` + `deploy-app-api-code` jobs run) |
| `backend/src/apis/inference_api/**` or `backend/src/agents/**` | Deploy Backend (only `build-inference-api` + `deploy-inference-api-code` jobs run) |
| `backend/src/lambdas/rag_ingestion/**` | Deploy Backend (only `build-rag-ingestion` + `deploy-rag-ingestion-code` jobs run) |
| `backend/src/lambdas/artifact_render/**` | Deploy Backend (only `deploy-artifact-render-code` runs — zip is built in the deploy job) |
| `frontend/ai.client/**` | Frontend Deploy |
| Default tools / models / roles | Bootstrap Data Seeding |

You don't need to gate Deploy Backend or Frontend Deploy behind Platform Stack on routine changes — they only depend on infrastructure that already exists.

---

## If a workflow fails

1. Click into the failed workflow run to see the error logs
2. Check the [Troubleshooting Guide](./troubleshooting.md) for common issues
3. Fix the issue (usually a missing variable or permission) and re-run the workflow
4. You don't need to re-run workflows that already succeeded

> [!TIP]
> The most common failure cause is a missing or incorrect variable in Step 3. Double-check your configuration if a workflow fails immediately.

---

### ➡️ [Next: Step 5 — Verify Deployment](./step-05-verify.md)
