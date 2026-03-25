# Release Notes — v1.0.0-beta.18

**Release Date:** March 24, 2026
**Previous Release:** v1.0.0-beta.17 (March 23, 2026)

---

## Highlights

This release is a **supply chain security hardening** release. Every dependency across all three ecosystems (Python, npm, GitHub Actions) has been pinned to exact versions, all GitHub Actions are SHA-pinned, CI runners are locked to `ubuntu-24.04`, Dockerfile `apt`/`dnf` packages are version-pinned, and a new 11-file property-based test suite enforces these invariants going forward. Alongside the hardening, the release adds **CodeQL Advanced security scanning**, a **flexible nightly track system** that replaces the monolithic nightly pipeline, and migrates **RAG resources out of the App API stack** into the dedicated RAG Ingestion stack.

---

## ⚠️ Deployment Note — RAG Data Loss on Existing Deployments

This release removes the assistants documents S3 bucket (`assistants-documents`), S3 Vector Bucket (`assistants-vector-store-v1`), and Vector Index (`assistants-vector-index-v1`) from `AppApiStack`. These resources are now created in `RagIngestionStack` under new names (`rag-vector-store-v1`, etc.). Because CloudFormation tracks resources by logical ID within a stack, deploying this release will cause CDK to delete the old resources from the App API stack. Any existing assistant documents and vector embeddings stored in those buckets will be lost.

If your deployment has data in these resources, you should manually back up or migrate the contents before deploying. If `CDK_RETAIN_DATA_ON_DELETE` is `true` in your environment, the removal policy may be set to `RETAIN`, which would orphan the resources instead of deleting them — but you should verify this against your configuration before relying on it.

---

## Supply Chain Security Hardening

A comprehensive security audit identified 17 findings across GitHub Actions, dependency manifests, Dockerfiles, and install scripts. This release addresses all of them.

### GitHub Actions SHA Pinning

All third-party GitHub Actions are now pinned to specific commit SHAs with version comments (e.g., `actions/checkout@de0fac2e...  # v6.0.2`). This prevents tag-rewriting supply chain attacks where a compromised action could inject malicious code into CI runs.

### Runner Pinning

All workflow jobs now use `ubuntu-24.04` instead of `ubuntu-latest`, ensuring consistent and reproducible build environments that won't silently change behavior when GitHub rolls forward the `latest` tag.

### Exact Dependency Pinning

All three ecosystems have been migrated from range specifiers (`>=`, `^`, `~`) to exact version pins:

- **Python** (`pyproject.toml`): Every dependency uses `==` pins (e.g., `fastapi==0.135.2`, `boto3==1.42.73`, `strands-agents==1.32.0`)
- **npm frontend** (`package.json`): All `^` prefixes removed, exact versions throughout (e.g., `@angular/core` `21.2.5`, `tailwindcss` `4.2.1`)
- **npm infrastructure** (`package.json`): Same treatment (e.g., `aws-cdk-lib` `2.244.0`, `aws-cdk` `2.1113.0`)

### Dockerfile Package Pinning

All `apt-get install` and `dnf install` commands now specify exact package versions:

- App API and Inference API Dockerfiles: `gcc=4:14.2.0-1`, `g++=4:14.2.0-1`, `curl=8.14.1-2+deb13u2`
- RAG Ingestion Dockerfile: All 9 `dnf` packages pinned (gcc, make, mesa-libGL, glib2, tar, gzip, ca-certificates, unzip)

### Script Hardening

All deployment and install scripts now use `npm ci` exclusively (no `npm install` fallback), ensuring lockfile-driven deterministic installs across all environments.

### Artifact Retention Policy

A new `.github/ARTIFACT_RETENTION.md` defines tiered retention periods: Docker tarballs and CDK build artifacts at 1 day, synthesized templates and test results at 7 days, deployment outputs and Trivy scan reports at 30 days. All workflow `retention-days` values have been aligned to this policy.

### Supply Chain Test Suite

A new `backend/tests/supply_chain/` directory contains 11 property-based test files that validate security invariants:

