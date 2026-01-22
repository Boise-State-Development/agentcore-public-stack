---
description: 'Agent to help with devops'
tools: ['runCommands', 'runTasks', 'edit', 'runNotebooks', 'search', 'new', 'extensions', 'usages', 'vscodeAPI', 'problems', 'changes', 'testFailure', 'openSimpleBrowser', 'fetch', 'githubRepo', 'todos', 'runSubagent', 'runTests']
---
# DevOps & Infrastructure Guide

This document provides a concise overview of the CI/CD pipelines, Infrastructure as Code (IaC) architecture, and critical development rules for the AgentCore Public Stack.

## 0. How to Jump In (Fast)

When you’re debugging a deploy or adding a stack, start here in this order:

1. **Workflow**: `.github/workflows/<stack>.yml` shows what runs in CI and when.
2. **Scripts**: `scripts/stack-<name>/` contains the actual build/test/deploy logic (YAML should be a thin wrapper).
3. **CDK Stack**: `infrastructure/lib/<stack>-stack.ts` defines the AWS resources.

Rule of thumb: if you’re looking for “what does this job do?”, it’s almost always in `scripts/`, not the workflow YAML.

## 1. GitHub Actions Workflows

The project uses a modular workflow architecture located in `.github/workflows/`. Each stack has its own dedicated workflow following a "Shell Scripts First" philosophy—logic resides in `scripts/`, not in YAML files.

### Workflow Architecture
The project employs a **Modular, Job-Centric Architecture** designed for parallelism and clear failure isolation. All workflows follow these core principles:

1.  **Single Responsibility Jobs**: Each job performs exactly one major task (e.g., `build-docker`, `synth-cdk`, `test-python`). This makes debugging easier and allows for granular retries.
2.  **Parallel Execution Tracks**: Independent processes run concurrently. For example, Docker images are built and pushed while the CDK code is simultaneously synthesized and diffed.
3.  **Artifact-Driven Handover**: Jobs do not share state. Instead, they produce immutable artifacts (Docker image tarballs, synthesized CloudFormation templates) that are uploaded and then downloaded by downstream jobs.
4.  **Script-Based Logic**: Workflows are thin wrappers around shell scripts. Every step calls a script in `scripts/stack-<name>/`, ensuring that CI logic can be reproduced locally.

### Workflow Invariants (Assume These Are True)

These conventions are relied on throughout the repo and are the fastest way to reason about the pipelines:

* **Job isolation is real**: each job starts on a fresh runner. If a downstream job needs something, it must come from an artifact (or from AWS).
* **Docker images move via artifacts**: images are exported as tar artifacts and loaded in later jobs (do not assume a prior job’s Docker cache exists).
* **CDK is “synth once”**: templates are synthesized to `cdk.out/` and deploy steps should reuse them when present.
* **YAML is the table of contents**: any non-trivial logic belongs in `scripts/`.

### Available Workflows
*   **`infrastructure.yml`**: Deploys the foundation (VPC, ALB, ECS Cluster). Runs first.
*   **`app-api.yml`**: Deploys the main application API (Fargate).
*   **`inference-api.yml`**: Deploys the inference runtime (Bedrock AgentCore Runtime).
*   **`frontend.yml`**: Deploys the Angular application (S3 + CloudFront).
*   **`gateway.yml`**: Deploys the Bedrock AgentCore Gateway and Lambda tools.

---

## 2. CDK Stacks (Infrastructure)

The infrastructure is defined in `infrastructure/lib/` and follows a strict layering model.

