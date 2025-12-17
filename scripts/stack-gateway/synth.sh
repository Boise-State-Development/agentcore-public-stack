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

# Ensure dependencies are installed
if [ ! -d "node_modules" ]; then
    log_info "node_modules not found in CDK directory. Installing dependencies..."
    npm install
fi

log_info "Running CDK synth for GatewayStack..."

# Build context parameters using shared helper function
CONTEXT_PARAMS=$(build_cdk_context_params)

# Execute CDK synth with context parameters
eval "cdk synth GatewayStack ${CONTEXT_PARAMS}" || {
    log_error "CDK synth failed for GatewayStack"
    exit 1
}

log_success "Gateway Stack synthesized successfully"
log_info "CloudFormation templates are in: ${INFRASTRUCTURE_DIR}/cdk.out/"