- Action SHA pinning, runner version pinning, dependency exact pinning
- Dockerfile package pinning, artifact retention consistency
- Concurrency configuration, secret scoping, script hardening
- Dependabot configuration, documentation presence

These tests run as part of the standard `pytest` suite and will catch regressions if anyone reintroduces range specifiers or unpinned actions.

---

## CodeQL Advanced Security Scanning

A new `codeql.yml` workflow provides static analysis across three languages: Python, TypeScript, and GitHub Actions. It uses the `security-and-quality` query suite for broad vulnerability and code quality coverage, plus the `github-actions` threat model for full Actions taint tracking (18 queries covering code injection, artifact poisoning, cache poisoning, and secret exposure).

The workflow runs on push and PR to `develop`, plus a weekly scheduled scan to catch new CVEs even when code hasn't changed. A custom `codeql-config.yml` excludes vendored, generated, test, and build artifact paths to keep scan times reasonable. The first scan already surfaced unused imports and variables in the supply chain test suite, which have been cleaned up in this release.

---

## Flexible Nightly Track Selection

The monolithic nightly pipeline has been replaced with a composable track-based system. Instead of a single `NIGHTLY_ENABLED` boolean, the workflow now reads a `NIGHTLY_TRACKS` variable (or `workflow_dispatch` input) containing comma-separated track tokens:

- `test-backend-<branch>` / `test-frontend-<branch>` — Run tests against any branch
- `deploy-<branch>` — Deploy full stack from any branch
- `merge-validation:<base>:<overlay>` — Deploy base, then overlay (simulates merge)
- `scan-images-<branch>` — Scan Docker images for vulnerabilities
- `all` — Run everything with default branches

A new `resolve-tracks` job parses the tokens into boolean flags and branch refs consumed by downstream jobs. The deploy pipeline is extracted into a reusable `nightly-deploy-pipeline.yml` called up to 3 times (deploy track, MV base, MV overlay), eliminating all duplication. Fork safety is preserved — if `NIGHTLY_TRACKS` is empty, nothing runs.

---

## RAG Resources Migration

RAG resources (assistants documents bucket, S3 Vector Bucket, Vector Index) have been removed from `AppApiStack` and are now exclusively managed by `RagIngestionStack`. The App API stack imports these resources via SSM parameters, improving separation of concerns and eliminating cross-stack resource ownership issues.

The vector store IAM permissions in the App API task role now reference the RAG vector bucket imported from SSM (`/${projectPrefix}/rag/vector-bucket-name`) instead of a locally-created bucket, with a named SID (`RagVectorStoreAccess`) for better auditability.

---

## Embeddings Refactor

Core embedding and vector store operations have been extracted from the ingestion pipeline into a new shared module at `apis.shared.embeddings`. The functions `generate_embeddings`, `store_embeddings_in_s3`, `search_assistant_knowledgebase`, and `delete_vectors_for_document` now live in `apis.shared.embeddings.bedrock_embeddings`, with the ingestion-specific module re-exporting them for backward compatibility.

A new `skip_token_validation` parameter on `generate_embeddings` allows callers to bypass tiktoken-based token validation for short inputs in environments where tiktoken is unavailable (e.g., search Lambda functions). The ingestion pipeline retains its own token validation and chunk-splitting logic.

---

## Dependabot Configuration

A new `.github/dependabot.yml` monitors all four ecosystems (pip, frontend npm, infrastructure npm, GitHub Actions) on a weekly Monday 9 AM Mountain Time schedule. Minor and patch updates are grouped to reduce PR noise (Angular updates grouped separately from other frontend deps, AWS CDK grouped separately from other infrastructure deps). All PRs target the `develop` branch with ecosystem-specific labels.

---

## CI/CD Improvements

