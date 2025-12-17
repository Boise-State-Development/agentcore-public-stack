#!/bin/bash
set -euo pipefail

# Test Gateway Stack CDK
# Validates CloudFormation templates with cdk diff

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
INFRASTRUCTURE_DIR="${PROJECT_ROOT}/infrastructure"

# Source common environment loader
source "${PROJECT_ROOT}/scripts/common/load-env.sh"

log_success() {
    echo -e "\033[0;32m✓ $1\033[0m"
}

log_info "Testing Gateway Stack..."

# ============================================================
# Validate with CDK Diff
# ============================================================

cd "${INFRASTRUCTURE_DIR}"

# Check if synthesized template exists
if [ ! -d "cdk.out" ] || [ ! -f "cdk.out/GatewayStack.template.json" ]; then
    log_error "Synthesized template not found. Run synth.sh first."
    exit 1
fi

log_info "Running cdk diff to compare synthesized template with deployed stack..."

# Run cdk diff using the pre-synthesized template
# This will show what would change if we deployed
cdk diff GatewayStack \
    --app "cdk.out/"

log_success "Gateway Stack validation complete"
