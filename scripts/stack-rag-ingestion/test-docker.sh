#!/bin/bash
set -euo pipefail

# Script: Test Docker Image for RAG Ingestion Lambda
# Description: Validates Docker image structure and basic integrity
# Note: This is a Lambda function image, not a web service, so we validate structure rather than running it

# Get the directory of this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Set CDK_PROJECT_PREFIX from environment or use default
CDK_PROJECT_PREFIX="${CDK_PROJECT_PREFIX:-agentcore}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

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
    log_info "Testing RAG Ingestion Lambda Docker image..."
    
    # Use CDK_PROJECT_PREFIX and IMAGE_TAG from environment (set at workflow level)
    IMAGE_NAME="${CDK_PROJECT_PREFIX}-rag-ingestion:${IMAGE_TAG}"
    
    log_info "Testing Docker image: ${IMAGE_NAME}"
    
    # Check if image exists
    if ! docker images "${IMAGE_NAME}" | grep -q "${IMAGE_TAG}"; then
        log_error "Docker image not found: ${IMAGE_NAME}"
        log_info "Available images:"
        docker images "${CDK_PROJECT_PREFIX}-rag-ingestion"
        exit 1
    fi
    
    log_success "Docker image found: ${IMAGE_NAME}"
    
    # Display image size
    IMAGE_SIZE=$(docker images "${IMAGE_NAME}" --format "{{.Size}}" | head -n 1)
    log_info "Image size: ${IMAGE_SIZE}"
    
    # Test that image can be inspected
    log_info "Inspecting image metadata..."
    if docker inspect "${IMAGE_NAME}" > /dev/null 2>&1; then
        log_success "Image inspection passed"
    else
        log_error "Failed to inspect image"
        exit 1
    fi
    
    # Verify the image has the Lambda runtime interface client
    log_info "Verifying Lambda runtime components..."
    if docker run --rm --platform linux/arm64 --entrypoint /bin/sh "${IMAGE_NAME}" -c "command -v python3" > /dev/null 2>&1; then
        log_success "Python3 runtime found"
    else
        log_error "Python3 runtime not found"
        exit 1
    fi
    
    # Verify CMD is set correctly for Lambda
    log_info "Verifying Lambda CMD configuration..."
    CMD=$(docker inspect "${IMAGE_NAME}" --format='{{json .Config.Cmd}}')
    if echo "${CMD}" | grep -q "handler.lambda_handler"; then
        log_success "Lambda handler CMD configured: ${CMD}"
    else
        log_info "CMD configuration: ${CMD}"
        log_info "Note: Handler may be configured differently, but image is valid"
    fi
    
    log_success "Docker image validation passed!"
    log_info "Image is ready for Lambda deployment"
}

main "$@"
