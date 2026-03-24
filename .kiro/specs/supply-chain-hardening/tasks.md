# Implementation Plan: Supply Chain Hardening

## Overview

Harden the CI/CD supply chain across GitHub Actions workflows, dependency manifests, Dockerfiles, and shell scripts. All changes are configuration-level — no application code is modified. Tasks are ordered so each builds on the previous, with property-based tests validating invariants after each group of changes.

## Tasks

- [x] 1. Pin GitHub Actions to SHA digests and standardize checkout version
  - [x] 1.1 Pin all third-party action references in workflow YAML files to SHA digests with version comments
    - For each workflow in `.github/workflows/*.yml` (13 files), replace every third-party `uses:` reference (e.g., `actions/checkout@v5`) with its SHA-256 digest plus version comment (e.g., `actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2`)
    - Standardize all `actions/checkout` references to the same SHA-pinned version across all 13 workflow files
    - Actions to pin: `actions/checkout`, `actions/cache/restore`, `actions/cache/save`, `actions/upload-artifact`, `actions/download-artifact`, `actions/setup-python`, `docker/setup-buildx-action`, `docker/build-push-action`, `aquasecurity/trivy-action` (added in task 5)
    - Leave the local composite action (`.github/actions/configure-aws-credentials`) referenced by relative path — exempt from SHA pinning
    - _Requirements: 1.1, 1.2, 1.4, 13.1_

  - [x] 1.2 Pin third-party actions inside the composite action to SHA digests
    - In `.github/actions/configure-aws-credentials/action.yml`, replace `aws-actions/configure-aws-credentials@v6` with its SHA digest plus version comment
    - _Requirements: 1.1_

  - [x] 1.3 Write property test for SHA pinning (Property 1)
    - **Property 1: Third-party actions are SHA-pinned with version comments**
    - Create `backend/tests/supply_chain/test_action_pinning.py`
    - Parse all workflow YAML files and the composite action, find all `uses:` values, verify each third-party reference matches `owner/action@<40-char-hex> # vX.Y.Z`
    - Verify local composite action references (starting with `./`) are exempt
    - **Validates: Requirements 1.1**

  - [x] 1.4 Write property test for consistent checkout SHA (Property 10)
    - **Property 10: Consistent checkout action SHA across all workflows**
    - In `backend/tests/supply_chain/test_action_pinning.py`, add test that extracts the SHA digest for `actions/checkout` from every workflow file and asserts they are all identical
    - **Validates: Requirements 13.1**

- [x] 2. Pin runner versions across all workflows
  - [x] 2.1 Replace all `ubuntu-latest` with `ubuntu-24.04` in workflow files
    - In all 13 workflow files under `.github/workflows/`, replace every `runs-on: ubuntu-latest` with `runs-on: ubuntu-24.04`
    - Jobs already using `ubuntu-24.04-arm` remain unchanged
    - _Requirements: 8.1_

  - [x] 2.2 Write property test for runner version pinning (Property 7)
    - **Property 7: No workflow job uses floating runner aliases**
    - Create `backend/tests/supply_chain/test_runner_pinning.py`
    - Parse all workflow YAML files, extract every `runs-on` value, assert none contain `-latest`
    - **Validates: Requirements 8.1**

- [x] 3. Checkpoint — Verify workflow YAML changes
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Pin Python dependencies to exact versions and fix mypy version
  - [x] 4.1 Replace all `>=`, `~=`, and unpinned versions in `pyproject.toml` with `==` exact pins
    - In `backend/pyproject.toml`, convert every dependency in `dependencies`, `[project.optional-dependencies].agentcore`, and `[project.optional-dependencies].dev` from floor pins (`>=`) to exact pins (`==`)
    - Keep the existing version numbers (e.g., `"boto3>=1.40.1"` → `"boto3==1.40.1"`)
    - After pinning, regenerate `uv.lock` with `uv lock`
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 4.2 Fix mypy `python_version` to match `requires-python`
    - In `backend/pyproject.toml`, change `[tool.mypy] python_version = "3.9"` to `python_version = "3.10"` to match `requires-python = ">=3.10"`
    - _Requirements: 12.1_

  - [x] 4.3 Write property test for Python dependency pinning (Property 2)
    - **Property 2: All Python dependencies use exact version pins**
    - Create `backend/tests/supply_chain/test_dependency_pinning.py`
    - Parse `pyproject.toml`, extract all dependency strings from all sections, verify each uses the `==` operator and none use `>=`, `~=`, `>`, `<`, or have no version constraint
    - **Validates: Requirements 2.1, 2.2, 2.4**

