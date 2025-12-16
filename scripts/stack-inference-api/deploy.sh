#!/bin/bash
set -euo pipefail

# Script: Deploy Inference API Infrastructure
# Description: Deploys CDK infrastructure and pushes Docker image to ECR

# Get the directory of this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
INFRASTRUCTURE_DIR="${PROJECT_ROOT}/infrastructure"

# Source common utilities
source "${PROJECT_ROOT}/scripts/common/load-env.sh"

# Logging functions
log_info() {
    echo "[INFO] $1"
}

log_error() {
    echo "[ERROR] $1" >&2
}

log_success() {
    echo "[SUCCESS] $1"
}

# Function to validate AgentCore Runtime health
validate_runtime_health() {
    local runtime_url=$1
    local max_retries=12
    local retry_count=0
    local wait_time=10
    
    log_info "Validating AgentCore Runtime health at: ${runtime_url}"
    
    while [ ${retry_count} -lt ${max_retries} ]; do
        log_info "Health check attempt $((retry_count + 1))/${max_retries}..."
        
        set +e
        response=$(curl -s -o /dev/null -w "%{http_code}" "${runtime_url}/ping" 2>/dev/null)
        local exit_code=$?
        set -e
        
        if [ ${exit_code} -eq 0 ] && [ "${response}" = "200" ]; then
            log_success "AgentCore Runtime is healthy!"
            return 0
        fi
        
        retry_count=$((retry_count + 1))
        if [ ${retry_count} -lt ${max_retries} ]; then
            log_info "Runtime not ready yet. Waiting ${wait_time} seconds before retry..."
            sleep ${wait_time}
        fi
    done
    
    log_error "AgentCore Runtime health check failed after ${max_retries} attempts"
    return 1
}

main() {
    log_info "Deploying Inference API Stack..."
    
    # Configuration already loaded by sourcing load-env.sh
    
    # Validate required environment variables
    if [ -z "${CDK_AWS_ACCOUNT}" ]; then
        log_error "CDK_AWS_ACCOUNT is not set"
        exit 1
    fi
    
    if [ -z "${CDK_AWS_REGION}" ]; then
        log_error "CDK_AWS_REGION is not set"
        exit 1
    fi
    
    # Change to infrastructure directory
    cd "${INFRASTRUCTURE_DIR}"
    
    # Check if node_modules exists
    if [ ! -d "node_modules" ]; then
        log_info "node_modules not found in CDK directory. Installing dependencies..."
        npm install
    fi
    
    # Bootstrap CDK if needed (idempotent operation)
    log_info "Ensuring CDK is bootstrapped..."
    npx cdk bootstrap "aws://${CDK_AWS_ACCOUNT}/${CDK_AWS_REGION}"
    
    # Deploy CDK stack
    log_info "Deploying InferenceApiStack with CDK..."
    
    # Use CDK_REQUIRE_APPROVAL env var with fallback to never
    REQUIRE_APPROVAL="${CDK_REQUIRE_APPROVAL:-never}"
    
    npx cdk deploy InferenceApiStack \
        --require-approval ${REQUIRE_APPROVAL} \
        --context environment="${DEPLOY_ENVIRONMENT}" \
        --context projectPrefix="${CDK_PROJECT_PREFIX}" \
        --context awsAccount="${CDK_AWS_ACCOUNT}" \
        --context awsRegion="${CDK_AWS_REGION}" \
        --context inferenceApi="{\"enableAuthentication\":\"${ENABLE_AUTHENTICATION}\",\"logLevel\":\"${LOG_LEVEL}\",\"uploadDir\":\"${UPLOAD_DIR}\",\"outputDir\":\"${OUTPUT_DIR}\",\"generatedImagesDir\":\"${GENERATED_IMAGES_DIR}\",\"apiUrl\":\"${API_URL}\",\"frontendUrl\":\"${FRONTEND_URL}\",\"corsOrigins\":\"${CORS_ORIGINS}\",\"tavilyApiKey\":\"${TAVILY_API_KEY}\",\"novaActApiKey\":\"${NOVA_ACT_API_KEY}\"}" \
        --outputs-file "${PROJECT_ROOT}/cdk-outputs-inference-api.json"
    
    log_success "CDK deployment completed successfully"
    
    # Construct ECR repository URI (no longer stored in SSM)
    REPO_NAME="${CDK_PROJECT_PREFIX}-inference-api"
    ECR_URI="${CDK_AWS_ACCOUNT}.dkr.ecr.${CDK_AWS_REGION}.amazonaws.com/${REPO_NAME}"
    
    log_info "ECR Repository URI: ${ECR_URI}"
    
    # Validate that IMAGE_TAG is set (should be passed from build job)
    if [ -z "${IMAGE_TAG:-}" ]; then
        log_error "IMAGE_TAG is not set. This should be the version tag from the build step."
        exit 1
    fi
    
    log_info "Using pre-built image with version tag: ${IMAGE_TAG}"
    log_info "Image URI: ${ECR_URI}:${IMAGE_TAG}"
    
    # Get AgentCore Runtime URL from outputs
    if [ -f "${PROJECT_ROOT}/cdk-outputs-inference-api.json" ]; then
        RUNTIME_URL=$(jq -r ".InferenceApiStack.InferenceApiRuntimeUrl // empty" "${PROJECT_ROOT}/cdk-outputs-inference-api.json")
        
        if [ -n "${RUNTIME_URL}" ]; then
            log_info "AgentCore Runtime URL: ${RUNTIME_URL}"
            
            # Validate runtime health (with retries)
            if validate_runtime_health "${RUNTIME_URL}"; then
                log_success "AgentCore Runtime is accessible and healthy"
            else
                log_error "AgentCore Runtime health check failed. Please check CloudWatch Logs."
                exit 1
            fi
        else
            log_info "Note: Runtime URL not found in CDK outputs (may be first deployment)"
        fi
    fi
    
    log_success "Inference API deployment completed successfully!"
    log_info ""
    log_info "Next steps:"
    log_info "  1. Check AgentCore Runtime status in AWS Bedrock Console"
    log_info "  2. Monitor CloudWatch Logs for container startup"
    log_info "  3. Test the Runtime HTTP endpoint"
    log_info "  4. Verify Memory and Tools are accessible"
}

main "$@"
