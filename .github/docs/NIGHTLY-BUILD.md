# Nightly Build & Test Workflow

## Overview

The nightly workflow validates the AgentCore Public Stack through configurable tracks — backend tests, frontend tests, full-stack deploys, and merge validation. It runs every night at 2 AM Mountain Time (9 AM UTC) and can be triggered manually.

**Fork safety**: If the `NIGHTLY_TRACKS` repository variable is not set, no tracks run. Forked repos are safe by default.

## Track System

### Track Vocabulary

Tracks are specified as a comma-separated string in the `NIGHTLY_TRACKS` repo variable (for scheduled runs) or the `tracks` input (for manual runs).

| Track | Description |
|-------|-------------|
| `test-backend-<branch>` | Run backend tests against `<branch>` |
| `test-frontend-<branch>` | Run frontend tests against `<branch>` |
| `deploy-<branch>` | Deploy full stack from `<branch>` with automatic teardown |
| `merge-validation:<base>:<overlay>` | Deploy `<base>`, then overlay `<overlay>` on top (colons delimit to avoid branch name ambiguity) |
| `all` | Run all tracks with defaults: tests + deploy on `develop`, MV `main`→`develop` |

### Examples

```
test-backend-develop
test-frontend-main,test-backend-main
deploy-develop
deploy-main,deploy-develop
merge-validation:main:develop
merge-validation:main:feature/my-branch
test-backend-develop,deploy-develop,merge-validation:main:develop
all
```

### Track Resolution

The `resolve-tracks` job parses the tracks string into boolean flags and branch refs:

- **Scheduled runs**: Read `vars.NIGHTLY_TRACKS`
- **Manual runs (`workflow_dispatch`)**: Use the `tracks` input only, ignoring the variable
- **Empty/unset**: Nothing runs

## Workflow Jobs

### resolve-tracks
Parses the tracks string and outputs boolean flags (`run_test_backend`, `run_test_frontend`, `run_deploy`, `run_mv`) and branch refs for each enabled track.

### Test Tracks

When `test-backend-<branch>` or `test-frontend-<branch>` is specified:

1. **install-backend / install-frontend**: Install and cache dependencies
2. **test-backend / test-frontend**: Run test suites with coverage, upload artifacts
3. **analyze-coverage**: Compare coverage against previous baseline (runs if any test succeeded)
4. **ai-coverage-analysis**: Uses GitHub Models API (GPT-4o) to analyze coverage gaps and create/update GitHub issues labeled `test-coverage` + `nightly-build`

### Deploy Track

When `deploy-<branch>` is specified, calls the reusable `nightly-deploy-pipeline.yml` with:
- `project-prefix`: `nightly-<branch>`
- `alb-subdomain`: `nightly-<branch>-api`
- Automatic teardown (unless `skip_teardown` is set)

The deploy pipeline runs: install-infra → check-stack-deps → deploy-infra → deploy-rag → deploy-inference → deploy-app → deploy-frontend + deploy-gateway → smoke-test → teardown.

### Merge Validation Track

When `merge-validation:<base>:<overlay>` is specified:

1. **mv-base**: Deploys `<base>` branch with `nightly-mv` prefix, `skip-teardown: true`. Uses `source-project-prefix: dev-boisestateai-v2` for Docker image promotion (promote-or-build pattern).
2. **mv-overlay**: Deploys `<overlay>` branch on top of the same `nightly-mv` stack, then tears down.

This simulates a real merge to catch CDK/infra incompatibilities between branches.

### Summary

Generates a GitHub Actions job summary table showing the status of all enabled tracks.

## Deploy Pipeline (`nightly-deploy-pipeline.yml`)

Reusable workflow (`workflow_call`) containing the full deploy pipeline.

### Inputs

| Input | Required | Description |
|-------|----------|-------------|
| `ref` | yes | Git ref to deploy |
| `project-prefix` | yes | CDK project prefix (e.g., `nightly-develop`) |
| `alb-subdomain` | yes | ALB subdomain for the deployment |
| `skip-teardown` | no | Skip teardown (default: `false`) |
| `label` | no | Label for job names |
| `source-project-prefix` | no | If set, Docker jobs try ECR image promotion before building |

### Promote-or-Build Pattern

When `source-project-prefix` is provided, Docker jobs (rag-ingestion, inference-api, app-api) attempt to promote existing images from the source ECR before falling back to a full build. This avoids unnecessary Docker builds when images haven't changed.

## Manual Triggers

Go to: **Actions** → **Nightly Build & Test** → **Run workflow**

| Input | Default | Description |
|-------|---------|-------------|
| `tracks` | `all` | Comma-separated tracks to run |
| `skip_teardown` | `false` | Leave resources deployed for debugging |

## Configuration

### Repository Variable

Set `NIGHTLY_TRACKS` in your repo's **Settings → Secrets and variables → Actions → Variables** tab.

Example values:
- `all` — full nightly suite
- `test-backend-develop,test-frontend-develop` — tests only
- `deploy-main` — deploy main branch only
- *(empty/unset)* — nothing runs (fork-safe)

### Environment

Deploy and MV tracks use the `development` GitHub environment with overrides:
- `CDK_RETAIN_DATA_ON_DELETE=false` (enables clean teardown)
- Minimal resource sizing

### Required Secrets
- `AWS_ROLE_ARN` or `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY`
- `GITHUB_TOKEN` (automatically provided)

### Required Variables
- `AWS_REGION`, `CDK_AWS_ACCOUNT`
- All CDK configuration variables from the development environment

## Artifacts

| Artifact | Retention | Condition |
|----------|-----------|-----------|
| `backend-coverage` | 30 days | Backend test track enabled |
| `frontend-coverage` | 30 days | Frontend test track enabled |
| `coverage-comparison` | 30 days | Any test track succeeded |

## Troubleshooting

### No tracks run on schedule
Check that `NIGHTLY_TRACKS` is set as a repository variable (not a secret). Empty or unset = nothing runs.

### Deploy fails
- Check AWS credentials in the development environment
- Verify CDK variables are set
- Review CloudFormation stack events in AWS Console

### Teardown fails
- S3 buckets may need manual emptying
- Run `cd infrastructure && npx cdk destroy --all --force` manually if needed

### Coverage analysis fails
- Ensure test jobs uploaded coverage artifacts
- Check Python script logs for errors

### Merge validation fails on overlay
This is the intended signal — it means the overlay branch has CDK/infra incompatibilities with the base branch that need to be resolved before merging.
