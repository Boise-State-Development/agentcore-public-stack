#!/bin/bash

#============================================================
# SageMaker Fine-Tuning Stack - Synthesize
#
# Synthesizes the SageMaker Fine-Tuning Stack CloudFormation template
#
# This creates the CloudFormation template without deploying it.
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
# Check if stack is enabled
# ===========================================================

if [ "${CDK_FINE_TUNING_ENABLED}" != "true" ] && [ "${CDK_FINE_TUNING_ENABLED}" != "1" ]; then
    log_info "SageMaker Fine-Tuning stack is disabled (CDK_FINE_TUNING_ENABLED=${CDK_FINE_TUNING_ENABLED:-<not set>}). Skipping synth."
    exit 0
fi

# ===========================================================
# Synthesize SageMaker Fine-Tuning Stack
# ===========================================================

log_info "Synthesizing SageMaker Fine-Tuning Stack CloudFormation template..."
cd "${PROJECT_ROOT}/infrastructure"

# Ensure dependencies are installed
if [ ! -d "node_modules" ]; then
    log_info "node_modules not found in CDK directory. Installing dependencies..."
    npm ci
fi

# Synthesize the SageMaker Fine-Tuning Stack
log_info "Running CDK synth for SageMakerFineTuningStack..."

# Build context parameters using shared helper function
CONTEXT_PARAMS=$(build_cdk_context_params)

# Execute CDK synth with context parameters
eval "cdk synth SageMakerFineTuningStack ${CONTEXT_PARAMS} --output \"${PROJECT_ROOT}/infrastructure/cdk.out\""

log_success "SageMaker Fine-Tuning Stack CloudFormation template synthesized successfully"
log_info "Template output directory: infrastructure/cdk.out"

# Display the synthesized stacks
if [ -d "${PROJECT_ROOT}/infrastructure/cdk.out" ]; then
    log_info "Synthesized stacks:"
    ls -lh "${PROJECT_ROOT}/infrastructure/cdk.out"/*.template.json 2>/dev/null || log_info "No template files found"
fi
