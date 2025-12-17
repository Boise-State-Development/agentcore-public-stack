#!/bin/bash
set -euo pipefail

# Script: Build Docker Image for Inference API
# Description: Builds Docker image for the Inference API service

# Get the directory of this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

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
    log_info "Building Inference API Docker image..."
    
    # Set image name and tag
    IMAGE_NAME="${CDK_PROJECT_PREFIX:-agentcore}-inference-api"
    IMAGE_TAG="${IMAGE_TAG:-latest}"
    FULL_IMAGE_NAME="${IMAGE_NAME}:${IMAGE_TAG}"
    
    log_info "Image name: ${FULL_IMAGE_NAME}"
    
    # Change to project root for Docker build context
    cd "${PROJECT_ROOT}"
    
    # Check if Dockerfile exists
    DOCKERFILE="${PROJECT_ROOT}/backend/Dockerfile.inference-api"
    if [ ! -f "${DOCKERFILE}" ]; then
        log_error "Dockerfile not found: ${DOCKERFILE}"
        exit 1
    fi
    
    # Check if Docker is installed
    if ! command -v docker &> /dev/null; then
        log_error "Docker is not installed. Please install Docker."
        exit 1
    fi
    
    # Build Docker image
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
    
    log_success "Build completed successfully!"
    log_info "To run the image locally:"
    log_info "  docker run -p 8080:8080 ${FULL_IMAGE_NAME}"
}

main "$@"
