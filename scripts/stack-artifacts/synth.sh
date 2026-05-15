#!/bin/bash

#============================================================
# Artifacts Stack - Synthesize
#
# Synthesizes the Artifacts Stack CloudFormation template.
# Skips silently if CDK_ARTIFACTS_ENABLED is false so the workflow
# can run unconditionally before the feature is turned on.
#============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

source "${PROJECT_ROOT}/scripts/common/load-env.sh"

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

if [ "${CDK_ARTIFACTS_ENABLED:-false}" != "true" ]; then
    log_info "CDK_ARTIFACTS_ENABLED is not 'true' — Artifacts Stack is disabled; skipping synth."
    exit 0
fi

log_info "Synthesizing Artifacts Stack CloudFormation template..."
cd "${PROJECT_ROOT}/infrastructure"

if [ ! -d "node_modules" ]; then
    log_info "node_modules not found in CDK directory. Installing dependencies..."
    npm ci
fi

log_info "Running CDK synth for ArtifactsStack..."

CONTEXT_PARAMS=$(build_cdk_context_params)

eval "cdk synth ArtifactsStack ${CONTEXT_PARAMS} --output \"${PROJECT_ROOT}/infrastructure/cdk.out\""

log_success "Artifacts Stack CloudFormation template synthesized successfully"

if [ -d "${PROJECT_ROOT}/infrastructure/cdk.out" ]; then
    log_info "Synthesized stacks:"
    ls -lh "${PROJECT_ROOT}/infrastructure/cdk.out"/*.template.json 2>/dev/null || log_info "No template files found"
fi
