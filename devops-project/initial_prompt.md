# Mission: Design AWS CDK Multi-Stack Infrastructure with Platform-Agnostic CI/CD

## Primary Goal
Create a comprehensive, actionable implementation plan for a 5-stack AWS CDK application with GitHub Actions-based CI/CD that maintains platform portability through shell script abstraction.

---

## Stack Architecture Requirements

### Stack Definitions
Each stack must leverage **CloudFormation Outputs** and **AWS Systems Manager Parameter Store** for cross-stack data sharing.

1. **Frontend Stack**
   - **AWS Resources**: S3 (static hosting), CloudFront (CDN), Route53 (DNS)
   - **Source Code**: `/frontend` directory (Angular application)
   - **Pipeline Trigger**: Changes to `/frontend/**`

2. **App API Stack**
   - **AWS Resources**: VPC, Subnets, Application Load Balancer, ECS Fargate, RDS/DynamoDB
   - **Source Code**: `backend/src/apis/app_api`
   - **Pipeline Trigger**: Changes to `backend/src/apis/app_api/**`

3. **Inference API Stack**
   - **AWS Resources**: Shared VPC/ALB from Stack 2 + dedicated ECS Fargate tasks/images
   - **Source Code**: `backend/src/apis/inference_api`
   - **Pipeline Trigger**: Changes to `backend/src/apis/inference_api/**`
   - **Dependencies**: Must reference Stack 2's VPC/ALB via Parameter Store or CFN Outputs

4. **Agent Core Stack**
   - **AWS Resources**: Managed service infrastructure for agent deployment
   - **Source Code**: `backend/src/agents`
   - **Pipeline Trigger**: Changes to `backend/src/agents/**`

5. **Gateway & MCP Stack**
   - **AWS Resources**: AgentCore Gateway configuration (initial scope)
   - **Pipeline Trigger**: Changes to CDK infrastructure code for this stack

---

## CI/CD Architecture: Platform-Agnostic Pattern

### Core Principle
GitHub Actions orchestrates workflows but **contains zero inline logic**. All build/test/deploy operations must execute via standalone shell scripts.

### Implementation Requirements
- **5 separate pipelines** (one per stack)
- **Runner**: `ubuntu-latest` (GitHub-hosted)
- **Dependency Installation**: Each pipeline must install required tooling (Node.js, CDK, Python, Docker, etc.)
- **Script Invocation Pattern**:
  ```yaml
  - name: Build Frontend
    run: ./scripts/build-frontend.sh
  ```
- **Portability Goal**: Scripts must run identically on:
  - Developer local machines
  - GitHub Actions
  - Azure DevOps / GitLab CI (future)

### Path-Based Triggers
Each pipeline activates only when changes occur in its respective directory (see Stack Definitions above).

---

## Local Orchestration Requirement

Design an **interactive top-level shell script** (e.g., `deploy.sh`) that:
- Presents a menu of deployment options (individual stacks or "deploy all")
- Reuses the same shell scripts used by GitHub Actions
- Allows local execution without GitHub Actions infrastructure

---

## Deliverables (3 Files)

### 1. `THE_PLAN.md`
**Purpose**: Comprehensive step-by-step implementation checklist.

**Required Sections**:
- **CDK Stack Design**: Detailed breakdown of constructs, resource dependencies, and Parameter Store/CFN Output strategy
- **Shell Script Inventory**: Complete list of required scripts (e.g., `build-frontend.sh`, `test-app-api.sh`, `deploy-inference-api.sh`)
- **GitHub Actions Workflows**: Structure of 5 YAML files with dependency installation steps and script invocations
- **Local Orchestration Script**: Design of interactive deployment menu and script logic
- **Task Status Tracking**: Use checkboxes (`- [ ]` / `- [x]`) to track completion

**Task Ordering Strategy**: 
Work **sequentially by stack** rather than by job type. Complete each stack's CDK infrastructure AND build/deploy pipeline before moving to the next stack. This allows for incremental testing of each stack in isolation.

**Preferred Order**:
1. Design Stack 1 (Frontend) CDK infrastructure
2. Design Stack 1 (Frontend) build/deploy pipeline
3. Design Stack 2 (App API) CDK infrastructure
4. Design Stack 2 (App API) build/deploy pipeline
5. Design Stack 3 (Inference API) CDK infrastructure
6. Design Stack 3 (Inference API) build/deploy pipeline
7. Design Stack 4 (Agent Core) CDK infrastructure
8. Design Stack 4 (Agent Core) build/deploy pipeline
9. Design Stack 5 (Gateway & MCP) CDK infrastructure
10. Design Stack 5 (Gateway & MCP) build/deploy pipeline
11. Design local orchestration script (integrates all stacks)

**Format**: Sequential, granular tasks that can be executed independently.

---

### 2. `CLAUDES_INSTRUCTIONS.md`
**Purpose**: System instructions for the coding agent (Claude Sonnet) that will execute `THE_PLAN.md`.

**Required Content**:
- **Project Overview**: 5-stack architecture + platform-agnostic CI/CD strategy
- **Execution Model**: Agent must read `THE_PLAN.md` → identify next pending task → complete task → mark as done
- **Constraints**:
  - **DO NOT** create new Markdown documentation files unless explicitly requested
  - **ONLY** update `README.md` to correct factual inaccuracies
  - **MUST** update `THE_PLAN.md` checkboxes immediately after completing each task
- **Workflow**: Agent is responsible for maintaining task status in `THE_PLAN.md`

---

### 3. `PROMPT_TEMPLATE.md`
**Purpose**: The final prompt to send to the coding agent with all context.

**Required Structure**:
```markdown
# Task: Implement AWS CDK Infrastructure and CI/CD

## Context Files
1. **THE_PLAN.md**: Your implementation checklist
2. **CLAUDES_INSTRUCTIONS.md**: Your operational constraints

## Instructions
1. Open and read `THE_PLAN.md`
2. Identify the next unchecked task
3. Complete the task (write code, create files, etc.)
4. Update `THE_PLAN.md` to mark the task as complete (`- [x]`)
5. Repeat until all tasks are done

## Critical Rules
- Follow the constraints in `CLAUDES_INSTRUCTIONS.md`
- Do not skip tasks or batch completions
- Verify your work before marking tasks complete
- Update `THE_PLAN.md` after EVERY completed task

Begin by reading `THE_PLAN.md` and starting with the first pending task.
```

---

## Planning Agent Instructions

Your task is to **generate all three files** (`THE_PLAN.md`, `CLAUDES_INSTRUCTIONS.md`, `PROMPT_TEMPLATE.md`) based on this specification. Ensure:
- `THE_PLAN.md` is granular, sequential, and comprehensive
- `CLAUDES_INSTRUCTIONS.md` clearly defines agent behavior and constraints
- `PROMPT_TEMPLATE.md` provides a clear entry point for the coding agent