# Step 4 of 5 — Deploy Workflows

✅ Step 1: Prerequisites<br>
✅ Step 2: AWS Setup<br>
✅ Step 3: Configure GitHub<br>
➡️ **Step 4: Deploy Workflows** ← You are here<br>
⬜ Step 5: Verify Deployment

⏱️ ~20-30 minutes · 🟢 Easy (just clicking buttons) · Requires: Steps 1-3 complete

---

Now for the fun part. You'll trigger up to 9 GitHub Actions workflows in order. Each one deploys a different layer of the stack. Workflows that share the same step number can be run in parallel — just wait for the previous step to finish first.

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
| 2 | **Deploy MCP Sandbox** *(optional)* | S3 + CloudFront at `mcp-sandbox.{domain}` + Route53 serving the MCP Apps sandbox-proxy shell. Requires `CDK_MCP_SANDBOX_ENABLED=true` and `CDK_MCP_SANDBOX_CERTIFICATE_ARN` (cert MUST be in `us-east-1`). Inert until later MCP Apps host-renderer PRs wire the SPA to it. |
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
| Deploy MCP Sandbox *(optional)* | [![2.](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/mcp-sandbox.yml/badge.svg)](https://github.com/Boise-State-Development/agentcore-public-stack/actions/workflows/mcp-sandbox.yml) |
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
- `CDK_ARTIFACTS_CERTIFICATE_ARN` — ACM cert ARN that covers `artifacts.{domain}`. **Must be in `us-east-1`** (CloudFront requirement). Reuse `CDK_FRONTEND_CERTIFICATE_ARN` **only if `CDK_DOMAIN_NAME` is your apex** — a `*.example.com` cert covers `artifacts.example.com` but, because TLS wildcards are one label deep, does **not** cover `artifacts.alpha.example.com`. If `CDK_DOMAIN_NAME` is a subdomain, issue a dedicated `us-east-1` cert for `*.{CDK_DOMAIN_NAME}`. See [Step 2c](./step-02-aws-setup.md#2c-create-acm-certificates).
- `CDK_ARTIFACTS_RETENTION_DAYS` *(optional, default 90)* — how long soft-deleted artifacts linger before lifecycle expiry.
- `CDK_ARTIFACTS_EXTRA_FRAME_ANCESTORS` *(optional, default none)* — comma-separated extra origins allowed to embed artifact iframes via CSP `frame-ancestors`, on top of `https://{domain}`. Set to `http://localhost:4200` to point a local SPA at this environment. **Leave unset in production** — every listed origin can frame users' artifacts. Prefer a one-off targeted `cdk deploy '*ArtifactsStack*'` with this var exported over committing it as a CI variable, so localhost never lands in automated shared-env deploys.

The artifact origin is intentionally a sibling subdomain (not the SPA origin) so artifact JS runs cross-origin and cannot access the `__Host-` session cookies, `localStorage`, or the app API. Defense in depth via strict CSP (`connect-src 'none'`, pinned `frame-ancestors`) is enforced both at the Lambda response and at the CloudFront response-headers policy.

</details>

<details>
<summary>2. Deploy MCP Sandbox (Optional)</summary>

Provisions the MCP Apps **sandbox-proxy origin** — a dedicated cross-origin shell that the SPA's MCP App iframe is pointed at, so interactive MCP App UIs run isolated from the `ai.client` origin. This is PR #1 of the MCP Apps host-renderer initiative (`docs/kaizen/scoping/mcp-apps-host-renderer.md`). Skip if you don't need MCP Apps; the rest of the stack works without it.

Creates:
- S3 bucket (private, OAC-only) holding the static `proxy.html` + `proxy.js` shell
- CloudFront distribution serving `mcp-sandbox.{CDK_DOMAIN_NAME}`
- Route 53 A record for the sandbox subdomain
- A CloudFront response-headers policy that stamps `Content-Security-Policy: frame-ancestors <SPA origin only>`

To enable, set these GitHub environment variables before running:

- `CDK_MCP_SANDBOX_ENABLED=true`
- `CDK_MCP_SANDBOX_CERTIFICATE_ARN` — ACM cert ARN that covers `mcp-sandbox.{domain}`. **Must be in `us-east-1`** (CloudFront requirement). The same wildcard-depth caveat as Artifacts applies: a `*.example.com` cert covers `mcp-sandbox.example.com` but **not** `mcp-sandbox.alpha.example.com`. If `CDK_DOMAIN_NAME` is a subdomain, issue a dedicated `us-east-1` cert for `*.{CDK_DOMAIN_NAME}`. See [Step 2c](./step-02-aws-setup.md#2c-create-acm-certificates).
- `CDK_MCP_SANDBOX_EXTRA_FRAME_ANCESTORS` *(optional, default none)* — comma-separated extra origins allowed to embed the proxy iframe via CSP `frame-ancestors`, on top of `https://{domain}`. Set to `http://localhost:4200` to point a local SPA at this environment. **Leave unset in production** — every listed origin can frame the proxy. Prefer a one-off targeted `cdk deploy '*McpSandboxStack*'` with this var exported over committing it as a CI variable, so localhost never lands in automated shared-env deploys.

The sandbox origin is intentionally a sibling subdomain (not the SPA origin), matching the Artifacts pattern: it is the **outer** half of the spec's Sandbox Proxy pattern, giving a stable cross-origin boundary so the inner MCP App content frame's `allow-same-origin` never reaches the SPA's cookies/`localStorage`/app API. When this stack is enabled, the Inference API conditionally consumes its `/mcp-sandbox/origin` SSM export into `AGENTCORE_MCP_APPS_SANDBOX_ORIGIN` and surfaces it on the `ui_resource` SSE event so the SPA knows where to frame an App. The MCP Apps host surface is now on by default (`AGENTCORE_MCP_APPS_HOST_ENABLED=true` since PR #7), but **stays dormant until an MCP-Apps-capable server is registered** in the tool catalog — see [Register an MCP-Apps-capable MCP server](#register-an-mcp-apps-capable-mcp-server) below. If you skip this stack the surface stays dormant regardless, because the SPA has no proxy origin to frame an App in.

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

## Register an MCP-Apps-capable MCP server

The MCP Apps host renderer (`docs/kaizen/scoping/mcp-apps-host-renderer.md`) is **on by default** (`AGENTCORE_MCP_APPS_HOST_ENABLED=true` since PR #7). But it stays completely dormant until you register an MCP-Apps-capable MCP server in the tool catalog — there is no built-in MCP App. Registration is a normal **tool-catalog** operation (DynamoDB, via the admin API); it is *not* a code constant or part of bootstrap seeding, so each environment opts in explicitly.

### What "MCP-Apps-capable" means

A server is MCP-Apps-capable when, on top of being a normal external MCP server, it:

- advertises `_meta.ui` on its `tools/list` entries (a `resourceUri` of the form `ui://…` plus a `visibility` that includes `app`), and
- serves that `ui://` resource via `resources/read` as `text/html;profile=mcp-app`.

Our host advertises the `io.modelcontextprotocol/ui` extension on every outbound MCP `initialize` automatically (Gateway + external clients) — the server side needs no per-server opt-in here. Servers that don't speak the extension simply ignore it and behave as plain MCP tools.

### Prerequisites

1. **MCP Sandbox stack deployed** (`CDK_MCP_SANDBOX_ENABLED=true`, see *Deploy MCP Sandbox (Optional)* under [What Each Workflow Does](#what-each-workflow-does)). Without it the Inference API has no `/{prefix}/mcp-sandbox/origin` SSM value to consume, `AGENTCORE_MCP_APPS_SANDBOX_ORIGIN` stays empty, and the SPA has no cross-origin shell to frame the App in — every other piece can be wired and the App still won't render.
2. **An MCP-Apps server reachable over Streamable HTTP.** External MCP tools connect via the Streamable-HTTP client (not stdio). Run the example server in HTTP mode (below).
3. **Admin access** to the running App API (admin session cookie; the tool admin endpoints chain through `require_admin`).

### Step 1 — Run the example server (dogfood)

We dogfood with [`modelcontextprotocol/ext-apps` → `budget-allocator-server`](https://github.com/modelcontextprotocol/ext-apps/tree/main/examples/budget-allocator-server) — a form-style App (sliders, presets, benchmarks) that exercises `ui/update-model-context` and app-initiated `tools/call` without any 3D/charting backend infra. `scenario-modeler-server` works the same way if you prefer it.

```bash
git clone https://github.com/modelcontextprotocol/ext-apps
cd ext-apps/examples/budget-allocator-server
npm install
npm run start:http          # stateless Streamable HTTP on http://localhost:3001/mcp
```

(Override the port with `PORT=…`. The README's default config is stdio — use `start:http`, since external MCP tools here are Streamable-HTTP only.)

### Step 2 — Register it in the tool catalog

Either use the **Admin UI** (Settings → Tools → Add tool → protocol *MCP (external)*) or the admin API directly. A ready-to-POST body is committed at [`docs/kaizen/scoping/mcp-apps-budget-allocator.tool.json`](../../../docs/kaizen/scoping/mcp-apps-budget-allocator.tool.json).

Optionally pre-flight discovery (lists the server's tool names so you can confirm reachability/auth before creating the catalog entry):

```bash
curl -X POST "$APP_API/admin/tools/discover" \
  -H 'Content-Type: application/json' --cookie "$ADMIN_COOKIE" \
  -d '{"serverUrl":"http://localhost:3001/mcp","transport":"streamable-http","authType":"none"}'
```

Then create the catalog entry:

```bash
curl -X POST "$APP_API/admin/tools/" \
  -H 'Content-Type: application/json' --cookie "$ADMIN_COOKIE" \
  -d @docs/kaizen/scoping/mcp-apps-budget-allocator.tool.json
```

`authType: none` is only appropriate for an unauthenticated local/dev server. A real deployed MCP-Apps server uses `aws-iam` (Lambda Function URL / API Gateway behind SigV4), `api-key` (+ `secretArn`), or `oauth2` — exactly like any other external MCP tool. After creating the tool, grant it to the relevant App roles (Settings → Tools → Roles, or the `/admin/tools/{id}/roles` endpoints); a freshly created tool is in the catalog but not yet visible to any role.

### Step 3 — Local-dev environment

When running the backend locally (App API + Inference API), set in `backend/src/.env` (template: `backend/src/.env.example`):

| Var | Value | Why |
|-----|-------|-----|
| `AGENTCORE_MCP_APPS_HOST_ENABLED` | `true` | Default; set `false` to opt this env out entirely. |
| `AGENTCORE_MCP_APPS_SANDBOX_ORIGIN` | the deployed `mcp-sandbox.{domain}` origin (SSM `/{prefix}/mcp-sandbox/origin`) | The agent puts this on the `ui_resource` SSE event as `sandboxOrigin`; the SPA frames the App in it. Empty ⇒ no render. There is no local sandbox shell — point at a deployed one. |

For the local SPA (`http://localhost:4200`) to be allowed to embed that deployed sandbox origin, the sandbox stack must list it in CSP `frame-ancestors` — deploy McpSandbox once with `CDK_MCP_SANDBOX_EXTRA_FRAME_ANCESTORS=http://localhost:4200` (one-off targeted deploy; never commit localhost as a shared CI variable — see *Deploy MCP Sandbox (Optional)* under [What Each Workflow Does](#what-each-workflow-does)).

### Step 4 — Verify end-to-end

In a chat, prompt the agent so it invokes the registered tool. You should see, in order: a `tool_use`/`tool_result` card, then the App's iframe rendering inside it (`ui_resource` SSE event carrying a non-empty `sandboxOrigin`). Driving the form should push `ui/notifications/tool-input`, app-initiated buttons should round-trip through `tools/call`, and an `ui/update-model-context` write should be visible to the model on the **next** turn. If the iframe stays blank, the `sandboxOrigin` is almost always empty (prerequisite 1) or the SPA origin isn't in the sandbox `frame-ancestors` (Step 3).

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
