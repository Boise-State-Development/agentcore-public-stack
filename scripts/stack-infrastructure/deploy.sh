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
source "${PROJECT_ROOT}/scripts/common/recover-stack.sh"

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
    npm ci
fi

# Bootstrap CDK (if not already bootstrapped)
# Run from parent directory to avoid loading the CDK app
log_info "Ensuring CDK is bootstrapped..."
cd "${PROJECT_ROOT}"
cdk bootstrap aws://${CDK_DEFAULT_ACCOUNT}/${CDK_DEFAULT_REGION} \
    || log_info "CDK already bootstrapped or bootstrap failed (continuing anyway)"
cd "${PROJECT_ROOT}/infrastructure"

# Build existing VPC context parameters (only when set)
EXISTING_VPC_CONTEXT=""
if [ -n "${CDK_EXISTING_VPC_ID:-}" ]; then
    EXISTING_VPC_CONTEXT="${EXISTING_VPC_CONTEXT} --context existingVpc.vpcId=\"${CDK_EXISTING_VPC_ID}\""
    EXISTING_VPC_CONTEXT="${EXISTING_VPC_CONTEXT} --context existingVpc.availabilityZones=\"${CDK_EXISTING_VPC_AZS}\""
    EXISTING_VPC_CONTEXT="${EXISTING_VPC_CONTEXT} --context existingVpc.publicSubnetIds=\"${CDK_EXISTING_VPC_PUBLIC_SUBNET_IDS}\""
    EXISTING_VPC_CONTEXT="${EXISTING_VPC_CONTEXT} --context existingVpc.privateSubnetIds=\"${CDK_EXISTING_VPC_PRIVATE_SUBNET_IDS}\""
    if [ -n "${CDK_EXISTING_VPC_CIDR:-}" ]; then
        EXISTING_VPC_CONTEXT="${EXISTING_VPC_CONTEXT} --context existingVpc.vpcCidrBlock=\"${CDK_EXISTING_VPC_CIDR}\""
    fi
fi

# Deploy the Infrastructure Stack
# Recover from DELETE_FAILED state if a previous teardown left the stack broken
recover_delete_failed_stack "${CDK_PROJECT_PREFIX}-InfrastructureStack"

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
        --context projectPrefix="${CDK_PROJECT_PREFIX}" \
        --context awsAccount="${CDK_AWS_ACCOUNT}" \
        --context awsRegion="${CDK_AWS_REGION}" \
        --context appVersion="${CDK_APP_VERSION}" \
        --context vpcCidr="${CDK_VPC_CIDR}" \
        --context infrastructureHostedZoneDomain="${CDK_HOSTED_ZONE_DOMAIN}" \
        --context albSubdomain="${CDK_ALB_SUBDOMAIN}" \
        --context certificateArn="${CDK_CERTIFICATE_ARN}" \
        --context domainName="${CDK_DOMAIN_NAME}" \
        ${EXISTING_VPC_CONTEXT} \
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
