# Step 4 of 5 — Deploy Workflows

✅ Step 1: Prerequisites<br>
✅ Step 2: AWS Setup<br>
✅ Step 3: Configure GitHub<br>
➡️ **Step 4: Deploy Workflows** ← You are here<br>
⬜ Step 5: Verify Deployment

⏱️ ~20-30 minutes · 🟢 Easy (just clicking buttons) · Requires: Steps 1-3 complete

---

Now for the fun part. You'll trigger up to 8 GitHub Actions workflows in order. Each one deploys a different layer of the stack. Workflows that share the same step number can be run in parallel — just wait for the previous step to finish first.

## What you'll need for this step

- Access to the **Actions** tab in your forked repository
- Patience — the first deploy takes the longest as AWS provisions new resources

---

## How to Run Each Workflow

1. Go to the **Actions** tab in your repository
2. Select the workflow from the left sidebar
3. Click **Run workflow** → select the `main` branch → click the green **Run workflow** button
4. Wait for it to complete (green checkmark) before starting the next one

> [!IMPORTANT]
> Run these workflows **in order**. Each step depends on resources created by the previous step. Wait for each workflow to finish before starting the next.

> [!WARNING]
> The first deployment takes the longest (~15-20 minutes for Step 1) because AWS is creating resources from scratch. Subsequent deployments are much faster.

---

## Deployment Order