- [x] 5. Pin frontend and infrastructure npm dependencies to exact versions
  - [x] 5.1 Pin all frontend dependencies in `frontend/ai.client/package.json` to exact versions from lockfile
    - For each dependency in `dependencies` and `devDependencies`, look up the resolved version in `frontend/ai.client/package-lock.json` and replace the version string with the exact resolved version (no `^` or `~`)
    - Do NOT simply strip `^`/`~` — use the actual resolved version from the lockfile
    - Run `npm install` to regenerate `package-lock.json`, then verify with `npm ci`
    - _Requirements: 3.1, 3.2, 3.3_

  - [x] 5.2 Pin infrastructure dependencies in `infrastructure/package.json` to exact versions
    - Set `"aws-cdk-lib": "2.244.0"` and `"aws-cdk": "2.1113.0"` (target CDK versions)
    - Pin all other dependencies and devDependencies to exact versions from `infrastructure/package-lock.json` (no `^` or `~`)
    - Run `npm install` to regenerate `package-lock.json`, then verify with `npm ci`
    - _Requirements: 5.1, 5.2, 5.3_

  - [x] 5.3 Write property test for npm dependency pinning (Property 3)
    - **Property 3: All npm dependencies use exact version pins**
    - In `backend/tests/supply_chain/test_dependency_pinning.py`, add test that parses `frontend/ai.client/package.json` and `infrastructure/package.json`, checks every version string in `dependencies` and `devDependencies` does not start with `^`, `~`, `>`, `<`, or `*`
    - **Validates: Requirements 3.1, 3.2, 5.1, 5.2**

- [x] 6. Harden install scripts (global tool pinning + npm ci enforcement)
  - [x] 6.1 Pin CDK CLI version in install scripts
    - In `scripts/common/install-deps.sh`, change `npm install -g aws-cdk` to `npm install -g aws-cdk@2.1113.0`
    - In `scripts/stack-infrastructure/install.sh`, change `npm install -g aws-cdk` to `npm install -g aws-cdk@2.1113.0`
    - _Requirements: 4.1, 4.3_

  - [x] 6.2 Replace `npm install` with `npm ci` and add lockfile checks in install scripts
    - In `scripts/stack-infrastructure/install.sh`, replace `npm install` with `npm ci` and add a lockfile existence check that exits non-zero if `package-lock.json` is missing
    - In `scripts/stack-app-api/install.sh`, replace the `npm install` in the CDK dependencies section with `npm ci` and add a lockfile existence check
    - Use `scripts/stack-frontend/install.sh` as the reference pattern
    - _Requirements: 6.1, 6.2, 6.3_

  - [x] 6.3 Write property test for global npm install pinning (Property 4)
    - **Property 4: Global npm installs specify exact versions**
    - Create `backend/tests/supply_chain/test_script_hardening.py`
    - Scan all shell scripts under `scripts/` for `npm install -g` commands, verify each package includes an `@version` suffix
    - **Validates: Requirements 4.1, 4.3**

  - [x] 6.4 Write property test for npm ci enforcement (Property 5)
    - **Property 5: CI install paths use npm ci with lockfile check**
    - In `backend/tests/supply_chain/test_script_hardening.py`, add test that scans install scripts for npm dependency installation commands, verifies they use `npm ci` (not `npm install` for project deps), and include a lockfile existence check
    - **Validates: Requirements 6.1, 6.2**

