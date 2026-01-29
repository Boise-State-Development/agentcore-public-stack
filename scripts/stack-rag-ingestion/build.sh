#!/bin/bash
set -euo pipefail

# Script: Build Docker Image for RAG Ingestion Lambda
# Description: Builds Docker image for the RAG ingestion Lambda function

# Get the directory of this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Source common environment loader
# shellcheck source=../common/load-env.sh
source "${SCRIPT_DIR}/../common/load-env.sh"

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
    log_info "Building RAG Ingestion Lambda Docker image..."
    
    # Set image name and tag
    IMAGE_NAME="${CDK_PROJECT_PREFIX}-rag-ingestion"
    IMAGE_TAG="${IMAGE_TAG:-latest}"
    FULL_IMAGE_NAME="${IMAGE_NAME}:${IMAGE_TAG}"
    
    log_info "Image name: ${FULL_IMAGE_NAME}"
    log_info "Platform: linux/arm64 (Lambda ARM64)"
    
    # Change to project root for Docker build context
    cd "${PROJECT_ROOT}"
    
    # Check if Dockerfile exists
    DOCKERFILE="${PROJECT_ROOT}/backend/Dockerfile.rag-ingestion"
    if [ ! -f "${DOCKERFILE}" ]; then
        log_error "Dockerfile not found: ${DOCKERFILE}"
        exit 1
    fi
    
    # Check if Docker is installed
    if ! command -v docker &> /dev/null; then
        log_error "Docker is not installed. Please install Docker."
        exit 1
    fi
    
    # Build Docker image for ARM64 platform (Lambda requirement)
    log_info "Building Docker image for ARM64 platform (this may take several minutes)..."
    if docker build \
        --platform linux/arm64 \
        -f "${DOCKERFILE}" \
        -t "${FULL_IMAGE_NAME}" \
        --build-arg BUILD_DATE="$(date -u +'%Y-%m-%dT%H:%M:%SZ')" \
        --build-arg VCS_REF="$(git rev-parse --short HEAD 2>/dev/null || echo 'unknown')" \
        .; then
        log_success "Docker image built successfully: ${FULL_IMAGE_NAME}"
    else
        log_error "Docker build failed"
        exit 1
    fi
    
    # Display image size
    IMAGE_SIZE=$(docker images "${IMAGE_NAME}" --format "{{.Size}}" | head -n 1)
    log_info "Image size: ${IMAGE_SIZE}"
    
    # Tag image with commit hash if in git repository
    if git rev-parse --git-dir > /dev/null 2>&1; then
        COMMIT_HASH=$(git rev-parse --short HEAD)
        COMMIT_TAG="${IMAGE_NAME}:${COMMIT_HASH}"
        log_info "Tagging image with commit hash: ${COMMIT_TAG}"
        docker tag "${FULL_IMAGE_NAME}" "${COMMIT_TAG}"
    fi
    
    # Validate build success
    log_info "Validating Docker image..."
    if docker images "${IMAGE_NAME}" | grep -q "${IMAGE_TAG}"; then
        log_success "Docker image validation passed"
    else
        log_error "Docker image validation failed - image not found in local registry"
        exit 1
    fi
    
    log_success "Build completed successfully!"
    log_info "Image is ready for Lambda deployment"
    log_info "To test the image locally (Lambda Runtime Interface Emulator required):"
    log_info "  docker run --platform linux/arm64 -p 9000:8080 ${FULL_IMAGE_NAME}"
}

main "$@"