- **AWS credentials action upgraded** to `v6.0.0` with SHA pinning, plus a new sanitization step that replaces illegal characters in OIDC role session names and truncates to the 64-character AWS limit
- **Explicit OIDC permissions** added to nightly deploy, MV base, and MV overlay jobs (`id-token: write`, `contents: read`)
- **SageMaker conditional gating** — synth job now outputs an `enabled` flag based on `CDK_FINE_TUNING_ENABLED`; test and deploy jobs skip when fine-tuning is disabled
- **Node.js 24 action warnings** fixed after SHA-pinning reintroduced older action references

---

## Dependency Upgrades

| Component | From | To |
|---|---|---|
| FastAPI | 0.116.1 | 0.135.2 |
| Starlette | 0.47.3 | 1.0.0 |
| strands-agents | 1.27.0+ | 1.32.0 |
| strands-agents-tools | 0.2.20 | 0.2.23 |
| boto3 | 1.40.1+ | 1.42.73 |
| bedrock-agentcore | latest | 1.4.7 |
| Angular packages | 21.0.x | 21.2.5 |
| @angular/cdk | 21.0.3 | 21.2.3 |
| Tailwind CSS | 4.1.12+ | 4.2.1 |
| aws-cdk-lib | 2.235.1 | 2.244.0 |
| aws-cdk (CLI) | 2.1033.0 | 2.1113.0 |
| DOMPurify | 3.3.1 | 3.3.3 |
| undici | 7.22.0 | 7.24.5 |
| hono | 4.12.2 | 4.12.9 |
| katex | 0.16.25 | 0.16.33 |
| mermaid | 11.12.1 | 11.12.3 |
| Vitest | 4.0.8 | 4.0.18 |
| mypy target | py3.9 | py3.10 |

---

## Bug Fixes

- **Fine-tuning dashboard** — Removed an incorrect "retention" label from the inference job display on the SageMaker fine-tuning dashboard.

---

## Documentation & Developer Experience

- Added `CONTRIBUTING.md` with prerequisites, clone/install instructions, environment configuration, testing commands, and contribution workflow
- Supply chain hardening spec (requirements, design, tasks) added under `.kiro/specs/supply-chain-hardening/`

---


---

# Release Notes — v1.0.0-beta.17

**Release Date:** March 23, 2026
**Previous Release:** v1.0.0-beta.16 (March 20, 2026)

---

## Highlights

This release delivers three major improvements: a **centralized Settings experience** that consolidates scattered user preferences into dedicated pages backed by a new DynamoDB table, a **pip-to-uv migration** that modernizes the entire Python build pipeline with hardened Docker images, and **runtime environment refresh** so AgentCore containers always pick up the latest SSM parameter values on every deploy instead of carrying forward stale configuration.

---

## Centralized User Settings

The user dropdown menu has been slimmed down to just email, admin link, settings, and logout. All user-facing features that were previously scattered across the dropdown and standalone pages have been consolidated into a `/settings/*` route hierarchy with dedicated pages:

- **Profile** — Read-only user info display with a link to My Files
- **Appearance** — Theme chooser (persisted to localStorage) with placeholders for density and font size
- **Chat Preferences** — Default model selector backed by a new User Settings API (`GET/PUT /users/me/settings`), show-token-count toggle, and links to Manage Conversations and Memories
- **Connections** — Full OAuth connect/disconnect flow via a new `ConnectionsService`
- **API Keys** — Migrated from the standalone `/api-keys` page with loading states
- **Usage** — Migrated from the standalone `/costs` dashboard with a month picker for historical data

### Backend

A new `user-settings` DynamoDB table and repository store per-user preferences (starting with `defaultModelId`). The table is provisioned in the Infrastructure stack with IAM permissions granted to both the App API Fargate tasks and Inference API runtime roles. Graceful degradation is built in — if the table doesn't exist yet, the API returns defaults without errors.

### Removed

The standalone Notifications and Privacy settings pages were removed as unnecessary.

---

## pip → uv Migration

