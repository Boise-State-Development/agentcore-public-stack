#!/bin/bash
set -euo pipefail

# Synthesize CloudFormation for Gateway Stack
# Generates CloudFormation templates with all context parameters

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
INFRASTRUCTURE_DIR="${PROJECT_ROOT}/infrastructure"

# Source common environment loader
source "${PROJECT_ROOT}/scripts/common/load-env.sh"

log_info "Synthesizing Gateway Stack..."

# ============================================================
# Synthesize CloudFormation Templates
# ============================================================

cd "${INFRASTRUCTURE_DIR}"

# Get stack name
STACK_NAME="${CDK_PROJECT_PREFIX}-GatewayStack"

log_info "Synthesizing ${STACK_NAME}..."

cdk synth "${STACK_NAME}" \
    --context projectPrefix="${CDK_PROJECT_PREFIX}" \
    --context awsRegion="${CDK_AWS_REGION}" \
    --context awsAccount="${CDK_AWS_ACCOUNT}" \
    --context environment="${CDK_ENVIRONMENT}" \
    --context gateway.enabled="${CDK_GATEWAY_ENABLED:-true}" \
    --context gateway.apiType="${CDK_GATEWAY_API_TYPE:-HTTP}" \
    --context gateway.throttleRateLimit="${CDK_GATEWAY_THROTTLE_RATE_LIMIT:-10000}" \
    --context gateway.throttleBurstLimit="${CDK_GATEWAY_THROTTLE_BURST_LIMIT:-5000}" \
    --context gateway.enableWaf="${CDK_GATEWAY_ENABLE_WAF:-false}" \
    --context gateway.logLevel="${CDK_GATEWAY_LOG_LEVEL:-INFO}" \
    || {
    log_error "CDK synth failed for ${STACK_NAME}"
    exit 1
}

log_success "Gateway Stack synthesized successfully"
log_info "CloudFormation templates are in: ${INFRASTRUCTURE_DIR}/cdk.out/"
