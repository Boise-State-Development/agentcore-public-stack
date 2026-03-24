# Requirements Document

## Introduction

This specification addresses supply chain hardening, build reproducibility, and CI/CD security across the AgentCore Public Stack. The audit identified 17 issues spanning GitHub Actions workflows, Python/npm dependency management, Docker builds, and pipeline configuration. This document formalizes each finding into testable, EARS-compliant requirements organized by priority.

**Scope**: GitHub Actions workflows, dependency manifests (`pyproject.toml`, `package.json`), Dockerfiles, shell scripts, and CI/CD pipeline configuration.

**Out of Scope**: Application-level security (XSS, injection), RBAC logic, runtime secrets rotation.

## Glossary

- **CI_Pipeline**: The set of GitHub Actions workflows that build, test, and deploy the AgentCore Public Stack
- **Dependency_Manager**: The tools responsible for resolving and installing packages (npm, uv/pip)
- **Workflow_Runner**: The GitHub Actions runner environment executing CI/CD jobs
- **Docker_Builder**: The multi-stage Docker build process producing container images for deployment
- **Dependabot**: GitHub's automated dependency update service configured via `.github/dependabot.yml`
- **Image_Scanner**: A container vulnerability scanning tool (e.g., Trivy, Grype, or ECR native scanning)
- **Install_Script**: Shell scripts in `scripts/` that install dependencies for CI/CD and local development
- **CDK_Deployer**: The AWS CDK synthesis and deployment pipeline for infrastructure stacks
- **SHA_Digest**: An immutable, content-addressable hash identifying a specific version of a GitHub Action or container image
- **Lockfile**: A file (`uv.lock`, `package-lock.json`) that records exact resolved dependency versions

## Requirements

### Requirement 1: Pin GitHub Actions to SHA Digests

**User Story:** As a DevOps engineer, I want all GitHub Actions references pinned to immutable SHA digests, so that a compromised or force-pushed tag cannot inject malicious code into the CI pipeline.

**Priority:** HIGH

#### Acceptance Criteria

1. THE CI_Pipeline SHALL reference every third-party GitHub Action using a full SHA-256 digest followed by a comment indicating the human-readable version tag
2. WHEN a new GitHub Action version is adopted, THE CI_Pipeline SHALL update both the SHA digest and the version comment in the same commit
3. WHEN Dependabot proposes a GitHub Actions digest bump, THE CI_Pipeline SHALL receive a pull request targeting the `develop` branch with the updated SHA digest
4. THE CI_Pipeline SHALL reference the local composite action (`.github/actions/configure-aws-credentials`) using a relative path (exempt from SHA pinning)

### Requirement 2: Pin Python Dependencies to Exact Versions

**User Story:** As a backend developer, I want Python dependencies pinned to exact versions in `pyproject.toml`, so that local development and CI resolve identical packages.

**Priority:** HIGH

#### Acceptance Criteria

1. THE Dependency_Manager SHALL resolve all direct Python dependencies in `pyproject.toml` to exact versions using the `==` operator
2. THE Dependency_Manager SHALL resolve all optional dependency groups (`agentcore`, `dev`) to exact versions using the `==` operator
3. WHEN a dependency version is updated, THE Dependency_Manager SHALL update both `pyproject.toml` and `uv.lock` in the same commit
4. IF a dependency in `pyproject.toml` uses a floor pin (`>=`), a compatible release pin (`~=`), or has no version constraint, THEN THE CI_Pipeline SHALL fail a lint check identifying the non-exact pin

### Requirement 3: Pin Frontend Dependencies to Exact Versions

**User Story:** As a frontend developer, I want npm dependencies pinned to exact versions in `package.json`, so that `npm ci` produces identical `node_modules` across all environments.

**Priority:** HIGH

#### Acceptance Criteria

1. THE Dependency_Manager SHALL specify all direct frontend dependencies in `package.json` using exact version strings (no `^` or `~` prefix)
2. THE Dependency_Manager SHALL specify all devDependencies in `package.json` using exact version strings (no `^` or `~` prefix)
3. WHEN a dependency version is updated, THE Dependency_Manager SHALL update both `package.json` and `package-lock.json` in the same commit

### Requirement 4: Pin Global Tool Installations in Install Scripts

**User Story:** As a DevOps engineer, I want global tool installations in CI scripts pinned to specific versions, so that builds are reproducible regardless of when they run.

**Priority:** HIGH

#### Acceptance Criteria