The entire Python toolchain has been migrated from pip to [uv](https://docs.astral.sh/uv/), affecting Docker builds, CI pipelines, and local development workflows.

### Docker Security Hardening

- All base images pinned to `@sha256` digests (Python 3.13-slim, Lambda Python 3.12)
- Non-root `USER` directive added to the App API Dockerfile
- Rust toolchain installed via `COPY --from=rust:1.87-slim` (pinned digest) instead of `curl | sh`
- Torch pinned to exact version (`2.10.0`) in RAG ingestion with `--require-hashes` install from a generated `requirements.lock`
- `curl` removed from builder stages

### CI/CD

- All three Dockerfiles (app-api, inference-api, rag-ingestion) rewritten for uv
- CI install and test scripts updated for both app-api and inference-api
- Workflow caching switched to uv cache paths
- `backend/uv.lock` added to workflow path triggers
- `sync-version.sh` now handles `uv.lock` regeneration with PEP 440 version conversion

### New Release Workflow

A standalone `release.yml` workflow triggers on push to main, creating annotated git tags and GitHub Releases from `RELEASE_NOTES.md`. Pre-release versions (alpha/beta/rc/dev) are automatically detected and flagged.

### Dependabot

A new `.github/dependabot.yml` monitors pip, npm, and GitHub Actions dependencies.

---

## Runtime Provisioner: SSM Environment Refresh

Previously, when an AgentCore runtime was updated (e.g., on redeploy), the provisioner Lambda preserved the existing environment variables from the original runtime creation. This meant renamed tables, new SSM parameters, or changed values were never picked up.

Now, `update_runtime()` re-fetches all environment variables from SSM on every update. A fallback to existing values is included if the SSM refresh fails, maintaining stability. The runtime-updater Lambda also gained a `get_fresh_environment_variables()` function for consistent handling.

---

## Configurable Memory Retrieval Thresholds

AgentCore Memory retrieval is now tunable via two new environment variables:

- `AGENTCORE_MEMORY_RELEVANCE_SCORE` — Minimum relevance score for retrieved memories (default raised from 0.3–0.5 to 0.7)
- `AGENTCORE_MEMORY_TOP_K` — Maximum number of memories to retrieve

All memory-related environment variables have been renamed from `COMPACTION_*` to `AGENTCORE_MEMORY_COMPACTION_*` for consistent naming.

---

## Assistant UX Improvements

The assistant experience in the chat interface received several polish updates:

- **Action dropdown** on the assistant indicator with options to start a new session, edit the assistant, or share it
- **Share dialog** on the assistant form page for sharing assistants with other users
- **Skeleton loading indicators** replace blank states while the assistant and chat input are loading
- **Improved greeting visibility** — the assistant greeting now shows/hides properly based on loading state
- **Sidenav updates** — the new session button and assistant navigation link are now accessible from the sidebar
- **Responsive card layout** fix for the assistant list page

---

## SageMaker Fine-Tuning Fixes

- **Job name scoping** — Training and transform job names are now prefixed with `PROJECT_PREFIX` to match the IAM policy's `${projectPrefix}-*` resource constraint. Previously, jobs used `ft-` and `inf-` prefixes which caused `AccessDeniedException` on `CreateTrainingJob`.
- **Missing IAM actions** — Added `sagemaker:CreateModel` and `sagemaker:DeleteModel` actions plus the model resource ARN to the IAM policy for transform job support.
- **Log access** — Added `logs:DescribeLogStreams` to the IAM policy so the fine-tuning dashboard can display SageMaker training logs.
- **CDK toggle** — Added `CDK_FINE_TUNING_ENABLED` environment variable to the app-api CI workflow for conditional stack deployment.

---

## Bug Fixes

- **User settings API trailing slashes** — Removed trailing slashes from the `/users/me/settings` routes that caused 307 redirects on some HTTP clients.
- **Assistant list card layout** — Fixed responsive grid breakpoints on the assistant list page so cards don't overflow on narrow viewports.

---

## Documentation & Developer Experience

- Updated `CLAUDE.md` with revised coding standards, testing guidelines, and file creation rules
- README logo and header formatting refreshed for better visibility and alignment

---


---

# Release Notes — v1.0.0-beta.16

**Release Date:** March 20, 2026
**Previous Release:** v1.0.0-beta.15 (March 20, 2026)

---

## Hotfix: Runtime Provisioner SSM Path

The runtime provisioner Lambda was still referencing the old `/file-upload/table-name` SSM parameter path for the user files DynamoDB table. This caused `AccessDeniedException` on `dynamodb:GetItem` because the AgentCore runtime container received the old table name (`user-files`) while the IAM policy was scoped to the new table (`user-file-uploads`). Updated to `/user-file-uploads/table-name` to match the Infrastructure stack's SSM exports.

---

---

# Release Notes — v1.0.0-beta.15

**Release Date:** March 20, 2026
**Previous Release:** v1.0.0-beta.8 (March 16, 2026)

---

## Highlights

This release introduces the **SageMaker Fine-Tuning** stack — a complete model training and inference platform built on Amazon SageMaker, deployable as an optional CDK stack. Beyond that, the release delivers **security hardening**, **deployment reliability**, and **platform modernization**: RBAC model access enforcement is now applied at the inference layer, the nightly CI/CD pipeline gains a full merge-validation track to catch integration issues before release, and the entire stack has been upgraded to current runtime versions (Python 3.13, Angular 21.2, Node.js 24 Actions, CDK 2.1112).

---

## ⚠️ Deployment Note

Merging this release will trigger all stack workflows simultaneously. File upload resources (S3 bucket, DynamoDB table, SSM parameters) were moved into the Infrastructure stack, so the App API and Inference API deployments may fail if Infrastructure hasn't finished yet. This is expected — just rerun the failed workflows after the Infrastructure deployment completes.

---

## New Feature: SageMaker Fine-Tuning

A complete model fine-tuning platform has been added, allowing users with admin-granted access to train and run inference on open-source models directly from the UI.

- New `SageMakerFineTuningStack` CDK stack with DynamoDB tables, S3 storage, and IAM roles for SageMaker training/inference
- Backend API with full CRUD for training jobs, inference jobs, and admin access management (`/fine-tuning/` routes)
- SageMaker integration for launching training jobs on models like BERT, RoBERTa, and GPT-2 with configurable hyperparameters (epochs, batch size, learning rate, train/test split)
- Batch inference support on trained models with real-time progress tracking
- Frontend dashboard with job creation wizards, detail pages, status badges, quota cards, and dataset upload via presigned S3 URLs
- Admin access control page for granting/revoking fine-tuning permissions per user
- Automatic 30-day artifact retention with lifecycle policies
- Dedicated CI/CD workflow (`sagemaker-fine-tuning.yml`) with build, synth, test, and deploy scripts
- EC2 networking permissions for VPC-based training jobs
- Elapsed time display and polling for active jobs
- Comprehensive test suite (admin routes, user routes, repositories, SageMaker service, training/inference scripts)

---

## Community Contribution 🎉

This release includes our first outside contribution! Thanks to [@magicfoodhand](https://github.com/magicfoodhand) for **Session List Grouping Enhancements** (#43) — the session sidebar now groups conversations by date range (Today, Yesterday, Previous 7 Days, etc.) and supports inline session renaming. A great UX improvement.

---

## Bug Fixes

- **RBAC model access not enforced on Inference API** (#31, #47) — Role-based model access was only checked on the App API side, allowing the Inference API's Converse and Invocations endpoints to bypass model-level RBAC. Both endpoints now call `can_access_model()` and reject unauthorized requests with HTTP 403 before any Bedrock invocation occurs. Includes 1,500+ lines of new test coverage.
- **Deprecated `datetime.utcnow()` replaced** — All backend modules (quota recorder, admin models, user service, file service, tools, document ingestion) now use timezone-aware `datetime.now(timezone.utc)`, resolving Python 3.12+ deprecation warnings.
- **Cross-stack SSM deployment failure properly fixed** — File upload resources (S3 bucket, DynamoDB table, SSM parameters) have been relocated from `AppApiStack` to `InfrastructureStack`, eliminating the cross-stack dependency that caused first-time deployment failures. The beta.8 hotfix (hardcoded ARN construction) was a temporary workaround; this is the permanent solution.
- **Dependency conflict resolved** — Pillow was temporarily removed then restored alongside numpy to resolve a packaging conflict with `strands-agents-tools`.

---

## Infrastructure & Configuration

### File Upload Resources Relocated to Infrastructure Stack
File upload S3 bucket and DynamoDB table have been moved from `AppApiStack` to `InfrastructureStack` to eliminate the cross-stack dependency between Inference API (tier 2) and App API (tier 3). Unfortunately, the path of least resistance was to recreate these resources with new names, so be aware that some data loss may occur when updating an existing deployment. SSM parameter paths have been renamed from `/file-upload/` to `/user-file-uploads/` for consistency. 

### Auto-Derived CORS Origins
Deployments no longer require explicit `CDK_CORS_ORIGINS`. If only `CDK_DOMAIN_NAME` is set, CORS origins are automatically derived as `https://<domain>`. This simplifies initial setup and reduces configuration errors.

### Unified Removal Policies
S3 buckets and Secrets Manager secrets across all stacks (`AppApiStack`, `InfrastructureStack`, `RagIngestionStack`) now use config-driven removal policies via `getRemovalPolicy(config)` and `getAutoDeleteObjects(config)` instead of hardcoded `RETAIN`. This enables clean teardown in non-production environments.

### AWS Account in Resource Naming
`getResourceName()` calls for S3 buckets now include `config.awsAccount`, ensuring unique and consistent resource names across multi-account deployments. Be aware of potential data loss when updating existing deployments as the default bucket naming scheme has changed. Each stack will now suffix the account number to prevent s3 name collisions.

---

## Platform Upgrades

| Component | From | To |
|---|---|---|
| Python runtime | 3.11 | 3.13 |
| FastAPI | 0.116.1 | 0.135.1 |
| Uvicorn | 0.35.0 | 0.42.0 |
| strands-agents-tools | 0.2.20 | 0.2.22 |
| Angular packages | 21.0.x | 21.2.x |
| Algolia client packages | 5.46.2 | 5.48.1 |
| AWS CDK | 2.1033.0 | 2.1112.0 |
| @types/jest | — | ^30.0.0 |
| jest | — | ^30.3.0 |
| Starlette | — | >=0.49.1 (new explicit dep) |
| cryptography | — | >=46.0.5 (new explicit dep) |

---

## CI/CD & DevOps

### Nightly Pipeline Improvements
A new merge-validation track deploys `main` branch infrastructure first, then deploys `develop` branch on top — simulating the real merge scenario. This catches integration issues between branches before they reach production. The track includes full stack deployment (infrastructure → RAG ingestion → inference API → app API → frontend) with automatic teardown. Nightlies also no longer rebuild Docker images; a new `promote-ecr-image.sh` script copies pre-built images from the develop ECR repository to the target environment, cutting pipeline time and ensuring image parity with what was tested on develop.

### Stack Dependency Validation
All GitHub workflows now include a `check-stack-dependencies` gate job that validates CDK stack dependencies before any build or deploy step runs. A new `test-stack-dependencies.sh` script powers this check.

### GitHub Actions Node.js 24 Migration
All GitHub Actions have been upgraded to Node.js 24-compatible versions:
- `actions/checkout` v4 → v5
- `actions/cache` v4 → v5
- `actions/upload-artifact` / `download-artifact` v4 → v5 (then v7)
- `aws-actions/configure-aws-credentials` v4 → v6
- `docker/setup-buildx-action` v3 → v4
- `docker/build-push-action` v6 → v7

### Additional CI Improvements
- Fork guard prevents accidental nightly runs on forked repositories
- Package-lock.json sync validation added to version-check workflow
- Frontend build caching with split build/deploy steps (nightly)
- Centralized pipeline summary table
- Artifact handling switched from cache to upload/download actions
- Retry logic added to smoke test health checks
- S3 Vector Bucket cleanup added to teardown scripts (nightly)
- CloudWatch log group cleanup added to teardown scripts (nightly)
- Reduced CI log verbosity across all workflows

---
