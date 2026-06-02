---
inclusion: fileMatch
fileMatchPattern: [".github/actions/*", ".github/workflows/*", "scripts/*", "**/*.sh", "infrastructure/*"]
---
# DevOps & Infrastructure Guide

This document provides a concise overview of the CI/CD pipelines, Infrastructure as Code (IaC) architecture, and critical development rules for the AgentCore Public Stack.

## 0. How to Jump In (Fast)

When you're debugging a deploy or adding a stack, start here in this order:

1. **Workflow**: `.github/workflows/<stack>.yml` shows what runs in CI and when.
2. **Scripts**: `scripts/<name>/` contains the actual build/test/deploy logic (YAML should be a thin wrapper). Common shared utilities live in `scripts/common/`; the content-hash Docker build pipeline lives in `scripts/build/`.
3. **CDK Stack**: `infrastructure/lib/<stack>-stack.ts` defines the AWS resources.

Rule of thumb: if you're looking for "what does this job do?", it's almost always in `scripts/`, not the workflow YAML.

## 1. GitHub Actions Workflows

The project uses a modular workflow architecture located in `.github/workflows/`. Each stack has its own dedicated workflow following a "Shell Scripts First" philosophy—logic resides in `scripts/`, not in YAML files.

### Workflow Architecture
The project employs a **Modular, Job-Centric Architecture** designed for parallelism and clear failure isolation. All workflows follow these core principles:

1.  **Single Responsibility Jobs**: Each job performs exactly one major task (e.g., `build-docker`, `synth-cdk`, `test-python`). This makes debugging easier and allows for granular retries.
2.  **Parallel Execution Tracks**: Independent processes run concurrently. For example, Docker images are built and pushed while the CDK code is simultaneously synthesized and diffed.
3.  **Artifact-Driven Handover**: Jobs do not share state. Instead, they produce immutable artifacts (Docker image tarballs, synthesized CloudFormation templates) that are uploaded and then downloaded by downstream jobs.
4.  **Script-Based Logic**: Workflows are thin wrappers around shell scripts. Every step calls a script in `scripts/<name>/`, ensuring that CI logic can be reproduced locally.

### Workflow Invariants (Assume These Are True)

These conventions are relied on throughout the repo and are the fastest way to reason about the pipelines:

* **Job isolation is real**: each job starts on a fresh runner. If a downstream job needs something, it must come from an artifact (or from AWS).
* **Docker images move via artifacts**: images are exported as tar artifacts and loaded in later jobs (do not assume a prior job’s Docker cache exists).
* **CDK is “synth once”**: templates are synthesized to `cdk.out/` and deploy steps should reuse them when present.
* **YAML is the table of contents**: any non-trivial logic belongs in `scripts/`.

### Available Workflows
*   **`platform.yml`**: Deploys the unified PlatformStack — the only stack now. Runs CFN updates for infrastructure changes (new tables, new IAM grants, network config, etc.). After Phases 5+6 of the platform-as-bootstrap refactor land, this is the *only* workflow that touches CFN; all other code/image deploys go through AWS APIs directly.
*   **`backend.yml`**: Backend code-deploy entry point. After Phases 5+6 of the platform-as-bootstrap refactor, this workflow runs **only** API-driven deploys — there's no `cdk deploy` step. The shape is:
    *   Test gates (`test-infra`, `test-backend`).
    *   Per-image builds (`build-app-api`, `build-inference-api`, `build-rag-ingestion`) — all gate on `test-backend`, push to ECR with content-hash tags, write tags to SSM.
    *   Per-service code deploys, all gating on `test-backend` and (for image deploys) the matching `build-*` job:
        *   `deploy-artifact-render-code` — `aws lambda update-function-code` for the artifact-render zip Lambda.
        *   `deploy-rag-ingestion-code` — `aws lambda update-function-code --image-uri` for the RAG-ingestion image Lambda.
        *   `deploy-inference-api-code` — `aws bedrock-agentcore-control update-agent-runtime` for the AgentCore Runtime.
        *   `deploy-app-api-code` — `aws ecs register-task-definition` + `update-service` + `wait services-stable` for the App API Fargate service.
*   **`frontend-deploy.yml`**: Builds the Angular SPA and syncs it to the SPA bucket created by PlatformStack.
*   **`teardown.yml`**: Manually-triggered. Calls `scripts/teardown/destroy.sh` to delete every CloudFormation stack with the project prefix (uses `aws cloudformation delete-stack`, not `cdk destroy`, so it works for both the current single-stack architecture and any legacy 2-stack or 9-stack deployments).
*   **`nightly-deploy-pipeline.yml`**: Combined platform → backend → frontend → smoke/E2E test pipeline that runs nightly.

---

## 2. CDK Stack (Infrastructure)

