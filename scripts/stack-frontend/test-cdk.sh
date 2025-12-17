#!/bin/bash

#============================================================
# Frontend Stack - Test CDK
# 
# Validates the synthesized CloudFormation template by running
# cdk diff against the deployed stack.
#============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Source common utilities
source "${PROJECT_ROOT}/scripts/common/load-env.sh"

# Additional logging function
log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

# ===========================================================
# Validate CloudFormation Template
# ===========================================================

log_info "Validating synthesized CloudFormation template..."
cd "${PROJECT_ROOT}/infrastructure"

# Check if synthesized template exists
if [ ! -d "cdk.out" ] || [ ! -f "cdk.out/FrontendStack.template.json" ]; then
    log_error "Synthesized template not found. Run synth.sh first."
    exit 1
fi

log_info "Running cdk diff to compare synthesized template with deployed stack..."

# Run cdk diff using the pre-synthesized template
# This will show what would change if we deployed
cdk diff FrontendStack \
    --app "cdk.out/"

log_success "CloudFormation template validation completed"