1. THE Install_Script SHALL install the AWS CDK CLI at a specific pinned version (e.g., `npm install -g aws-cdk@2.1033.0`) instead of resolving to the latest release
2. THE Install_Script SHALL install Node.js from a versioned distribution URL specifying a major version (e.g., `setup_20.x`)
3. WHEN the Install_Script installs any global npm package, THE Install_Script SHALL specify an exact version using the `@version` suffix

### Requirement 5: Tighten aws-cdk-lib Version Range

**User Story:** As an infrastructure developer, I want the `aws-cdk-lib` dependency range tightened, so that weekly CDK releases with breaking construct changes do not silently enter the build.

**Priority:** HIGH

#### Acceptance Criteria

1. THE Dependency_Manager SHALL specify `aws-cdk-lib` in `infrastructure/package.json` using an exact version pin (e.g., `"aws-cdk-lib": "2.235.1"`)
2. THE Dependency_Manager SHALL specify the `aws-cdk` CLI devDependency in `infrastructure/package.json` using an exact version pin
3. WHEN the CDK version is updated, THE Dependency_Manager SHALL update both `package.json` and `package-lock.json` in the same commit

### Requirement 6: Enforce npm ci in All CI Install Paths

**User Story:** As a DevOps engineer, I want all CI dependency installations to use `npm ci`, so that the lockfile is the single source of truth for resolved versions.

**Priority:** HIGH

#### Acceptance Criteria

1. THE CI_Pipeline SHALL use `npm ci` (not `npm install`) for all npm dependency installations during CI/CD jobs
2. WHEN a `package-lock.json` file is present, THE Install_Script SHALL use `npm ci` for dependency installation
3. IF a `package-lock.json` file is missing, THEN THE Install_Script SHALL exit with a non-zero status code and a descriptive error message

### Requirement 7: Add Container Image Scanning

**User Story:** As a security engineer, I want automated vulnerability scanning on all Docker images before deployment, so that known CVEs are detected before reaching production.

**Priority:** HIGH

#### Acceptance Criteria

1. WHEN a Docker image is built in the CI_Pipeline, THE Image_Scanner SHALL scan the image for known vulnerabilities before the image is pushed to ECR
2. IF the Image_Scanner detects a vulnerability with severity CRITICAL or HIGH, THEN THE CI_Pipeline SHALL fail the build and report the findings in the job summary
3. THE Image_Scanner SHALL produce a scan report artifact with retention matching the existing artifact retention policy
4. THE CI_Pipeline SHALL scan all Dockerfiles in the repository (`Dockerfile.app-api`, `Dockerfile.inference-api`, `Dockerfile.rag-ingestion`)

### Requirement 8: Pin GitHub Actions Runner Versions

**User Story:** As a DevOps engineer, I want CI runners pinned to specific OS versions, so that runner image updates do not introduce unexpected environment changes.

**Priority:** HIGH

#### Acceptance Criteria

1. THE CI_Pipeline SHALL specify explicit runner OS versions (e.g., `ubuntu-24.04`) instead of floating aliases (e.g., `ubuntu-latest`) for all workflow jobs
2. WHEN a runner OS version is updated, THE CI_Pipeline SHALL update all workflow files referencing that runner in the same pull request

### Requirement 9: Enhance Dependabot Configuration for SHA Pinning

**User Story:** As a DevOps engineer, I want Dependabot configured to propose SHA digest bumps for GitHub Actions, so that pinned actions stay current with security patches.

**Priority:** HIGH

**Note:** A `dependabot.yml` already exists with coverage for pip, npm (frontend and infrastructure), and github-actions ecosystems. This requirement focuses on ensuring the configuration supports the SHA-pinned workflow.

#### Acceptance Criteria

1. THE Dependabot configuration SHALL include a `github-actions` ecosystem entry that proposes SHA digest updates for all pinned actions
2. THE Dependabot configuration SHALL target the `develop` branch for all update pull requests
3. THE Dependabot configuration SHALL group minor and patch updates to reduce pull request volume

### Requirement 10: Pin Docker apt-get Package Versions

**User Story:** As a DevOps engineer, I want apt-get packages in Dockerfiles pinned to specific versions, so that the apt layer does not undermine the base image digest pin.

**Priority:** HIGH

#### Acceptance Criteria

1. THE Docker_Builder SHALL install all apt-get packages with explicit version pins (e.g., `gcc=12.2.0-14`)
2. WHEN a Dockerfile installs system packages via apt-get, THE Docker_Builder SHALL specify the package version for each package
3. IF an apt-get package version is unavailable in the base image's package repository, THEN THE Docker_Builder SHALL document the version constraint as a comment in the Dockerfile