After Phase 7 of the platform-as-bootstrap refactor, the infrastructure is a single `PlatformStack` defined in `infrastructure/lib/platform-stack.ts`. Every resource — data, edge, AgentCore, compute — lives there. There are no cross-stack references to manage and no second stack to deploy in a particular order.

| Stack Name | Class | Description |
| :--- | :--- | :--- |
| **Platform** | `PlatformStack` | All resources: VPC, ALB, ECS cluster + Fargate App API service, security groups, every DynamoDB table, every S3 bucket (SPA, RAG documents, RAG vectors, file uploads, mcp-sandbox, fine-tuning data, artifacts content), Cognito, KMS keys, secrets, AgentCore Memory/Gateway/Code-Interpreter/Browser/Runtime, CloudFront distributions (SPA + mcp-sandbox + artifacts), Route53 aliases, RAG ingestion Lambda, artifact render Lambda + distribution, SageMaker fine-tuning IAM. Construction is split across the constructor (data + edge + AgentCore Memory/CI/Browser/Gateway), `wireSpaDistribution()` (SPA + RAG-CORS updater), and `wireCompute()` (Inference Runtime + SageMaker + App API). |

### Key Concepts
*   **No cross-stack SSM**: Single-stack means there are no `Fn::ImportValue` references to other stacks. SSM is still used for *runtime* lookups by container env-vars and by the workflow's API-driven code-deploy steps (e.g. `/${prefix}/artifacts/render-function-name` so the deploy script can find the auto-generated Lambda name).
*   **Same-stack SSM is still forbidden**: `valueForStringParameter` resolves a CFN template parameter before any of the stack's resources are created, so reading a parameter that this same stack would publish is unsatisfiable on first deploy. Pass values via construct refs or function args inside the same stack.
*   **Context Configuration**: Project prefix, account IDs, regions, and any tunables are passed via CDK Context, never hardcoded.

### Deployment Order
*   **Single stack**: just `cdk deploy <prefix>-PlatformStack`. The workflow's `deploy` job in either `platform.yml` or `backend.yml` runs the same script (`scripts/platform/deploy.sh`).
*   **Code deploys** for artifact-render and rag-ingestion Lambdas don't go through CFN at all — they're API calls directly from the `deploy-artifact-render-code` and `deploy-rag-ingestion-code` jobs.
*   **App API and Inference API** also use the bootstrap-container pattern (Phases 5+6 of the refactor). The CDK construct ships a stable bootstrap container at synth time; the workflow ships the real image via `aws bedrock-agentcore-control update-agent-runtime` (inference-api) or `aws ecs register-task-definition` + `aws ecs update-service` (app-api). No CFN deploy is needed for backend code changes.

### The Platform-as-Bootstrap Pattern

Several Lambdas in PlatformStack don't ship their real code via CDK. CDK ships a small, byte-stable "bootstrap" placeholder; the workflow ships the real handler via `aws lambda update-function-code`:

*   **artifact-render** (zip Lambda): bootstrap at `infrastructure/bootstrap-assets/artifact-render/handler.py` (returns 503). Workflow's `deploy-artifact-render-code` step zips `backend/src/lambdas/artifact_render/`, hashes it, calls `update-function-code` if the hash differs from the SSM-tracked latest.
*   **rag-ingestion** (image Lambda): bootstrap at `infrastructure/bootstrap-assets/rag-ingestion/` (Dockerfile + handler.py, base image digest-pinned). Workflow's `deploy-rag-ingestion-code` step calls `update-function-code --image-uri` with the image tag the build job just pushed.

Why it works: CFN tracks the Lambda's `Code` property from its own model, not by querying live AWS. With a byte-stable bootstrap asset, the CDK-computed S3Key/digest is constant across synths, so CFN sees no change to the `Code` property on subsequent Platform deploys and leaves the out-of-band-deployed real code untouched. (Drift detection would surface this if anyone ran it manually, but normal stack updates leave the Lambda alone.)

The same pattern applies to two container-based services:

*   **inference-api** (AgentCore Runtime): bootstrap at `infrastructure/bootstrap-assets/inference-api/` (Dockerfile + handler.py — stdlib HTTP server on port 8080 with `/ping` for AgentCore's health check). Workflow's `deploy-inference-api-code` step calls `aws bedrock-agentcore-control update-agent-runtime` with the new image URI; polls for `READY` state before/after.
*   **app-api** (ECS Fargate task): bootstrap at `infrastructure/bootstrap-assets/app-api/` (Dockerfile + handler.py — stdlib HTTP server on port 8000 with `/health` for ALB target group health checks). Workflow's `deploy-app-api-code` step does `aws ecs register-task-definition` (mutates the live task def's `containerDefinitions[0].image`, registers a new revision of the same family) → `aws ecs update-service` → `aws ecs wait services-stable`.

