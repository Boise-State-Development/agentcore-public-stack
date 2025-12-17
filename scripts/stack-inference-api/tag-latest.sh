#!/bin/bash
set -euo pipefail

# Script: Tag ECR Image as Latest
# Description: Tags a specific version in ECR with the 'latest' tag after successful deployment

# Get the directory of this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

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

main() {
    log_info "Tagging ECR image as latest..."
    
    # Validate required environment variables
    if [ -z "${CDK_AWS_REGION:-}" ]; then
        log_error "CDK_AWS_REGION is not set"
        exit 1
    fi
    
    if [ -z "${IMAGE_TAG:-}" ]; then
        log_error "IMAGE_TAG is not set. This should be the version tag to promote to latest."
        exit 1
    fi
    
    if [ -z "${CDK_AWS_ACCOUNT:-}" ]; then
        log_error "CDK_AWS_ACCOUNT is not set"
        exit 1
    fi
    
    # Construct ECR repository URI (no longer stored in SSM)
    REPO_NAME="${CDK_PROJECT_PREFIX}-inference-api"
    ECR_URI="${CDK_AWS_ACCOUNT}.dkr.ecr.${CDK_AWS_REGION}.amazonaws.com/${REPO_NAME}"
    
    log_info "ECR Repository URI: ${ECR_URI}"
    log_info "Version tag to promote: ${IMAGE_TAG}"
    
    # Extract region and account from ECR URI
    local ecr_region=$(echo "${ECR_URI}" | cut -d'.' -f4)
    local ecr_account=$(echo "${ECR_URI}" | cut -d'.' -f1 | cut -d'/' -f1)
    local repo_name=$(echo "${ECR_URI}" | cut -d'/' -f2)
    
    # Get the manifest for the versioned image
    log_info "Retrieving image manifest for version ${IMAGE_TAG}..."
    MANIFEST=$(aws ecr batch-get-image \
        --repository-name "${repo_name}" \
        --image-ids imageTag="${IMAGE_TAG}" \
        --region "${ecr_region}" \
        --query 'images[0].imageManifest' \
        --output text)
    
    if [ -z "${MANIFEST}" ] || [ "${MANIFEST}" == "None" ]; then
        log_error "Failed to retrieve manifest for image tag: ${IMAGE_TAG}"
        exit 1
    fi
    
    # Tag the image as latest
    log_info "Tagging image ${IMAGE_TAG} as 'latest'..."
    aws ecr put-image \
        --repository-name "${repo_name}" \
        --image-tag "latest" \
        --image-manifest "${MANIFEST}" \
        --region "${ecr_region}" \
        > /dev/null
    
    # Also tag with 'deployed-' prefix for lifecycle policy protection
    log_info "Tagging image ${IMAGE_TAG} as 'deployed-${IMAGE_TAG}' for retention..."
    aws ecr put-image \
        --repository-name "${repo_name}" \
        --image-tag "deployed-${IMAGE_TAG}" \
        --image-manifest "${MANIFEST}" \
        --region "${ecr_region}" \
        > /dev/null
    
    log_success "Successfully tagged ${ECR_URI}:${IMAGE_TAG} as 'latest' and 'deployed-${IMAGE_TAG}'"
    log_info "Image is now available at:"
    log_info "  - ${ECR_URI}:latest"
    log_info "  - ${ECR_URI}:deployed-${IMAGE_TAG} (protected from cleanup)"
}

main "$@"
