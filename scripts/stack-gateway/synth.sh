#!/bin/bash
set -euo pipefail

# Synthesize CloudFormation for Gateway Stack
# Generates CloudFormation templates with all context parameters

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
INFRASTRUCTURE_DIR="${PROJECT_ROOT}/infrastructure"

# Source common environment loader
source "${PROJECT_ROOT}/scripts/common/load-env.sh"

log_success() {
    echo -e "\033[0;32m✓ $1\033[0m"
}

log_info "Synthesizing Gateway Stack..."

# ============================================================
# Synthesize CloudFormation Templates
# ============================================================

cd "${INFRASTRUCTURE_DIR}"

# Get stack name
STACK_NAME="${CDK_PROJECT_PREFIX}-GatewayStack"

log_info "Synthesizing ${STACK_NAME}..."

# Build context parameters using shared helper function
CONTEXT_PARAMS=$(build_cdk_context_params)

# Execute CDK synth with context parameters
eval "cdk synth \"${STACK_NAME}\" ${CONTEXT_PARAMS}" || {
    log_error "CDK synth failed for ${STACK_NAME}"
    exit 1
}

log_success "Gateway Stack synthesized successfully"
log_info "CloudFormation templates are in: ${INFRASTRUCTURE_DIR}/cdk.out/"
