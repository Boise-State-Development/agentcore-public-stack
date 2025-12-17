#!/bin/bash
set -euo pipefail

# Test Gateway Stack
# Validates Gateway connectivity and lists available tools

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Source common environment loader
source "${PROJECT_ROOT}/scripts/common/load-env.sh"

log_success() {
    echo -e "\033[0;32m✓ $1\033[0m"
}

log_warning() {
    echo -e "\033[1;33m⚠ $1\033[0m"
}

log_info "Testing Gateway Stack..."

# ============================================================
# Get Gateway Information
# ============================================================

log_info "Retrieving Gateway information from SSM..."

# Get Gateway ID
GATEWAY_ID=$(aws ssm get-parameter \
    --name "/${CDK_PROJECT_PREFIX}/gateway/id" \
    --region "${CDK_AWS_REGION}" \
    --query "Parameter.Value" \
    --output text 2>/dev/null || echo "")

if [ -z "${GATEWAY_ID}" ]; then
    log_error "Gateway ID not found in SSM. Has the Gateway Stack been deployed?"
    exit 1
fi

log_success "Gateway ID: ${GATEWAY_ID}"

# Get Gateway URL
GATEWAY_URL=$(aws ssm get-parameter \
    --name "/${CDK_PROJECT_PREFIX}/gateway/url" \
    --region "${CDK_AWS_REGION}" \
    --query "Parameter.Value" \
    --output text 2>/dev/null || echo "")

if [ -z "${GATEWAY_URL}" ]; then
    log_warning "Gateway URL not found in SSM"
else
    log_info "Gateway URL: ${GATEWAY_URL}"
fi

# ============================================================
# Test Gateway Status
# ============================================================

log_info "Checking Gateway status..."

set +e
GATEWAY_INFO=$(aws bedrock-agentcore-control get-gateway \
    --gateway-identifier "${GATEWAY_ID}" \
    --region "${CDK_AWS_REGION}" \
    --output json 2>&1)
GET_GATEWAY_EXIT=$?
set -e

if [ $GET_GATEWAY_EXIT -ne 0 ]; then
    log_error "Failed to get Gateway information"
    log_error "Error: ${GATEWAY_INFO}"
    exit 1
fi

# Parse status
GATEWAY_STATUS=$(echo "${GATEWAY_INFO}" | jq -r '.status // "UNKNOWN"')
log_success "Gateway Status: ${GATEWAY_STATUS}"

# Display Gateway details
log_info "Gateway Details:"
echo "${GATEWAY_INFO}" | jq '{
    name: .name,
    status: .status,
    authorizerType: .authorizerType,
    protocolType: .protocolType,
    createdAt: .createdAt,
    updatedAt: .updatedAt
}'

# ============================================================
# List Gateway Targets (Tools)
# ============================================================

log_info "Listing Gateway Targets (tools)..."

set +e
TARGETS=$(aws bedrock-agentcore-control list-gateway-targets \
    --gateway-identifier "${GATEWAY_ID}" \
    --region "${CDK_AWS_REGION}" \
    --output json 2>&1)
LIST_TARGETS_EXIT=$?
set -e

if [ $LIST_TARGETS_EXIT -ne 0 ]; then
    log_error "Failed to list Gateway Targets"
    log_error "Error: ${TARGETS}"
    exit 1
fi

# Count targets
TARGET_COUNT=$(echo "${TARGETS}" | jq '.items | length')
log_success "Found ${TARGET_COUNT} Gateway Targets"

# Display targets
if [ "${TARGET_COUNT}" -gt 0 ]; then
    log_info "Available Tools:"
    echo "${TARGETS}" | jq -r '.items[] | "  - \(.name): \(.description // "No description")"'
else
    log_warning "No Gateway Targets found. This may indicate a deployment issue."
fi

# ============================================================
# Test Lambda Function
# ============================================================

log_info "Testing Lambda function..."

FUNCTION_NAME="${CDK_PROJECT_PREFIX}-mcp-google-search"

set +e
FUNCTION_INFO=$(aws lambda get-function \
    --function-name "${FUNCTION_NAME}" \
    --region "${CDK_AWS_REGION}" \
    --output json 2>&1)
GET_FUNCTION_EXIT=$?
set -e

if [ $GET_FUNCTION_EXIT -ne 0 ]; then
    log_error "Lambda function not found: ${FUNCTION_NAME}"
    log_error "Error: ${FUNCTION_INFO}"
    exit 1
fi

FUNCTION_STATE=$(echo "${FUNCTION_INFO}" | jq -r '.Configuration.State // "UNKNOWN"')
log_success "Lambda Function State: ${FUNCTION_STATE}"

if [ "${FUNCTION_STATE}" != "Active" ]; then
    log_warning "Lambda function is not Active. Current state: ${FUNCTION_STATE}"
fi

# ============================================================
# Summary
# ============================================================

log_info ""
log_info "============================================================"
log_info "Gateway Stack Test Summary"
log_info "============================================================"
log_success "✓ Gateway is accessible (Status: ${GATEWAY_STATUS})"
log_success "✓ Gateway has ${TARGET_COUNT} targets configured"
log_success "✓ Lambda function is deployed (State: ${FUNCTION_STATE})"
log_info ""
log_success "All Gateway Stack tests passed!"
