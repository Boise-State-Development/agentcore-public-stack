#!/bin/bash
set -euo pipefail

# Script: Test Docker Image for Inference API
# Description: Starts Docker container and validates health endpoint

# Get the directory of this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Set CDK_PROJECT_PREFIX from environment or use default
# This script doesn't need full configuration validation, just the project prefix
CDK_PROJECT_PREFIX="${CDK_PROJECT_PREFIX:-agentcore}"

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
    log_info "Testing ARM64 Docker image..."
    
    # Check if running on ARM64 or need QEMU emulation
    ARCH=$(uname -m)
    if [ "${ARCH}" != "aarch64" ] && [ "${ARCH}" != "arm64" ]; then
        log_info "Running on x86_64, testing with QEMU emulation (this may be slower)"
        
        # Check if QEMU is available for ARM64 emulation
        if ! docker run --rm --privileged multiarch/qemu-user-static --reset -p yes > /dev/null 2>&1; then
            log_error "QEMU emulation setup failed. ARM64 testing may not work."
            log_info "Continuing anyway..."
        fi
    else
        log_info "Running on ARM64 platform natively"
    fi
    
    # Use IMAGE_TAG from environment (set by build job), fallback to latest for local testing
    IMAGE_TAG="${IMAGE_TAG:-latest}"
    IMAGE_NAME="${CDK_PROJECT_PREFIX}-inference-api:${IMAGE_TAG}"
    
    log_info "Testing Docker image: ${IMAGE_NAME}"
    
    # Start container in background on port 8080 (AgentCore Runtime standard)
    CONTAINER_ID=$(docker run -d --platform linux/arm64 -p 8080:8080 "${IMAGE_NAME}")
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
        
        # Try health check on /ping endpoint (AgentCore Runtime standard)
        if curl -f http://localhost:8080/ping 2>/dev/null; then
            log_success "Container is healthy (ping endpoint responded)"
            
            # Also test /health endpoint if it exists
            if curl -f http://localhost:8080/health 2>/dev/null; then
                log_success "Health endpoint also responding"
            fi
            
            docker stop "${CONTAINER_ID}" > /dev/null
            log_success "ARM64 Docker image test passed"
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
