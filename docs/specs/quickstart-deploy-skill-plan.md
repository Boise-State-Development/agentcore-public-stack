# Plan: "Quick Deploy" Agent Skill for an Eval-Grade AgentCore Public Stack

**Status:** Proposal / for review
**Audience:** Colin (+ team)
**Author:** Phil (with Claude)
**Date:** 2026-06-05

---

## TL;DR

Build an **agent skill** that stands up a working, **eval-grade** AgentCore Public Stack in a **single AWS account** with the **fewest possible dependencies** — no custom domain, no ACM certificates, no Route 53, no GitHub fork. The skill drives the deploy locally against the user's AWS credentials and hands back a working `https://<id>.cloudfront.net` URL.

We keep two clearly separated deploy paths:

| Path | Engine | For |
|------|--------|-----|
| **Production / multi-environment** | GitHub Actions (existing, documented in `.github/docs/deploy/`) | Real environments — auditable, OIDC, gated, reproducible |
| **Quick Deploy (this proposal)** | Local scripts driven by an agent skill | Fast, throwaway, single-account evaluations |

The one genuinely hard part is **Docker image architecture** (the stack ships amd64 *and* arm64 images). We recommend shipping v1 with local `buildx` + QEMU emulation (exactly what CI already does), and commit to **publishing prebuilt multi-arch images** as the strategic end-state — that's the only option that removes the Mac-silicon pain *and* unlocks a true "AWS CLI only" experience.

---

## 1. Goals & Non-Goals

### Goals
- An agent skill that deploys a **functional** AgentCore Public Stack (chat works end-to-end) for evaluation.
- **Minimize dependencies:** domainless, no certs, no Route 53, no GitHub.
- **Stretch goal:** require only the **AWS CLI** configured against the target account.
- Be honest about what "eval-grade" gives up vs. the production path.
- Reuse the existing, maintained `scripts/*.sh` that CI already calls — not a parallel deploy implementation.

### Non-Goals
- Not a production deployment mechanism. Production stays on the GitHub Actions pipeline.
- Not multi-environment. One account, one stack, one region.
- Not a replacement for `.github/docs/deploy/` — the skill explicitly points multi-env users there.
- No federated identity setup, custom domains, or TLS-on-a-real-name (all addable post-deploy).

---

## 2. Strategic Context: Why a Separate Quick Path

The documented deploy (steps 1–5 in `.github/docs/deploy/`) is entirely **GitHub-Actions-driven**: fork the repo, set repo Variables/Secrets, run four workflows. That path is correct for production — OIDC (no long-lived keys), environment protection, reproducibility, an audit trail.

But it's a poor fit for "let me see it running in my account today," because it requires a **fork + GitHub admin + repo secrets + GitHub Environments** before anything happens. That's "fast in a GitHub fork," not "fast in an AWS account."

**Insight that makes the quick path cheap to build:** the GitHub workflows are thin wrappers. Every step is `run: bash scripts/.../x.sh`. Running those same scripts locally with config supplied as `CDK_*` env vars (which `scripts/common/load-env.sh` already prefers over `cdk.context.json`) is the *same code path*, not a second one. The skill orchestrates the maintained scripts; it doesn't fork the logic.

> We deliberately **avoid** the stale top-level `scripts/deploy.sh` — it still describes the retired multi-stack model. The current building blocks are the per-service scripts below.

---

## 3. What "Eval-Grade / Domainless" Means