| Stack Name | Class | Description | Dependencies |
| :--- | :--- | :--- | :--- |
| **Infrastructure** | `InfrastructureStack` | **Foundation Layer**. Creates VPC, ALB, ECS Cluster, and Security Groups. Exports resource IDs to SSM. | None |
| **App API** | `AppApiStack` | **Service Layer**. Fargate service for the application backend. Imports network resources via SSM. | Infrastructure |
| **Inference API** | `InferenceApiStack` | **Service Layer**. Bedrock AgentCore Runtime which hosts the inference API. | Infrastructure |
| **Gateway** | `GatewayStack` | **Integration Layer**. AWS Bedrock AgentCore Gateway and Lambda-based MCP tools. | Infrastructure |
| **Frontend** | `FrontendStack` | **Presentation Layer**. S3 Bucket for assets and CloudFront Distribution. | Infrastructure |

### Key Concepts
*   **SSM Parameter Store**: Used for all cross-stack references (e.g., `/${projectPrefix}/network/vpc-id`).
*   **Context Configuration**: Project prefix, account IDs, and regions are passed via CDK Context (`cdk.json` or CLI flags), never hardcoded.

### Deployment Order & Layering Contract

* **Deploy order (default)**: Infrastructure → Gateway → App API → Inference API → Frontend.
* **Contract**: The Infrastructure stack is the foundation layer and exports shared IDs/attributes to SSM. All other stacks import those values from SSM.
* **No direct cross-stack coupling**: Prefer SSM parameters over CloudFormation cross-stack references to keep stacks independently deployable.

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
- Value is needed **by another stack** (cross-stack reference)
- Examples: VPC ID (InfrastructureStack → AppApiStack), ALB ARN
- Consumer stack reads via `ssm.StringParameter.valueForStringParameter()`


### B. Scripting & Automation
*   **Shell Scripts First**: GitHub Actions YAML should **ONLY** call scripts in `scripts/`.
*   **Portability**: Scripts must run locally and in CI. Use `set -euo pipefail` for error handling.
*   **Naming**: Scripts follow the pattern `scripts/stack-<name>/<operation>.sh` (e.g., `scripts/stack-app-api/deploy.sh`).

### C. Deployment Safety
*   **Synth Once, Deploy Anywhere**: Synthesize CloudFormation templates in the `synth` job/step. The `deploy` step must use the generated `cdk.out/` artifacts, not re-synthesize.
*   **Docker Artifacts**: Build Docker images once. Export them as `.tar` files to pass between CI jobs. Never rebuild the same image in a later stage.

### D. Resource Referencing
*   **Importing Resources**: When importing resources (VPC, Cluster, ALB) in a consumer stack, use `fromAttributes` methods (e.g., `Vpc.fromVpcAttributes`), not `fromLookup`. This avoids environment-dependent token issues.

### E. When Adding/Modifying a Stack (Minimal Checklist)

* **CDK**: Add/update `infrastructure/lib/<your-stack>.ts` and wire it in `infrastructure/bin/infrastructure.ts`.
* **SSM I/O**: Export shared values via SSM with the `/${projectPrefix}/...` convention; import via SSM in dependent stacks.
* **Scripts**: Add a `scripts/stack-<name>/` folder and keep scripts single-purpose (install/build/synth/test/deploy as needed).
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

**Files**: `scripts/stack-<name>/synth.sh` and `scripts/stack-<name>/deploy.sh`

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

Add to the `env:` section at workflow level:

- **Secrets** (sensitive data): Use `secrets.`
- **Variables** (non-sensitive config): Use `vars.`

```yaml
env:
  # CDK Configuration - from GitHub Variables
  CDK_ALB_SUBDOMAIN: ${{ vars.CDK_ALB_SUBDOMAIN }}
  
  # CDK Secrets - from GitHub Secrets
  CDK_CERTIFICATE_ARN: ${{ secrets.CDK_CERTIFICATE_ARN }}
```

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
.github/workflows/infrastructure.yml (env section)
        ↓
scripts/common/load-env.sh (export CDK_CERTIFICATE_ARN)
        ↓
scripts/stack-infrastructure/synth.sh (--context certificateArn)
        ↓
infrastructure/lib/config.ts (loadConfig function)
        ↓
infrastructure/lib/infrastructure-stack.ts (config.certificateArn)
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
