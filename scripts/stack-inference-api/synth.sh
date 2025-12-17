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
    --context inferenceApi="{\"enableAuthentication\":\"${ENABLE_AUTHENTICATION}\",\"logLevel\":\"${LOG_LEVEL}\",\"uploadDir\":\"${UPLOAD_DIR}\",\"outputDir\":\"${OUTPUT_DIR}\",\"generatedImagesDir\":\"${GENERATED_IMAGES_DIR}\",\"apiUrl\":\"${API_URL}\",\"frontendUrl\":\"${FRONTEND_URL}\",\"corsOrigins\":\"${CORS_ORIGINS}\",\"tavilyApiKey\":\"${TAVILY_API_KEY}\",\"novaActApiKey\":\"${NOVA_ACT_API_KEY}\"}" \
    --output "${PROJECT_ROOT}/infrastructure/cdk.out"

log_success "Inference API Stack CloudFormation template synthesized successfully"
log_info "Template output directory: infrastructure/cdk.out"

# Display the synthesized stacks
if [ -d "${PROJECT_ROOT}/infrastructure/cdk.out" ]; then
    log_info "Synthesized stacks:"
    ls -lh "${PROJECT_ROOT}/infrastructure/cdk.out"/*.template.json 2>/dev/null || log_info "No template files found"
fi