Container HEALTHCHECK note: the App API construct's container-level `healthCheck` uses `python3 -c '...'` (stdlib `urllib.request`) rather than `curl`, so both bootstrap (no curl) and real (Python-based, has curl) images pass the same health probe.

---

## 3. Critical Development Rules

Follow these rules when adding or modifying stacks to ensure stability and maintainability.

### A. Configuration Management
*   **NEVER Hardcode**: Account IDs, Regions, ARNs, or resource names.
*   **Use SSM**: Store dynamic values (like Docker image tags or VPC IDs) in SSM Parameter Store.
*   **Hierarchy**: Environment Variables > CDK Context > Defaults.

#### Decision Tree: Where Should This Value Live?

**Use `config.ts` + `cdk.context.json` when:**
- Value is needed **at CDK resource creation time**
- Examples: CORS origins (for S3 bucket CORS rules), CPU/memory (for ECS task definitions), max file size (for bucket policies)

**Use ECS/Lambda `environment` block when:**
- Value is needed **at runtime by application code**
- Resource is in the **same stack** as the service
- Examples: DynamoDB table names, S3 bucket names, API URLs
- Application reads via `os.getenv("TABLE_NAME")` in Python

**Use SSM Parameter Store when:**
- Value is needed **at runtime by application code** running in ECS/Lambda
- Examples: table names, bucket names, memory IDs — anything the Python/Node app reads via `os.getenv()` at startup
- Published by CDK constructs so the running container can discover its dependencies
- **Never** use this for same-stack CDK-to-CDK reads. CloudFormation resolves
  `AWS::SSM::Parameter::Value<String>` template parameters before any
  of the stack's resources are created, so reading a parameter that
  this same stack would publish is unsatisfiable on first deploy.
  Pass values via construct refs or function args inside a single stack.


### B. Scripting & Automation
*   **Shell Scripts First**: GitHub Actions YAML should **ONLY** call scripts in `scripts/`.
*   **Portability**: Scripts must run locally and in CI. Use `set -euo pipefail` for error handling.
*   **Naming**: Scripts follow the pattern `scripts/<name>/<operation>.sh` (e.g., `scripts/platform/deploy.sh`, `scripts/backend/deploy.sh`).

### C. Deployment Safety
*   **Synth Once, Deploy Anywhere**: Synthesize CloudFormation templates in the `synth` job/step. The `deploy` step must use the generated `cdk.out/` artifacts, not re-synthesize.
*   **Docker Artifacts**: Build Docker images once. Export them as `.tar` files to pass between CI jobs. Never rebuild the same image in a later stage.

### D. Resource Referencing
*   **Importing Resources**: When importing resources (VPC, Cluster, ALB) in a consumer stack, use `fromAttributes` methods (e.g., `Vpc.fromVpcAttributes`), not `fromLookup`. This avoids environment-dependent token issues.

### E. When Adding/Modifying a Stack (Minimal Checklist)

* **CDK**: Add/update `infrastructure/lib/<your-stack>.ts` and wire it in `infrastructure/bin/infrastructure.ts`.
* **SSM I/O**: Export shared values via SSM with the `/${projectPrefix}/...` convention; import via SSM in dependent stacks.
* **Scripts**: Add a `scripts/<name>/` folder and keep scripts single-purpose (install/build/synth/test/deploy as needed).
* **Workflow**: Add/update `.github/workflows/<stack>.yml` so it only calls scripts (no inline logic).
* **Context discipline**: Keep context flags consistent between `synth.sh` and `deploy.sh` for the same stack.

### F. Adding New Configuration Properties

When adding a new configuration value that flows from GitHub Actions through to CDK stacks, follow this 7-step pattern:

#### Step 1: Add to TypeScript Config Interface

**File**: `infrastructure/lib/config.ts`

Add the property to `AppConfig` (or relevant sub-interface):

```typescript
export interface AppConfig {
  // ... existing properties
  certificateArn?: string; // ACM certificate ARN for HTTPS on ALB
}
```

#### Step 2: Load from Environment/Context

**File**: `infrastructure/lib/config.ts` (in `loadConfig` function)

Add environment variable and context fallback:

```typescript
const config: AppConfig = {
  // ... existing properties
  certificateArn: process.env.CDK_CERTIFICATE_ARN || scope.node.tryGetContext('certificateArn'),
};
```

**Naming Convention**: Use `CDK_` prefix for CDK-specific config, `ENV_` for runtime container environment variables.

#### Step 3: Use in CDK Stack

**File**: `infrastructure/lib/<stack-name>-stack.ts`

Access via the config object:

```typescript
if (config.certificateArn) {
  const certificate = acm.Certificate.fromCertificateArn(
    this,
    'Certificate',
    config.certificateArn
  );
  // Use certificate...
}
```

#### Step 4: Add to load-env.sh

**File**: `scripts/common/load-env.sh`

Add three things:

