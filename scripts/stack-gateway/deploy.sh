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

log_success "Gateway Stack deployment complete"

# Display usage instructions
log_info ""
log_info "============================================================"
log_info "Gateway Usage Instructions"
log_info "============================================================"
log_info ""
log_warning "⚠️  IMPORTANT: Update Google API credentials before using search tools"
log_info ""
log_info "The secret was created with placeholder values. Update with real credentials:"
log_info ""
log_info "  aws secretsmanager put-secret-value \\"
log_info "    --secret-id ${CDK_PROJECT_PREFIX}/mcp/google-credentials \\"
log_info "    --secret-string '{\"api_key\":\"YOUR_API_KEY\",\"search_engine_id\":\"YOUR_ENGINE_ID\"}' \\"
log_info "    --region ${CDK_AWS_REGION}"
log_info ""
log_info "Get credentials from:"
log_info "  - API Key: https://console.cloud.google.com/apis/credentials"
log_info "  - Search Engine ID: https://programmablesearchengine.google.com/"
log_info ""
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
