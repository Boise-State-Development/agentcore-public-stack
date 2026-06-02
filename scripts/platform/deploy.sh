#!/usr/bin/env bash
# scripts/platform/deploy.sh — deploy PlatformStack via CDK.
#
# Deploy ordering matters because the compute constructs (app-api
# task def, inference-api Runtime) read their container image URIs
# from SSM at CFN deploy time. The SSM paths must exist before
# CFN starts resolving template parameters; otherwise the deploy
# fails with "Unable to fetch parameters from parameter store".
#
# Steps:
#   1. cdk synth → produces cdk.out/<stack>.template.json and
#      <stack>.assets.json. Compute constructs emit
#      AppApiBootstrapImageHash + InferenceApiBootstrapImageHash
#      CfnOutputs that the seed script reads.
#   2. cdk-assets publish → pushes the bootstrap container images
#      to the cdk-assets ECR repo so the URIs the seed script
#      writes are valid.
#   3. seed-image-tags.sh → for each compute service, if the SSM
#      image-tag param doesn't exist, write the bootstrap URI to
#      it. Idempotent: skips when the param already exists (build
#      pipeline owns it after first deploy).
#   4. cdk deploy --app cdk.out/ → CFN resolves the SSM-Parameter::
#      Value<String> template params, picks up the freshly-seeded
#      bootstrap URI on first deploy or the build-pipeline-written
#      live URI on every subsequent deploy.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

source "${PROJECT_ROOT}/scripts/common/load-env.sh"

cd "${PROJECT_ROOT}/infrastructure"

# Install/refresh CDK npm deps via the centralised install script.
"${PROJECT_ROOT}/scripts/cdk/install.sh"

CDK_CONTEXT_PARAMS=$(build_cdk_context_params)

# ── 1. Synth ──
if [ -d "cdk.out" ] && [ -f "cdk.out/manifest.json" ]; then
    log_info "Using pre-synthesized template from cdk.out/"
else
    log_info "Synthesizing PlatformStack..."
    eval npx cdk synth "${CDK_PROJECT_PREFIX}-PlatformStack" \
        ${CDK_CONTEXT_PARAMS} \
        --quiet
fi

# ── 2. Publish bootstrap container images to cdk-assets ECR ──
# `cdk deploy` would do this anyway, but we need it to happen
# BEFORE the SSM seed so the URIs the seed script writes are
# valid (the cdk-assets repo only contains the image after
# publish). cdk-assets publish is idempotent — checks the ECR
# digest and skips push if already present.
log_info "Publishing cdk-assets (bootstrap container images, lambda zips)..."
npx cdk-assets publish \
    --path "cdk.out/${CDK_PROJECT_PREFIX}-PlatformStack.assets.json" \
    --verbose 2>&1 | tail -20

# ── 3. Seed SSM image-tag params on first deploy ──
bash "${PROJECT_ROOT}/scripts/stack-bootstrap/seed-image-tags.sh"

# ── 4. Deploy ──
log_info "Deploying PlatformStack..."
npx cdk deploy "${CDK_PROJECT_PREFIX}-PlatformStack" \
    --app "cdk.out/" \
    --exclusively \
    --require-approval never \
    --outputs-file "${PROJECT_ROOT}/infrastructure/platform-outputs.json"

log_info "PlatformStack deployed successfully"