- [x] 7. Checkpoint — Verify dependency and script changes
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Pin Docker apt-get/dnf package versions
  - [x] 8.1 Pin apt-get package versions in `Dockerfile.app-api` and `Dockerfile.inference-api`
    - In both builder stages, pin `gcc` and `g++` to exact versions available in the `python:3.13-slim` base image (Debian Bookworm)
    - In both production stages, pin `curl` to the exact version available in the base image
    - Query available versions by inspecting the base image's package repository
    - _Requirements: 10.1, 10.2_

  - [x] 8.2 Pin dnf package versions in `Dockerfile.rag-ingestion` where practical
    - In the builder stage, pin `gcc`, `gcc-c++`, `make`, `tar`, `gzip`, `ca-certificates`, `unzip` to versions available in the AL2023 Lambda base image
    - In the production stage, pin `mesa-libGL` and `glib2` to available versions
    - Where exact versions are unavailable or impractical on AL2023, add a comment documenting the constraint
    - _Requirements: 10.1, 10.2, 10.3_

  - [x] 8.3 Write property test for Dockerfile package pinning (Property 9)
    - **Property 9: Dockerfile apt-get packages have version pins**
    - Create `backend/tests/supply_chain/test_dockerfile_pinning.py`
    - Parse all Dockerfiles, find `apt-get install` and `dnf install` commands, verify every package name includes a version pin (`package=version` for apt-get, `package-version` for dnf) or has a comment documenting why the pin is omitted
    - **Validates: Requirements 10.1, 10.2**

- [x] 9. Add container image scanning as nightly track
  - [x] 9.1 Add `scan-images` track resolution to `nightly.yml`
    - In the `resolve-tracks` job, add a new `scan-images-*` case that sets `run_scan_images=true` and `scan_images_ref`
    - Add `run_scan_images` and `scan_images_ref` to the job outputs
    - Include `scan-images` in the `all` case with `scan_images_ref="develop"`
    - _Requirements: 7.1, 7.4_

  - [x] 9.2 Add `scan-images` job to `nightly.yml`
    - Add a new `scan-images` job that builds all three Docker images (app-api, inference-api, rag-ingestion) and runs Trivy against each
    - Use `aquasecurity/trivy-action` (SHA-pinned) with `exit-code: '0'` (advisory mode — does NOT fail the job)
    - Upload scan reports as artifacts with 30-day retention
    - The job runs in parallel with existing tracks, does NOT block any deploy
    - _Requirements: 7.1, 7.3, 7.4_

  - [x] 9.3 Add scan results to nightly summary job
    - Add `scan-images` to the `summary` job's `needs` list
    - Add a row to the summary report for the image scan track status
    - _Requirements: 7.1_

  - [x] 9.4 Write property test for nightly scan track (Property 6)
    - **Property 6: Nightly workflow includes image scanning track**
    - Create `backend/tests/supply_chain/test_docker_scanning.py`
    - Parse `nightly.yml`, verify `resolve-tracks` outputs include `run_scan_images`, verify a `scan-images` job exists that references all three Dockerfiles, uses `exit-code: '0'`, and uploads artifacts
    - **Validates: Requirements 7.1, 7.4**

