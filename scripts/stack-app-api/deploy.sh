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

# Function to push Docker image to ECR
push_to_ecr() {
    local ecr_uri=$1
    local image_name=$2
    local image_tag=$3
    
    log_info "Pushing Docker image to ECR: ${ecr_uri}"
    
    # Extract region and account from ECR URI
    local ecr_region=$(echo "${ecr_uri}" | cut -d'.' -f4)
    local ecr_account=$(echo "${ecr_uri}" | cut -d'.' -f1 | cut -d'/' -f1)
    
    # Login to ECR
    log_info "Logging in to ECR..."
    aws ecr get-login-password --region "${ecr_region}" | \
        docker login --username AWS --password-stdin "${ecr_account}.dkr.ecr.${ecr_region}.amazonaws.com"
    
    # Tag image for ECR
    local local_image="${image_name}:${image_tag}"
    local remote_image="${ecr_uri}:${image_tag}"
    
    log_info "Tagging image: ${local_image} -> ${remote_image}"
    docker tag "${local_image}" "${remote_image}"
    
    # Push image
    log_info "Pushing image to ECR (this may take several minutes)..."
    docker push "${remote_image}"
    
    # Also push with 'latest' tag
    local remote_latest="${ecr_uri}:latest"
    log_info "Tagging and pushing with 'latest' tag..."
    docker tag "${local_image}" "${remote_latest}"
    docker push "${remote_latest}"
    
    log_success "Docker image pushed successfully to ECR"
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
    
    # Bootstrap CDK if needed (idempotent operation)
    log_info "Ensuring CDK is bootstrapped..."
    npx cdk bootstrap "aws://${CDK_AWS_ACCOUNT}/${CDK_AWS_REGION}" \
        --context projectPrefix="${CDK_PROJECT_PREFIX}" \
        --context awsAccount="${CDK_AWS_ACCOUNT}" \
        --context awsRegion="${CDK_AWS_REGION}"
    
    # Deploy CDK stack
    log_info "Deploying AppApiStack with CDK..."
    npx cdk deploy AppApiStack \
        --require-approval never \
        --context projectPrefix="${CDK_PROJECT_PREFIX}" \
        --context awsAccount="${CDK_AWS_ACCOUNT}" \
        --context awsRegion="${CDK_AWS_REGION}" \
        --outputs-file "${PROJECT_ROOT}/cdk-outputs-app-api.json"
    
    log_success "CDK deployment completed successfully"
    
    # Get ECR repository URI from SSM Parameter Store
    log_info "Retrieving ECR repository URI from SSM..."
    set +e
    ECR_URI=$(aws ssm get-parameter \
        --name "/${CDK_PROJECT_PREFIX}/app-api/ecr-repository-uri" \
        --region "${CDK_AWS_REGION}" \
        --query 'Parameter.Value' \
        --output text 2>&1)
    local exit_code=$?
    set -e
    
    if [ ${exit_code} -ne 0 ]; then
        log_error "Failed to retrieve ECR repository URI from SSM"
        log_error "Detailed error: ${ECR_URI}"
        log_error "Possible causes:"
        log_error "  1. CDK deployment didn't complete successfully"
        log_error "  2. SSM parameter wasn't created"
        log_error "  3. Insufficient AWS permissions"
        exit 1
    fi
    
    log_info "ECR Repository URI: ${ECR_URI}"
    
    # Build Docker image
    log_info "Building Docker image..."
    IMAGE_NAME="${CDK_PROJECT_PREFIX}-app-api"
    IMAGE_TAG="${DOCKER_IMAGE_TAG:-latest}"
    
    cd "${PROJECT_ROOT}"
    "${SCRIPT_DIR}/build.sh"
    
    # Push Docker image to ECR
    push_to_ecr "${ECR_URI}" "${IMAGE_NAME}" "${IMAGE_TAG}"
    
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
