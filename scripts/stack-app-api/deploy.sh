#!/bin/bash
set -euo pipefail

# Script: Deploy App API Infrastructure
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

# Function to update ECS service
update_ecs_service() {
    local cluster_name=$1
    local service_name=$2
    
    log_info "Forcing new deployment of ECS service: ${service_name}"
    
    set +e
    aws ecs update-service \
        --cluster "${cluster_name}" \
        --service "${service_name}" \
        --force-new-deployment \
        --region "${CDK_AWS_REGION}" \
        > /dev/null 2>&1
    local exit_code=$?
    set -e
    
    if [ ${exit_code} -eq 0 ]; then
        log_success "ECS service update initiated"
    else
        log_info "Note: ECS service update may not be needed (service might not exist yet)"
    fi
}

main() {
    log_info "Deploying App API Stack..."
    
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
    # Note: Run from project root to avoid loading CDK app context (see CLAUDES_LESSONS_PHASE4.md Challenge 1)
    log_info "Ensuring CDK is bootstrapped..."
    cd "${REPO_ROOT}"
    npx cdk bootstrap "aws://${CDK_AWS_ACCOUNT}/${CDK_AWS_REGION}" \
        || log_info "CDK already bootstrapped or bootstrap failed (continuing anyway)"
    cd infrastructure/
    
    # Check if pre-synthesized template exists
    if [ -d "cdk.out" ] && [ -f "cdk.out/AppApiStack.template.json" ]; then
        log_info "Using pre-synthesized CloudFormation template from cdk.out/"
        CDK_APP="cdk.out/"
    else
        log_info "No pre-synthesized template found. CDK will synthesize during deployment."
        CDK_APP=""
    fi
    
    # Deploy CDK stack
    log_info "Deploying AppApiStack with CDK..."
    
    # Use CDK_REQUIRE_APPROVAL env var with fallback to never
    REQUIRE_APPROVAL="${CDK_REQUIRE_APPROVAL:-never}"
    
    if [ -n "${CDK_APP}" ]; then
        # Deploy using pre-synthesized template
        npx cdk deploy AppApiStack \
            --app "${CDK_APP}" \
            --require-approval ${REQUIRE_APPROVAL} \
            --outputs-file "${PROJECT_ROOT}/cdk-outputs-app-api.json"
    else
        # Deploy with context parameters (will synthesize first)
        # Build context parameters using shared helper function
        CONTEXT_PARAMS=$(build_cdk_context_params)
        
        # Execute CDK deploy with context parameters
        eval "npx cdk deploy AppApiStack --require-approval ${REQUIRE_APPROVAL} ${CONTEXT_PARAMS} --outputs-file \"${PROJECT_ROOT}/cdk-outputs-app-api.json\""
    fi
    
    log_success "CDK deployment completed successfully"
    
    # Construct ECR repository URI (no longer stored in SSM)
    REPO_NAME="${CDK_PROJECT_PREFIX}-app-api"
    ECR_URI="${CDK_AWS_ACCOUNT}.dkr.ecr.${CDK_AWS_REGION}.amazonaws.com/${REPO_NAME}"
    
    log_info "ECR Repository URI: ${ECR_URI}"
    
    # Validate that IMAGE_TAG is set (should be passed from build job)
    if [ -z "${IMAGE_TAG:-}" ]; then
        log_error "IMAGE_TAG is not set. This should be the version tag from the build step."
        exit 1
    fi
    
    log_info "Using pre-built image with version tag: ${IMAGE_TAG}"
    log_info "Image URI: ${ECR_URI}:${IMAGE_TAG}"
    
    # Get ECS cluster and service names from outputs
    if [ -f "${PROJECT_ROOT}/cdk-outputs-app-api.json" ]; then
        CLUSTER_NAME=$(jq -r ".AppApiStack.EcsClusterName // empty" "${PROJECT_ROOT}/cdk-outputs-app-api.json")
        SERVICE_NAME=$(jq -r ".AppApiStack.EcsServiceName // empty" "${PROJECT_ROOT}/cdk-outputs-app-api.json")
        
        if [ -n "${CLUSTER_NAME}" ] && [ -n "${SERVICE_NAME}" ]; then
            update_ecs_service "${CLUSTER_NAME}" "${SERVICE_NAME}"
        fi
    fi
    
    log_success "App API deployment completed successfully!"
    log_info ""
    log_info "Next steps:"
    log_info "  1. Check ECS service status in AWS Console"
    log_info "  2. Monitor CloudWatch Logs for container startup"
    log_info "  3. Verify ALB health checks are passing"
    log_info "  4. Test the API endpoint"
}

main "$@"
