#!/bin/bash

#============================================================
# App API Stack - Synthesize
# 
# Synthesizes the App API Stack CloudFormation template
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
# Synthesize App API Stack
# ===========================================================

log_info "Synthesizing App API Stack CloudFormation template..."
cd "${PROJECT_ROOT}/infrastructure"

# Ensure dependencies are installed
if [ ! -d "node_modules" ]; then
    log_info "node_modules not found in CDK directory. Installing dependencies..."
    npm install
fi

# Synthesize the App API Stack
log_info "Running CDK synth for AppApiStack..."
cdk synth AppApiStack \
    --context environment="${DEPLOY_ENVIRONMENT}" \
    --context projectPrefix="${CDK_PROJECT_PREFIX}" \
    --context awsAccount="${CDK_AWS_ACCOUNT}" \
    --context awsRegion="${CDK_AWS_REGION}" \
    --context vpcCidr="${CDK_VPC_CIDR}" \
    --context infrastructureHostedZoneDomain="${CDK_HOSTED_ZONE_DOMAIN}" \
    --context appApi.enabled="${CDK_APP_API_ENABLED}" \
    --context appApi.cpu="${CDK_APP_API_CPU}" \
    --context appApi.memory="${CDK_APP_API_MEMORY}" \
    --context appApi.desiredCount="${CDK_APP_API_DESIRED_COUNT}" \
    --context appApi.maxCapacity="${CDK_APP_API_MAX_CAPACITY}" \
    --output "${PROJECT_ROOT}/infrastructure/cdk.out"

log_success "App API Stack CloudFormation template synthesized successfully"
log_info "Template output directory: infrastructure/cdk.out"

# Display the synthesized stacks
if [ -d "${PROJECT_ROOT}/infrastructure/cdk.out" ]; then
    log_info "Synthesized stacks:"
    ls -lh "${PROJECT_ROOT}/infrastructure/cdk.out"/*.template.json 2>/dev/null || log_info "No template files found"
fi