The platform already supports a no-domain mode (it's the `else` branch of the cert guard that otherwise fails `cdk synth`). Choosing it removes the heaviest prerequisites:

| Dropped prerequisite | Why it's safe to drop for eval |
|----------------------|-------------------------------|
| Custom domain | CloudFront serves the SPA on its default `*.cloudfront.net` (browser still gets HTTPS). |
| ACM certificates (ALB + CloudFront, incl. `us-east-1`) | No custom names to certify. CloudFront default cert covers the SPA; ALB runs HTTP behind CloudFront. |
| Route 53 hosted zone + nameserver delegation | No DNS records to create. |
| `artifacts.` / `mcp-sandbox.` subdomain certs (the wildcard-depth gotcha) | These origins fall back to CloudFront default domains when domainless. |
| X-Ray Transaction Search (account singleton) | Optional observability; not required to run. |

**What you give up:** vanity URL, TLS on a real name, federated SSO out of the gate, the MCP-Apps sandbox iframe (needs the `mcp-sandbox.` origin to resolve), and production-grade hardening. All are addable later by switching to the documented domained path. The skill states this plainly.

### Two domainless details that are *not* free

These are the only real engineering wrinkles, both handled by the skill:

1. **The SPA is fine as-is (good news).** The production SPA calls the API through a **relative `/api` path** (build-time `environment.appApiUrl = "/api"`), and the SPA's CloudFront distribution already routes `/api/*` to the ALB origin (`infrastructure/lib/platform-stack.ts:539`). So the **prebuilt SPA bundle works on any CloudFront domain with no per-deploy rebuild.**
2. **Cognito callback chicken-and-egg (the real wrinkle).** `cognito-construct.ts` sets the app client's callback/logout URLs to `https://{domainName}/...` only when a domain is set — **otherwise it hardcodes `http://localhost:8000/...`**. A domainless cloud deploy therefore has no working callback for its CloudFront origin until we feed it in. The CloudFront domain isn't known until after the distribution exists ⇒ the skill does a **two-pass finalize**: deploy → read `distributionDomainName` from stack outputs → `aws cognito-idp update-user-pool-client` to add `https://<cf-domain>/...` callbacks → done.

---

## 4. Dependency Target States — Answering "Only the AWS CLI?"

"Only AWS CLI" is achievable, but not in one step. The deploy has non-Docker local steps (CDK, frontend build, data seeding) that each carry their own toolchain. We propose three target states and ship iteratively:

| | **T0 — v1 (ship now)** | **T1 — reduced** | **T2 — north star** |
|---|---|---|---|
| **Local deps** | AWS CLI + Docker (buildx/QEMU) + Node/CDK + uv/Python | AWS CLI + Node/CDK (`crane` for images) | **AWS CLI only** (+ `jq`, `crane` static binary) |
| **App images** | Built locally | **Prebuilt, copied** to user ECR | Prebuilt, copied |
| **SPA** | Built locally | **Prebuilt bundle**, `s3 sync` | Prebuilt bundle, `s3 sync` |
| **Infra** | `cdk deploy` | `cdk deploy` | **Pre-synthed CFN template** → `aws cloudformation deploy` |
| **Seed data** | `seed.sh` (Python) | `seed.sh` (Python) | Seed via AWS CLI batch-writes **or** a one-shot in-stack seeding Lambda |
| **Effort** | Low — orchestrate existing scripts | Medium — add an image/SPA publish pipeline | High — template + assets publishing + seed rework |

**Recommendation:** Ship **T0** first (fastest route to a working skill, zero new pipelines), then invest in **T1's publish pipeline** because it simultaneously (a) eliminates the Mac-silicon Docker problem, (b) drops the frontend toolchain, and (c) is a prerequisite for T2. T2 is the true "only AWS CLI" experience and a clear stretch milestone.

---

## 5. The Deploy Flow the Skill Orchestrates

Four stages, mirroring the production pipeline, minus the GitHub layer:

| # | Stage | v1 command(s) | Notes |
|:-:|-------|---------------|-------|
| 0 | **Preflight** | `aws sts get-caller-identity`; tool/version checks; `npx cdk bootstrap aws://<acct>/<region>` (idempotent) | Gate hard; capture `prefix`, `region`. |
| 1 | **Platform (infra)** | `bash scripts/platform/deploy.sh` | `cdk synth` → `cdk-assets publish` (bootstrap stub images) → seed SSM image-tags → `cdk deploy <prefix>-PlatformStack`. Writes `platform-outputs.json`. |
| 2 | **Backend code** | `bash scripts/build/deploy-ecs-service-one.sh app-api`; `bash scripts/build/deploy-runtime-image-one.sh inference-api`; *(opt)* `…deploy-image-lambda-one.sh rag-ingestion`; `…deploy-zip-lambda-one.sh artifact-render` | Each is content-hash short-circuited. See §6 for the image-arch handling. |
| 3 | **Frontend** | `bash scripts/frontend/build.sh` → `bash scripts/frontend/deploy.sh` *(or T1: `s3 sync` a prebuilt bundle)* | `s3 sync` + CloudFront invalidation. |
| 4 | **Seed + finalize** | `bash scripts/stack-bootstrap/seed.sh`; then **Cognito callback patch** (see §3.2) | Idempotent seed of models/roles/quotas/tools, then wire callbacks to the CloudFront domain. |
| 5 | **Verify** | ECS `wait services-stable`; `curl` the CloudFront URL; print the URL | First-boot admin signup happens in-browser. |

**rag-ingestion is opt-in** (`--with-rag`, default off). It's the slowest image (docling + a Rust toolchain) and on an amd64 host it must be emulated to arm64. Deferring it leaves the bootstrap stub in place; basic chat is unaffected. RAG can be enabled later with one command.

---

## 6. Docker Build & Deploy — the Multi-Arch / Apple-Silicon Problem (recommended + alternatives)

### Why it's hard

The stack ships **four** build targets that do **not** share an architecture:

| Target | Required arch | Pinned in |
|--------|--------------|-----------|
| app-api (ECS Fargate) | **amd64** | `app-api-service-construct.ts` → `Platform.LINUX_AMD64` |
| inference-api (AgentCore Runtime) | **arm64** | `inference-agentcore-construct.ts` → `LINUX_ARM64` |
| rag-ingestion (Lambda) | **arm64** | `rag-ingestion-lambda-construct.ts` → `Architecture.ARM_64` |
| artifact-render (Lambda **zip**) | arm64 — **no Docker** | zipped + `aws lambda update-function-code` |

So **no single host builds everything natively** — exactly one image always needs cross-arch emulation. On **Apple Silicon (arm64)**, 3 of 4 are native and only **app-api** (a small Python image) is emulated — the *good* case. On an amd64 Linux box it's reversed, and the emulated one is the slow `rag-ingestion`.

**The Apple-Silicon trap to fix first:** `scripts/build/build-one.sh` only forces `--platform` for `inference-api`. For `app-api` and `rag-ingestion` it builds **whatever the host is**. On a Mac that means `build-one.sh app-api` silently produces an **arm64** image for an **amd64** Fargate task — a broken deploy. (This same gap looks like a **latent CI bug**: `rag-ingestion`'s Lambda is `ARM_64`, but its CI job builds amd64 — see §8.) **Enabling fix:** pin explicit per-service platforms so builds are correct on any host.

### Solutions

#### ✅ D1 — Local `buildx` + QEMU emulation, platform forced per service *(recommended for v1)*
Exactly what CI does today (`.github/actions/build-and-push-image` conditionally runs `docker/setup-qemu-action` + `setup-buildx-action` for arm64). Docker Desktop ships QEMU/binfmt by default.
- Skill ensures binfmt (`docker run --privileged --rm tonistiigi/binfmt --install all` if needed), then forces `--platform`: `app-api=linux/amd64`, `inference-api=linux/arm64`, `rag-ingestion=linux/arm64`.
- **Pros:** works today; mirrors proven CI behavior; no new infrastructure.
- **Cons:** heaviest local deps (Docker + Node/CDK + uv); emulated builds are slow (worst: `rag-ingestion` on amd64 hosts); risk of local-toolchain drift.

#### ⭐ D2 — Prebuilt, published multi-arch images, copied into the user's ECR *(recommended end-state)*
Add a release step that pushes multi-arch images to a **public registry** (Amazon ECR Public Gallery or GHCR). Quick Deploy copies them into the user's account.
- Copy with **`crane copy`** (a single static binary — *no Docker daemon required*) or `docker pull --platform … && tag && push`.
- The images are **config-free** (all runtime config is injected via task-def/Lambda env vars), so they're safe to publish once and reuse across accounts.
- **Pros:** *zero local image build*; no arch/emulation pain at all; fast; reproducible/pinned to a release; the key enabler for T1/T2 and the "AWS CLI only" goal.
- **Cons:** net-new publish pipeline; public image hosting + versioning to maintain; large images to host; must track releases.

#### D3 — Remote native build (AWS CodeBuild, arm64 + amd64 fleets, or an ephemeral right-arch runner)
Skill provisions a throwaway CodeBuild project that builds **natively** and pushes to the user's ECR.
- **Pros:** no local Docker; native arch (fast, no emulation).
- **Cons:** provisions extra infra + IAM; essentially re-implements CI; added cost/latency. Overkill for a quick-start.

#### D4 — Containerized deployer ("one local dependency = Docker")
Ship a `boisestate/agentcore-quickstart` image with `aws-cli` + `cdk` + `uv` + `node` baked in. User runs `docker run -v ~/.aws:/root/.aws … deploy`.
- **Pros:** collapses local deps to Docker + AWS creds; no toolchain install.
- **Cons:** building the *app* images from inside needs a mounted Docker socket / buildx (still emulation under the hood); DinD ergonomics.

### Recommendation
**v1 = D1** (ship the skill against today's scripts). **Commit to D2** as the strategic investment — it's the single change that removes Apple-Silicon emulation entirely *and* moves us toward "AWS CLI only." Offer **D3/D4** as documented escape hatches for users who can't/won't run local Docker.

---

## 7. The Skill Itself

A guided, gated agent skill (a `SKILL.md` under `.claude/skills/quickstart-deploy/`) that an operator runs conversationally.

**Preface (per the brief):** the skill opens by stating that prerequisites are required, that this is a **fast, single-account, eval-grade** path, and that **multi-environment / production deployments should follow `.github/docs/deploy/`**.

**Inputs (prompted, with sensible defaults):**
- `CDK_AWS_ACCOUNT` (default: from `aws sts get-caller-identity`)
- `CDK_AWS_REGION` (default: caller's configured region)
- `CDK_PROJECT_PREFIX` (default: e.g. `eval-agentcore`)
- Image strategy: `local-buildx` (D1) | `prebuilt` (D2, once available)
- `--with-rag` (default off)

**Behavior:**
1. **Preflight gate** — verify AWS creds + account match; check required tools for the chosen image strategy; confirm/run `cdk bootstrap`; warn on missing Bedrock model access.
2. **Confirm plan** — echo resolved config + the domainless simplifications, get a go/no-go.
3. **Execute** stages 1–5 from §5, streaming progress; stop on first failure with a clear remediation hint.
4. **Finalize** — patch Cognito callbacks from stack outputs; print the CloudFront URL and first-boot instructions.
5. **Teardown pointer** — reference `scripts/teardown/destroy.sh` for cleanup.

**Idempotency:** every underlying script is content-hash short-circuited or upsert-safe, so re-running the skill is safe and only ships deltas.

---

## 8. Required Enabling Changes (small, and they help CI too)

1. **Pin explicit per-service build platforms** in `scripts/build/build-one.sh` (`app-api=linux/amd64`, `rag-ingestion=linux/arm64`; `inference-api` already pinned). Makes **local builds correct on any host** and removes the Apple-Silicon trap.
2. **Fix the latent `rag-ingestion` arch mismatch** (looks like a real CI bug worth verifying independently): its Lambda is `ARM_64` but its CI build job passes no platform ⇒ builds amd64 on the amd64 runner. The §8.1 platform pin fixes it locally; the CI job also needs to request QEMU (`platform: linux/arm64`) for the build to succeed. *(Recommend tracking as its own issue.)*
3. **(T1) Image + SPA publish pipeline** — multi-arch images to ECR Public/GHCR; prebuilt SPA bundle as a release asset.
4. **(T2) Pre-synthed CFN template + asset publishing + a CLI/Lambda-based seed path** to drop the CDK and Python local deps.

---

## 9. Risks & Open Questions

- **Bedrock model access** isn't provisioned by the stack — the account must have access to the seeded default models or chat will fail at inference. Skill should check/warn.
- **`cdk bootstrap`** must run once per account/region; harmless but adds a CDK dependency for T0/T1.
- **ALB is HTTP behind CloudFront** when domainless — fine for eval, not for production. Make this explicit.
- **MCP-Apps sandbox iframe is disabled** domainless (needs the `mcp-sandbox.` origin to resolve). Acceptable for eval; call it out.
- **Cost:** the stack always provisions artifacts, the MCP sandbox, and SageMaker fine-tuning IAM. Idle cost is low but non-zero (NAT gateway, ALB, CloudFront, DynamoDB). Document expected idle cost + the teardown path.
- **Image publish hosting** (D2) — decide ECR Public vs GHCR, retention, and who owns the publish workflow.
- **Open:** do we want T0 and T1 as separate releases, or jump straight to D2 (skip local Docker entirely)? See §10.

---

## 10. Phased Plan / Milestones

| Phase | Deliverable | Gets us |
|------|-------------|---------|
| **P0 — Spike** | Run the §5 sequence by hand on a fresh account, domainless; confirm chat works end-to-end; capture the exact Cognito-callback patch. | Validated flow + a known-good command list. |
| **P1 — Enabling fixes** | §8.1 platform pins (+ track §8.2 rag bug). | Correct local builds on Apple Silicon. |
| **P2 — Skill v1 (T0/D1)** | `.claude/skills/quickstart-deploy/SKILL.md` orchestrating §5, two-pass finalize, preflight gates, preface. | A working "quick deploy" skill (local toolchain). |
| **P3 — Publish pipeline (T1/D2)** | Multi-arch image publish + prebuilt SPA; skill `prebuilt` strategy via `crane`. | Removes Docker-build + frontend toolchain; kills Apple-Silicon pain. |
| **P4 — AWS-CLI-only (T2)** | Pre-synthed template + asset publish + CLI/Lambda seed; `aws cloudformation deploy`. | The "only AWS CLI" north star. |

**Suggested cut line for a first useful release:** **P2** (skill v1). It delivers the eval-grade, domainless deploy the brief asks for. **P3** is the high-leverage follow-up.

---

## Appendix A — Quick-Deploy Prerequisite Checklist (eval-grade)

**Always required**
- [ ] AWS CLI installed and configured against the **target account** (`aws sts get-caller-identity` returns it).
- [ ] Permissions equivalent to `AdministratorAccess` (or the service list in `step-01-prerequisites.md`).
- [ ] Bedrock model access enabled for the default seeded models in the region.

**Required for v1 (T0/D1) only — removed by P3/P4**
- [ ] Docker Desktop (with `buildx` + QEMU/binfmt).
- [ ] Node.js + CDK toolchain (for `cdk deploy`).
- [ ] `uv` / Python (for data seeding).

**Not required (the point of domainless):** custom domain, ACM certs, Route 53, GitHub fork, GitHub CLI.

## Appendix B — Concrete v1 Command Sequence (reference)

```bash
# 0. Preflight
export CDK_AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_AWS_REGION=us-west-2
export CDK_PROJECT_PREFIX=eval-agentcore
# (domainless: deliberately leave CDK_DOMAIN_NAME and all *_CERTIFICATE_ARN unset)
npx cdk bootstrap aws://$CDK_AWS_ACCOUNT/$CDK_AWS_REGION   # idempotent

# 1. Platform (infra)
bash scripts/platform/deploy.sh

# 2. Backend code (platform forced per service in build-one.sh per §8.1)
bash scripts/build/deploy-ecs-service-one.sh app-api
bash scripts/build/deploy-runtime-image-one.sh inference-api
bash scripts/build/deploy-zip-lambda-one.sh artifact-render
# (optional) bash scripts/build/deploy-image-lambda-one.sh rag-ingestion

# 3. Frontend  (v1 builds locally; T1 swaps in a prebuilt bundle + s3 sync)
bash scripts/frontend/build.sh && bash scripts/frontend/deploy.sh

# 4. Seed + finalize
bash scripts/stack-bootstrap/seed.sh
#   then read distributionDomainName from infrastructure/platform-outputs.json
#   and: aws cognito-idp update-user-pool-client \
#        --user-pool-id <id> --client-id <id> \
#        --callback-urls https://<cf-domain>/api/auth/callback \
#        --logout-urls   https://<cf-domain>

# 5. Verify → open https://<cf-domain> → complete first-boot admin signup
```

*(Commands are illustrative; the skill resolves IDs from stack outputs/SSM and gates each step.)*
