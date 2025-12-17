#!/bin/bash

#============================================================
# Frontend Stack - Synthesize
# 
# Synthesizes the Frontend Stack CloudFormation template
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
# Synthesize Frontend Stack
# ===========================================================

log_info "Synthesizing Frontend Stack CloudFormation template..."
cd "${PROJECT_ROOT}/infrastructure"

# Ensure dependencies are installed
if [ ! -d "node_modules" ]; then
    log_info "node_modules not found in CDK directory. Installing dependencies..."
    npm install
fi

# Synthesize the Frontend Stack
log_info "Running CDK synth for FrontendStack..."
cdk synth FrontendStack \
    --context environment="${DEPLOY_ENVIRONMENT}" \
    --context projectPrefix="${CDK_PROJECT_PREFIX}" \
    --context awsAccount="${CDK_AWS_ACCOUNT}" \
    --context awsRegion="${CDK_AWS_REGION}" \
    --context vpcCidr="${CDK_VPC_CIDR}" \
    --context infrastructureHostedZoneDomain="${CDK_HOSTED_ZONE_DOMAIN}" \
    --context frontend.domainName="${CDK_FRONTEND_DOMAIN_NAME}" \
    --context frontend.enableRoute53="${CDK_FRONTEND_ENABLE_ROUTE53}" \
    --context frontend.certificateArn="${CDK_FRONTEND_CERTIFICATE_ARN}" \
    --context frontend.bucketName="${CDK_FRONTEND_BUCKET_NAME}" \
    --context frontend.enabled="${CDK_FRONTEND_ENABLED}" \
    --context frontend.cloudFrontPriceClass="${CDK_FRONTEND_CLOUDFRONT_PRICE_CLASS}" \
    --output "${PROJECT_ROOT}/infrastructure/cdk.out"

log_success "Frontend Stack CloudFormation template synthesized successfully"
log_info "Template output directory: infrastructure/cdk.out"

# Display the synthesized stacks
if [ -d "${PROJECT_ROOT}/infrastructure/cdk.out" ]; then
    log_info "Synthesized stacks:"
    ls -lh "${PROJECT_ROOT}/infrastructure/cdk.out"/*.template.json 2>/dev/null || log_info "No template files found"
fi