| # | Workflow | What It Deploys |
|:-:|---------|-----------------|
| 1 | **Deploy Infrastructure** | VPC, subnets, ALB, ECS cluster, DynamoDB tables, S3 buckets |
| 2 | **Deploy RAG Ingestion** | Document ingestion pipeline for retrieval-augmented generation |
| 2 | **Deploy SageMaker Fine-Tuning** *(optional)* | SageMaker training/inference resources, S3 bucket, DynamoDB tables. Requires `CDK_FINE_TUNING_ENABLED=true` ([Step 3](./step-03-github-config.md#optional-features)). |
| 2 | **Deploy Artifacts** *(optional)* | DynamoDB metadata + S3 content + CloudFront at `artifacts.{domain}` + Lambda render service. Requires `CDK_ARTIFACTS_ENABLED=true` and `CDK_ARTIFACTS_CERTIFICATE_ARN` (cert MUST be in `us-east-1`). |
| 3 | **Deploy Inference API** | Strands Agent runtime container on ECS (Bedrock AgentCore) |
| 4 | **Deploy App API** | Backend REST API container on ECS |
| 5 | **Deploy Frontend** | Angular app to S3 + CloudFront distribution |
| 5 | **Deploy Gateway** | Lambda functions for MCP tool endpoints |
| 6 | **Seed Bootstrap Data** | Auth provider, default models, roles, tools, and admin config |

> [!TIP]
> Steps that share the same number can be deployed in parallel — just wait for the previous step to finish first.

### Status Badges

You can monitor the current state of each workflow:

| Workflow | Status |
|----------|--------|
| Deploy Infrastructure | [![1.](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/infrastructure.yml/badge.svg)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/infrastructure.yml) |
| Deploy RAG Ingestion | [![2.](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/rag-ingestion.yml/badge.svg)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/rag-ingestion.yml) |
| Deploy SageMaker Fine-Tuning *(optional)* | [![2.](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/sagemaker-fine-tuning.yml/badge.svg)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/sagemaker-fine-tuning.yml) |
| Deploy Artifacts *(optional)* | [![2.](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/artifacts.yml/badge.svg)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/artifacts.yml) |
| Deploy Inference API | [![3.](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/inference-api.yml/badge.svg)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/inference-api.yml) |
| Deploy App API | [![4.](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/app-api.yml/badge.svg)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/app-api.yml) |
| Deploy Frontend | [![5.](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/frontend.yml/badge.svg)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/frontend.yml) |
| Deploy Gateway | [![5.](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/gateway.yml/badge.svg)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/gateway.yml) |
| Seed Bootstrap Data | [![6.](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/bootstrap-data-seeding.yml/badge.svg)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/bootstrap-data-seeding.yml) |

> [!NOTE]
> All workflows default to the **production** environment when triggered manually.

---

## First-time deploy of a new optional stack

> [!IMPORTANT]
> When you flip a previously-disabled stack on for the first time (e.g. setting `CDK_FINE_TUNING_ENABLED=true` or `CDK_ARTIFACTS_ENABLED=true`), the PR that enables it will modify multiple stacks at once — the new stack itself, plus any consumer stacks that read its SSM exports. If you simply merge to `develop`, every affected workflow fires in parallel and the consumers will fail with `Parameter not found` because the new stack hasn't written its SSM keys yet. Subsequent normal pushes don't hit this — the race only happens on the very first deploy of new cross-stack SSM dependencies.

**Recommended sequence — deploy in tier order via `workflow_dispatch` against the feature branch, then merge:**

1. Push your feature branch. Do **not** merge yet.
2. Open the **Actions** tab, pick the **Infrastructure** workflow, click **Run workflow**, and select your feature branch. Wait for it to go green. This publishes any new foundation SSM keys (e.g. the JWT signing secret for artifacts).
3. Run the new stack's workflow (e.g. **Artifacts** or **SageMaker Fine-Tuning**) the same way. Wait for green. This publishes the new stack's SSM exports.
4. Merge the PR. The consumer workflows (Inference API, App API, Frontend) re-deploy automatically on push to `develop` and find every SSM key they need on the first try.

If you skip steps 2–3 and merge directly, the failing consumer workflows aren't broken — just click **Re-run** in the Actions tab after the foundation + new stack have completed, and they'll succeed. The pre-merge sequence above just spares you the retry dance and keeps your Actions history clean.

---

## What Each Workflow Does

<details>
<summary>1. Deploy Infrastructure (VPC, ALB, ECS)</summary>

Creates the foundational AWS resources:
- VPC with public and private subnets
- Application Load Balancer with HTTPS
- ECS cluster for running containers
- DynamoDB tables (sessions, users, roles, quotas, models, costs)
- S3 buckets (file uploads, frontend assets)
- Route 53 DNS records for your API subdomain

This is the longest-running workflow on first deploy (~15-20 minutes).

</details>

<details>
<summary>2. Deploy RAG Ingestion</summary>

Sets up the document ingestion pipeline for retrieval-augmented generation. This enables your agents to search through uploaded documents when answering questions.

</details>

<details>
<summary>2. Deploy SageMaker Fine-Tuning (Optional)</summary>

Deploys the ML training and inference infrastructure for fine-tuning open-source models. This step is optional — skip it if you don't need fine-tuning capabilities. To enable, set the `CDK_FINE_TUNING_ENABLED` variable to `true` in your GitHub environment before running.

Creates:
- DynamoDB tables for job tracking and access control
- S3 bucket for datasets, model artifacts, and inference results (30-day lifecycle)
- SageMaker execution IAM role
- Security group for SageMaker training jobs (outbound HTTPS only)

After deployment, grant fine-tuning access to users via the admin dashboard.

</details>

<details>
<summary>2. Deploy Artifacts (Optional)</summary>

Provisions iframe-isolated artifact rendering — versioned, sandboxed HTML/code artifacts the agent can generate and the user can render inline. Skip if you don't need this capability; the rest of the stack works without it.

Creates:
- DynamoDB table for artifact metadata + version log
- S3 bucket for artifact content blobs (private, no CORS)
- CloudFront distribution serving `artifacts.{CDK_DOMAIN_NAME}`
- Route 53 A record for the artifacts subdomain
- Python render Lambda that wraps content in a strict CSP shell

To enable, set these GitHub environment variables before running:

- `CDK_ARTIFACTS_ENABLED=true`
- `CDK_ARTIFACTS_CERTIFICATE_ARN` — ACM cert ARN that covers `artifacts.{domain}`. **Must be in `us-east-1`** (CloudFront requirement). If you followed [Step 2c](./step-02-aws-setup.md#2c-create-acm-certificates) and issued the CloudFront cert with a `*.example.com` SAN, reuse the same value as `CDK_FRONTEND_CERTIFICATE_ARN` — no third certificate is needed.
- `CDK_ARTIFACTS_RETENTION_DAYS` *(optional, default 90)* — how long soft-deleted artifacts linger before lifecycle expiry.

The artifact origin is intentionally a sibling subdomain (not the SPA origin) so artifact JS runs cross-origin and cannot access the `__Host-` session cookies, `localStorage`, or the app API. Defense in depth via strict CSP (`connect-src 'none'`, pinned `frame-ancestors`) is enforced both at the Lambda response and at the CloudFront response-headers policy.

</details>

<details>
<summary>3. Deploy Inference API (AgentCore Runtime)</summary>

Deploys the Strands Agent runtime as an ECS service. This is the brain of the system — it runs the AI agent that processes user messages, invokes tools, and generates responses using AWS Bedrock models.

</details>

<details>
<summary>4. Deploy App API (Backend)</summary>

Deploys the main backend REST API as an ECS service. Handles:
- User authentication and session management
- Chat message routing to the Inference API
- Admin endpoints for models, tools, and roles
- SSE streaming for real-time responses

</details>

<details>
<summary>5. Deploy Frontend (CloudFront)</summary>

Builds the Angular application and deploys it to:
- S3 bucket for static asset hosting
- CloudFront CDN for global distribution and HTTPS
- Route 53 DNS record for your frontend domain

</details>

<details>
<summary>5. Deploy Gateway (Lambda Tools)</summary>

Deploys Lambda-based MCP tool endpoints behind API Gateway. These provide the agent with access to external services like Wikipedia, ArXiv, financial data, and more. Tools are invoked via MCP protocol with AWS SigV4 authentication.

</details>

<details>
<summary>6. Seed Bootstrap Data</summary>

Seeds your application with initial configuration:
- Default AI models and pricing
- Application roles and permissions
- Default tool configurations

You can re-run this workflow later to update seed data.

> [!NOTE]
> Authentication is handled by Cognito's first-boot flow — no auth provider seeding is needed. The first person to access the application will create the admin account directly.

</details>

---

## If a Workflow Fails

1. Click into the failed workflow run to see the error logs
2. Check the [Troubleshooting Guide](./troubleshooting.md) for common issues
3. Fix the issue (usually a missing variable or permission) and re-run the workflow
4. You don't need to re-run workflows that already succeeded

> [!TIP]
> The most common failure cause is a missing or incorrect variable in Step 3. Double-check your configuration if a workflow fails immediately.

---

### ➡️ [Next: Step 5 — Verify Deployment](./step-05-verify.md)
