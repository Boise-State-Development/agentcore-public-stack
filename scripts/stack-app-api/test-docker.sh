#!/bin/bash
set -euo pipefail

# Script: Test Docker Image for App API
# Description: Starts Docker container and validates health endpoint

# Get the directory of this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Set CDK_PROJECT_PREFIX from environment or use default
# This script doesn't need full configuration validation, just the project prefix
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
    log_info "Testing Docker image..."
    
    # Use CDK_PROJECT_PREFIX and IMAGE_TAG from environment (set at workflow level)
    IMAGE_NAME="${CDK_PROJECT_PREFIX}-app-api:${IMAGE_TAG}"
    
    log_info "Testing Docker image: ${IMAGE_NAME}"
    
    # Start container in background
    CONTAINER_ID=$(docker run -d -p 8000:8000 "${IMAGE_NAME}")
    log_info "Container ID: ${CONTAINER_ID}"
    
    # Wait for container to be healthy
    log_info "Waiting for container to be healthy..."
    
    # Give container a moment to start up before checking
    sleep 5
    
    for i in {1..30}; do
        # Check if container is still running using docker inspect (more reliable than ps | grep)
        CONTAINER_STATE=$(docker inspect -f '{{.State.Running}}' "${CONTAINER_ID}" 2>/dev/null || echo "false")
        
        if [ "${CONTAINER_STATE}" != "true" ]; then
            log_error "Container exited unexpectedly"
            docker logs "${CONTAINER_ID}"
            exit 1
        fi
        
        # Try health check
        if curl -f http://localhost:8000/health 2>/dev/null; then
            log_success "Container is healthy"
            docker stop "${CONTAINER_ID}" > /dev/null
            log_success "Docker image test passed"
            exit 0
        fi
        
        log_info "Attempt $i/30: Container running, waiting for health check..."
        sleep 2
    done
    
    log_error "Container health check timed out"
    docker logs "${CONTAINER_ID}"
    docker stop "${CONTAINER_ID}" > /dev/null 2>&1 || true
    exit 1
}

main "$@"
