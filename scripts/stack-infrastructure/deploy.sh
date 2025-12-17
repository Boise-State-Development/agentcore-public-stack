#!/bin/bash

#============================================================
# Infrastructure Stack - Deploy
# 
# Deploys the Infrastructure Stack (VPC, ALB, ECS Cluster)
# 
# This stack MUST be deployed FIRST before any application stacks.
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
# Deploy Infrastructure Stack
# ===========================================================

log_info "Deploying Infrastructure Stack..."
cd "${PROJECT_ROOT}/infrastructure"

# Ensure dependencies are installed
if [ ! -d "node_modules" ]; then
    log_info "node_modules not found in CDK directory. Installing dependencies..."
    npm install
fi

# Bootstrap CDK (if not already bootstrapped)
# Run from parent directory to avoid loading the CDK app
log_info "Ensuring CDK is bootstrapped..."
cd "${PROJECT_ROOT}"
cdk bootstrap aws://${CDK_DEFAULT_ACCOUNT}/${CDK_DEFAULT_REGION} \
    || log_info "CDK already bootstrapped or bootstrap failed (continuing anyway)"
cd "${PROJECT_ROOT}/infrastructure"

# Deploy the Infrastructure Stack
# Check if pre-synthesized template exists (from CI/CD pipeline)
if [ -d "${PROJECT_ROOT}/infrastructure/cdk.out" ] && [ -f "${PROJECT_ROOT}/infrastructure/cdk.out/InfrastructureStack.template.json" ]; then
    log_info "Using pre-synthesized CloudFormation template from cdk.out/..."
    log_info "Deploying InfrastructureStack from pre-synthesized template..."
    cdk deploy InfrastructureStack \
        --app "cdk.out/" \
        --require-approval never \
        --outputs-file "${PROJECT_ROOT}/infrastructure/infrastructure-outputs.json"
else
    log_info "No pre-synthesized template found. Synthesizing and deploying..."
    log_info "Deploying InfrastructureStack..."
    cdk deploy InfrastructureStack \
        --context environment="${DEPLOY_ENVIRONMENT}" \
        --context projectPrefix="${CDK_PROJECT_PREFIX}" \
        --context awsAccount="${CDK_AWS_ACCOUNT}" \
        --context awsRegion="${CDK_AWS_REGION}" \
        --context vpcCidr="${CDK_VPC_CIDR}" \
        --context infrastructureHostedZoneDomain="${CDK_HOSTED_ZONE_DOMAIN}" \
        --require-approval never \
        --outputs-file "${PROJECT_ROOT}/infrastructure/infrastructure-outputs.json"
fi

log_success "Infrastructure Stack deployed successfully"

# Display stack outputs
log_info "Stack outputs saved to infrastructure/infrastructure-outputs.json"

if [ -f "${PROJECT_ROOT}/infrastructure/infrastructure-outputs.json" ]; then
    log_info "Infrastructure Stack Outputs:"
    cat "${PROJECT_ROOT}/infrastructure/infrastructure-outputs.json"
fi
