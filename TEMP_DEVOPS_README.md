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

### F. Repo-Specific Gotchas (Read Before You Lose Time)

* **Token-safe imports**: Use `Vpc.fromVpcAttributes()` (not `fromLookup()`) when importing VPC details that come from SSM tokens.
* **AgentCore CLI**: Use `aws bedrock-agentcore-control ...` for Gateway control-plane calls; gateway target lists are under `.items[]`.
* **SSM overwrite**: `aws ssm put-parameter --overwrite` cannot be used with `--tags` for an existing parameter.
