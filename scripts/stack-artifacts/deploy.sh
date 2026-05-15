#!/bin/bash

#============================================================
# Artifacts Stack - Deploy
#
# Deploys the Artifacts Stack (DDB, S3, CloudFront, Lambda).
#
# Deploy order: Infrastructure → Artifacts → (Inference API, App API,
# Frontend). Parallel-safe with RAG Ingestion and Fine-Tuning.
#============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

source "${PROJECT_ROOT}/scripts/common/load-env.sh"
source "${PROJECT_ROOT}/scripts/common/recover-stack.sh"

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

if [ "${CDK_ARTIFACTS_ENABLED:-false}" != "true" ]; then
    log_info "CDK_ARTIFACTS_ENABLED is not 'true' — Artifacts Stack is disabled; skipping deploy."
    exit 0
fi

log_info "Deploying Artifacts Stack..."
cd "${PROJECT_ROOT}/infrastructure"

if [ ! -d "node_modules" ]; then
    log_info "node_modules not found in CDK directory. Installing dependencies..."
    npm ci
fi

# Recover from DELETE_FAILED state if a previous teardown left the stack broken.
recover_delete_failed_stack "${CDK_PROJECT_PREFIX}-ArtifactsStack"

CONTEXT_PARAMS=$(build_cdk_context_params)

# Prefer the pre-synthesized template when available (CI path) so deploy
# matches exactly what was reviewed in cdk diff.
if [ -d "cdk.out" ] && [ -f "cdk.out/ArtifactsStack.template.json" ]; then
    log_info "Using pre-synthesized template from cdk.out/"
    eval "cdk deploy ArtifactsStack ${CONTEXT_PARAMS} \
        --app \"cdk.out/\" \
        --require-approval never \
        --outputs-file artifacts-outputs.json"
else
    log_info "Synthesizing and deploying in one step..."
    eval "cdk deploy ArtifactsStack ${CONTEXT_PARAMS} \
        --require-approval never \
        --outputs-file artifacts-outputs.json"
fi

log_success "Artifacts Stack deployed"

if [ -f "artifacts-outputs.json" ]; then
    log_info "Stack outputs:"
    cat artifacts-outputs.json
fi