- [x] 10. Scope secrets to AWS-using jobs only
  - [x] 10.1 Audit and move AWS credentials from workflow-level to job-level env blocks
    - Review all 13 workflow files for AWS credential variables (`AWS_ROLE_ARN`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`) in workflow-level `env:` blocks
    - Move any workflow-level AWS credentials to job-level `env:` blocks, only on jobs that perform AWS operations (configure-aws-credentials, ECR push, CDK deploy)
    - Ensure non-AWS jobs (install, build, test, lint) do not have AWS credentials in their env
    - _Requirements: 17.1, 17.2_

  - [x] 10.2 Write property test for secret scoping (Property 14)
    - **Property 14: AWS credentials scoped to AWS-using jobs only**
    - Create `backend/tests/supply_chain/test_secret_scoping.py`
    - Parse all workflow YAML files, verify no AWS credential variables appear in workflow-level `env:` blocks, and that job-level AWS credentials only appear on jobs containing AWS interaction steps
    - **Validates: Requirements 17.1, 17.2**

- [x] 11. Checkpoint — Verify Docker, scanning, and secret scoping changes
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. Create documentation files
  - [x] 12.1 Create `CONTRIBUTING.md` at repository root
    - Document prerequisites: Node.js 20+, Python 3.13+, Docker, AWS CLI v2, uv
    - Document clone and install steps for backend, frontend, and infrastructure
    - Document environment variable configuration (referencing `backend/src/.env` and `frontend/ai.client/src/environments/`)
    - Document how to run test suites: `uv run pytest` (backend), `npm test` (frontend), `npx cdk synth` (infrastructure)
    - Document AWS credential setup for local development
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5_

  - [x] 12.2 Create `.github/ARTIFACT_RETENTION.md`
    - Document retention periods by artifact type: Docker image tarballs (1 day), CDK synth templates (7 days), test results/coverage (7 days), deployment outputs (30 days), Trivy scan reports (30 days)
    - Verify all `retention-days` values in workflow files match the documented policy
    - _Requirements: 14.1, 14.2, 14.3_

  - [x] 12.3 Write property test for artifact retention consistency (Property 11)
    - **Property 11: Consistent artifact retention per artifact type**
    - Create `backend/tests/supply_chain/test_artifact_retention.py`
    - Parse all workflow files, find `upload-artifact` steps, group by artifact category, verify `retention-days` is consistent within each category
    - **Validates: Requirements 14.2**

  - [x] 12.4 Write property test for cancel-in-progress on deploy workflows (Property 12)
    - **Property 12: All deployment workflows retain cancel-in-progress false**
    - Create `backend/tests/supply_chain/test_concurrency_config.py`
    - Parse all workflow files that contain CDK deploy jobs, verify `concurrency.cancel-in-progress` is `false`
    - **Validates: Requirements 15.2**

  - [x] 12.5 Write unit tests for documentation and mypy version
    - Create `backend/tests/supply_chain/test_documentation.py`
    - Test that `CONTRIBUTING.md` exists and contains required sections (prerequisites, install steps, environment config, test suites, AWS credentials)
    - Test that `.github/ARTIFACT_RETENTION.md` exists and documents all artifact types
    - Test that `[tool.mypy] python_version` matches the `requires-python` minimum version
    - **Validates: Requirements 11.1–11.5, 12.1, 14.1, 14.3**

- [x] 13. Validate Dependabot configuration (no changes needed)
  - [x] 13.1 Write property test for Dependabot config (Property 8)
    - **Property 8: Dependabot entries target develop with grouped updates**
    - Create `backend/tests/supply_chain/test_dependabot_config.py`
    - Parse `.github/dependabot.yml`, verify every ecosystem entry has `target-branch: "develop"` and a `groups` section with `update-types` covering both `"minor"` and `"patch"`
    - **Validates: Requirements 9.2, 9.3**

- [x] 14. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Component 6 (Dependabot) requires NO file changes — existing config already meets requirements; only a validation test is added
- Component 11 (cancel-in-progress) requires NO changes — all workflows correctly use `false` since they all have CDK deploys
- Component 12 (smoke tests) and Property 13 are DEFERRED — no tasks created
- For npm version pinning (tasks 5.1, 5.2), resolved versions must be read from the lockfile, not just stripped of `^` or `~`
- CDK target versions: `aws-cdk` CLI = `2.1113.0`, `aws-cdk-lib` = `2.244.0`
- Property tests use Python `hypothesis` + `pytest` (already in dev dependencies)
- All property tests go under `backend/tests/supply_chain/`