**a) Export the variable** (priority: env var > context file):
```bash
export CDK_CERTIFICATE_ARN="${CDK_CERTIFICATE_ARN:-$(get_json_value "certificateArn" "${CONTEXT_FILE}")}"
```

**b) Add to context parameters function** (if optional):
```bash
if [ -n "${CDK_CERTIFICATE_ARN:-}" ]; then
    context_params="${context_params} --context certificateArn=\"${CDK_CERTIFICATE_ARN}\""
fi
```

**c) Display in config output** (optional):
```bash
if [ -n "${CDK_CERTIFICATE_ARN:-}" ]; then
    log_info "  Certificate:    ${CDK_CERTIFICATE_ARN:0:50}..."
fi
```

#### Step 5: Update Stack Scripts

**Files**: `scripts/<name>/synth.sh` and `scripts/<name>/deploy.sh`

Add context parameter to both scripts (must match exactly):

```bash
cdk synth StackName \
    --context certificateArn="${CDK_CERTIFICATE_ARN}" \
    # ... other context params
```

```bash
cdk deploy StackName \
    --context certificateArn="${CDK_CERTIFICATE_ARN}" \
    # ... other context params
```

**Critical**: Context parameters must be **identical** in both `synth.sh` and `deploy.sh`.

#### Step 6: Add to GitHub Workflow

**File**: `.github/workflows/<stack>.yml`

Add to the `env:` section **at the job level** (NOT the workflow top-level). Environment-scoped variables (`vars.*`) and secrets (`secrets.*`) require the `environment:` key, which is set on individual jobs. Placing them at the workflow top-level will silently resolve to empty strings.

```yaml
jobs:
  deploy:
    environment: production
    env:
      # CDK Configuration - from GitHub Variables (MUST be at job level)
      CDK_ALB_SUBDOMAIN: ${{ vars.CDK_ALB_SUBDOMAIN }}
      
      # CDK Secrets - from GitHub Secrets
      CDK_CERTIFICATE_ARN: ${{ secrets.CDK_CERTIFICATE_ARN }}
```

**CRITICAL**: Only non-sensitive, non-environment-scoped values (like `CDK_REQUIRE_APPROVAL: never`) belong in the workflow-level `env:`. Everything that reads from `vars.*` or `secrets.*` MUST be in a job-level `env:` block on a job that has `environment:` set.

**When to use Secrets vs Variables:**
- **Secrets**: API keys, passwords, certificate ARNs, AWS credentials
- **Variables**: Project names, regions, non-sensitive config

#### Step 7: Set in GitHub Repository

**For Variables** (Settings → Secrets and variables → Actions → Variables):
```
CDK_ALB_SUBDOMAIN = api
```

**For Secrets** (Settings → Secrets and variables → Actions → Secrets):
```
CDK_CERTIFICATE_ARN = arn:aws:acm:us-east-1:123456789012:certificate/...
```

---

### Example: Certificate ARN Flow

Here's how `CDK_CERTIFICATE_ARN` flows through the system:

```
GitHub Secret (CDK_CERTIFICATE_ARN)
        ↓
.github/workflows/platform.yml (env section)
        ↓
scripts/common/load-env.sh (export CDK_CERTIFICATE_ARN)
        ↓
scripts/platform/synth.sh (--context certificateArn)
        ↓
infrastructure/lib/config.ts (loadConfig function)
        ↓
infrastructure/lib/platform-stack.ts (config.certificateArn)
        ↓
AWS CloudFormation Template (Certificate resource)
```

### Checklist for New Properties

- [ ] Add to `config.ts` interface
- [ ] Load from env/context in `config.ts` `loadConfig()`
- [ ] Use in CDK stack TypeScript file
- [ ] Export in `load-env.sh`
- [ ] Add to context params in `load-env.sh` (if applicable)
- [ ] Update `synth.sh` with context flag
- [ ] Update `deploy.sh` with context flag (must match synth.sh)
- [ ] Add to workflow YAML `env:` section
- [ ] Set GitHub Secret or Variable
- [ ] Test locally with environment variable
- [ ] Test in CI/CD pipeline

---

### G. Repo-Specific Gotchas (Read Before You Lose Time)

* **Token-safe imports**: Use `Vpc.fromVpcAttributes()` (not `fromLookup()`) when importing VPC details that come from SSM tokens.
* **AgentCore CLI**: Use `aws bedrock-agentcore-control ...` for Gateway control-plane calls; gateway target lists are under `.items[]`.
* **SSM overwrite**: `aws ssm put-parameter --overwrite` cannot be used with `--tags` for an existing parameter.
* **Context parameter mismatch**: If `synth.sh` and `deploy.sh` have different context parameters, deployment may use wrong values or fail validation.
* **Empty context values**: CDK context doesn't support `--context key=""` for empty strings; omit the flag entirely for optional parameters.
