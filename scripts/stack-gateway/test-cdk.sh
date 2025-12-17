#!/bin/bash
set -euo pipefail

# Test Gateway Stack CDK
# Validates CloudFormation templates with cdk diff

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
INFRASTRUCTURE_DIR="${PROJECT_ROOT}/infrastructure"

# Source common environment loader
source "${PROJECT_ROOT}/scripts/common/load-env.sh"

log_info "Testing Gateway Stack..."

# ============================================================
# Validate with CDK Diff
# ============================================================

cd "${INFRASTRUCTURE_DIR}"

# Get stack name
STACK_NAME="${CDK_PROJECT_PREFIX}-GatewayStack"

log_info "Running cdk diff for ${STACK_NAME}..."

# Check if pre-synthesized templates exist
if [ -d "cdk.out" ] && [ -f "cdk.out/${STACK_NAME}.template.json" ]; then
    log_info "Using pre-synthesized templates from cdk.out/"
    
    # Run diff using pre-synthesized templates
    cdk diff "${STACK_NAME}" --app "cdk.out/" || {
        EXIT_CODE=$?
        if [ $EXIT_CODE -ne 0 ]; then
            log_error "cdk diff failed with exit code $EXIT_CODE"
            exit $EXIT_CODE
        fi
    }
else
    log_info "No pre-synthesized templates found, synthesizing on-the-fly"
    
    # Run diff with explicit context
    cdk diff "${STACK_NAME}" \
        --context projectPrefix="${CDK_PROJECT_PREFIX}" \
        --context awsRegion="${CDK_AWS_REGION}" \
        --context awsAccount="${CDK_AWS_ACCOUNT}" \
        --context environment="${CDK_ENVIRONMENT}" \
        || {
        EXIT_CODE=$?
        if [ $EXIT_CODE -ne 0 ]; then
            log_error "cdk diff failed with exit code $EXIT_CODE"
            exit $EXIT_CODE
        fi
    }
fi

log_success "Gateway Stack validation complete"
