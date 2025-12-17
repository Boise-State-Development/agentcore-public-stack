#!/bin/bash

#============================================================
# Inference API Stack - Synthesize
# 
# Synthesizes the Inference API Stack CloudFormation template
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
# Synthesize Inference API Stack
# ===========================================================

log_info "Synthesizing Inference API Stack CloudFormation template..."
cd "${PROJECT_ROOT}/infrastructure"

# Ensure dependencies are installed
if [ ! -d "node_modules" ]; then
    log_info "node_modules not found in CDK directory. Installing dependencies..."
    npm install
fi

# Synthesize the Inference API Stack
log_info "Running CDK synth for InferenceApiStack..."
cdk synth InferenceApiStack \
    --context environment="${DEPLOY_ENVIRONMENT}" \
    --context projectPrefix="${CDK_PROJECT_PREFIX}" \
    --context awsAccount="${CDK_AWS_ACCOUNT}" \
    --context awsRegion="${CDK_AWS_REGION}" \
    --context vpcCidr="${CDK_VPC_CIDR}" \
    --context infrastructureHostedZoneDomain="${CDK_HOSTED_ZONE_DOMAIN}" \
    --context inferenceApi.enabled="${CDK_INFERENCE_API_ENABLED:-true}" \
    --context inferenceApi.cpu="${CDK_INFERENCE_API_CPU:-1024}" \
    --context inferenceApi.memory="${CDK_INFERENCE_API_MEMORY:-2048}" \
    --context inferenceApi.desiredCount="${CDK_INFERENCE_API_DESIRED_COUNT:-1}" \
    --context inferenceApi.maxCapacity="${CDK_INFERENCE_API_MAX_CAPACITY:-5}" \
    --context inferenceApi.enableGpu="${CDK_INFERENCE_API_ENABLE_GPU:-false}" \
    --context inferenceApi.enableAuthentication="${ENABLE_AUTHENTICATION:-true}" \
    --context inferenceApi.logLevel="${LOG_LEVEL:-INFO}" \
    --context inferenceApi.uploadDir="${UPLOAD_DIR:-uploads}" \
    --context inferenceApi.outputDir="${OUTPUT_DIR:-output}" \
    --context inferenceApi.generatedImagesDir="${GENERATED_IMAGES_DIR:-generated_images}" \
    --context inferenceApi.apiUrl="${API_URL:-}" \
    --context inferenceApi.frontendUrl="${FRONTEND_URL:-}" \
    --context inferenceApi.corsOrigins="${CORS_ORIGINS:-}" \
    --context inferenceApi.tavilyApiKey="${TAVILY_API_KEY:-}" \
    --context inferenceApi.novaActApiKey="${NOVA_ACT_API_KEY:-}" \
    --output "${PROJECT_ROOT}/infrastructure/cdk.out"

log_success "Inference API Stack CloudFormation template synthesized successfully"
log_info "Template output directory: infrastructure/cdk.out"

# Display the synthesized stacks
if [ -d "${PROJECT_ROOT}/infrastructure/cdk.out" ]; then
    log_info "Synthesized stacks:"
    ls -lh "${PROJECT_ROOT}/infrastructure/cdk.out"/*.template.json 2>/dev/null || log_info "No template files found"
fi