### Requirement 11: Create Fork Setup Documentation

**User Story:** As an external contributor, I want a step-by-step fork setup guide, so that I can reproduce the development environment without internal knowledge.

**Priority:** HIGH

#### Acceptance Criteria

1. THE CI_Pipeline repository SHALL include a `CONTRIBUTING.md` file at the repository root
2. THE `CONTRIBUTING.md` SHALL document prerequisites (Node.js version, Python version, AWS CLI, Docker)
3. THE `CONTRIBUTING.md` SHALL document step-by-step instructions for cloning, installing dependencies, and running the application locally
4. THE `CONTRIBUTING.md` SHALL document how to configure required environment variables and AWS credentials for local development
5. THE `CONTRIBUTING.md` SHALL document how to run the test suites for backend, frontend, and infrastructure

### Requirement 12: Fix mypy Target Version Mismatch

**User Story:** As a backend developer, I want the mypy target version aligned with the project's minimum Python version, so that type checking reflects the actual runtime environment.

**Priority:** MEDIUM

#### Acceptance Criteria

1. THE Dependency_Manager configuration SHALL set `[tool.mypy] python_version` to match the `requires-python` minimum version in `pyproject.toml`
2. WHEN the `requires-python` minimum version is changed, THE Dependency_Manager configuration SHALL update the mypy `python_version` in the same commit

### Requirement 13: Standardize GitHub Actions Checkout Versions

**User Story:** As a DevOps engineer, I want all workflows using the same version of `actions/checkout`, so that inconsistent behavior across workflows is eliminated.

**Priority:** MEDIUM

#### Acceptance Criteria

1. THE CI_Pipeline SHALL use the same SHA-pinned version of `actions/checkout` across all workflow files
2. WHEN the `actions/checkout` version is updated, THE CI_Pipeline SHALL update all workflow files in the same pull request

### Requirement 14: Document Artifact Retention Policy

**User Story:** As a DevOps engineer, I want a documented artifact retention policy, so that storage costs are predictable and retention periods are intentional.

**Priority:** MEDIUM

#### Acceptance Criteria

1. THE CI_Pipeline repository SHALL include a documented artifact retention policy specifying retention periods by artifact type
2. THE CI_Pipeline SHALL apply consistent retention periods across all workflows for the same artifact type (e.g., all Docker image artifacts use the same retention, all CDK synth artifacts use the same retention)
3. THE artifact retention policy SHALL define retention periods for: Docker image artifacts, CDK synthesized templates, test results, and deployment outputs

### Requirement 15: Enable cancel-in-progress for Frontend Workflows

**User Story:** As a DevOps engineer, I want `cancel-in-progress: true` on frontend asset workflows, so that superseded builds are cancelled to save runner minutes.

**Priority:** MEDIUM

#### Acceptance Criteria

1. THE CI_Pipeline SHALL set `cancel-in-progress: true` on the concurrency group for the frontend workflow
2. THE CI_Pipeline SHALL retain `cancel-in-progress: false` for workflows that deploy infrastructure or push Docker images to ECR

### Requirement 16: Add Post-Deployment Smoke Tests

**User Story:** As a DevOps engineer, I want smoke tests after every deployment, so that broken deployments are detected before users are affected.

**Priority:** MEDIUM

#### Acceptance Criteria

1. WHEN a deployment job completes successfully, THE CI_Pipeline SHALL execute a health check against the deployed service endpoint
2. IF the post-deployment health check fails, THEN THE CI_Pipeline SHALL report the failure in the job summary and exit with a non-zero status
3. THE CI_Pipeline SHALL include post-deployment smoke tests for the App API, Inference API, and Frontend workflows

### Requirement 17: Scope Secrets to Jobs That Require Them

**User Story:** As a security engineer, I want AWS credentials scoped to only the jobs that need AWS access, so that the blast radius of a credential leak is minimized.

**Priority:** MEDIUM

#### Acceptance Criteria

1. THE CI_Pipeline SHALL define AWS credential environment variables at the job level (not the workflow level) only for jobs that perform AWS operations
2. THE CI_Pipeline SHALL omit AWS credential environment variables from jobs that do not interact with AWS services (e.g., unit test jobs, lint jobs, build jobs without ECR push)
3. WHEN a new job is added to a workflow, THE CI_Pipeline SHALL include AWS credentials only if the job requires AWS API calls
