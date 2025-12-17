#!/bin/bash
set -euo pipefail

# Deploy Gateway Stack
# Deploys CDK stack with pre-synthesized templates or on-the-fly synthesis

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
INFRASTRUCTURE_DIR="${PROJECT_ROOT}/infrastructure"

# Source common environment loader
source "${PROJECT_ROOT}/scripts/common/load-env.sh"

log_success() {
    echo -e "\033[0;32m✓ $1\033[0m"
}

log_warning() {
    echo -e "\033[1;33m⚠ $1\033[0m"
}

log_info "Deploying Gateway Stack..."

# ============================================================
# Validate Prerequisites
# ============================================================

log_info "Validating prerequisites..."

# Check if Google API credentials secret exists
log_info "Checking for Google API credentials in Secrets Manager..."

SECRET_NAME="${CDK_PROJECT_PREFIX}/mcp/google-credentials"

set +e
aws secretsmanager describe-secret \
    --secret-id "${SECRET_NAME}" \
    --region "${CDK_AWS_REGION}" \
    --output json > /dev/null 2>&1
SECRET_EXISTS=$?
set -e

if [ $SECRET_EXISTS -ne 0 ]; then
    log_warning "Google API credentials secret not found: ${SECRET_NAME}"
    log_warning ""
    log_warning "You must create this secret before deploying the Gateway Stack:"
    log_warning ""
    log_warning "  aws secretsmanager create-secret \\"
    log_warning "    --name \"${SECRET_NAME}\" \\"
    log_warning "    --secret-string '{\"api_key\":\"YOUR_API_KEY\",\"search_engine_id\":\"YOUR_ENGINE_ID\"}' \\"
    log_warning "    --description \"Google Custom Search API credentials\" \\"
    log_warning "    --region \"${CDK_AWS_REGION}\""
    log_warning ""
    log_warning "Get credentials from:"
    log_warning "  - API Key: https://console.cloud.google.com/apis/credentials"
    log_warning "  - Search Engine ID: https://programmablesearchengine.google.com/"
    log_warning ""
    
    read -p "Do you want to continue with deployment anyway? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        log_info "Deployment cancelled"
        exit 0
    fi
else
    log_success "Google API credentials secret found"
fi

# ============================================================
# Deploy Stack
# ============================================================

cd "${INFRASTRUCTURE_DIR}"

log_info "Deploying GatewayStack..."

# Check if pre-synthesized templates exist
if [ -d "cdk.out" ] && [ -f "cdk.out/GatewayStack.template.json" ]; then
    log_info "Using pre-synthesized templates from cdk.out/"
    
    cdk deploy GatewayStack \
        --app "cdk.out/" \
        --require-approval never \
        || {
        log_error "CDK deployment failed"
        exit 1
    }
else
    log_info "Synthesizing on-the-fly"
    
    # Build context parameters using shared helper function
    CONTEXT_PARAMS=$(build_cdk_context_params)
    
    # Execute CDK deploy with context parameters
    eval "cdk deploy GatewayStack ${CONTEXT_PARAMS} --require-approval never" || {
        log_error "CDK deployment failed"
        exit 1
    }
fi

# ============================================================
# Post-Deployment Validation
# ============================================================

log_info "Validating Gateway deployment..."

# Get Gateway ID from SSM
GATEWAY_ID=$(aws ssm get-parameter \
    --name "/${CDK_PROJECT_PREFIX}/gateway/id" \
    --region "${CDK_AWS_REGION}" \
    --query "Parameter.Value" \
    --output text 2>/dev/null || echo "")

if [ -z "${GATEWAY_ID}" ]; then
    log_warning "Could not retrieve Gateway ID from SSM"
else
    log_info "Gateway ID: ${GATEWAY_ID}"
    
    # Check Gateway status
    log_info "Checking Gateway status..."
    
    set +e
    GATEWAY_STATUS=$(aws bedrock-agentcore get-gateway \
        --gateway-identifier "${GATEWAY_ID}" \
        --region "${CDK_AWS_REGION}" \
        --query "status" \
        --output text 2>&1)
    STATUS_EXIT=$?
    set -e
    
    if [ $STATUS_EXIT -eq 0 ]; then
        log_success "Gateway Status: ${GATEWAY_STATUS}"
    else
        log_warning "Could not verify Gateway status (this is normal immediately after creation)"
    fi
fi

log_success "Gateway Stack deployment complete"

# Display usage instructions
log_info ""
log_info "============================================================"
log_info "Gateway Usage Instructions"
log_info "============================================================"
log_info ""
log_info "1. Test Gateway connectivity:"
log_info "   aws bedrock-agentcore list-gateway-targets \\"
log_info "     --gateway-identifier \${GATEWAY_ID} \\"
log_info "     --region ${CDK_AWS_REGION}"
log_info ""
log_info "2. View Gateway details in AWS Console:"
log_info "   https://console.aws.amazon.com/bedrock/home?region=${CDK_AWS_REGION}#/agentcore/gateways"
log_info ""
log_info "3. Integrate with AgentCore Runtime:"
log_info "   - Update Runtime environment with Gateway URL from SSM"
log_info "   - Ensure Runtime execution role has bedrock-agentcore:InvokeGateway permission"
log_info ""
