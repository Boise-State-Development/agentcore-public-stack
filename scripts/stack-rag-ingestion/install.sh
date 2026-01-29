#!/bin/bash
set -euo pipefail

# Script: Install Dependencies for RAG Ingestion Stack
# Description: Installs Node.js dependencies for CDK synthesis and deployment
# Note: Python dependencies are installed inside the Docker container, not in CI

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
    log_info "Installing RAG Ingestion Stack dependencies..."
    
    # ===========================================================
    # Install CDK Dependencies
    # ===========================================================
    
    log_info "Installing CDK dependencies..."
    cd "${PROJECT_ROOT}/infrastructure"
    
    # Check if package.json exists
    if [ ! -f "package.json" ]; then
        log_error "package.json not found in ${PROJECT_ROOT}/infrastructure"
        exit 1
    fi
    
    # Check if Node.js is installed
    if ! command -v node &> /dev/null; then
        log_error "Node.js is not installed. Please install Node.js 18 or higher."
        exit 1
    fi
    
    # Display Node.js version
    NODE_VERSION=$(node --version)
    log_info "Using Node.js ${NODE_VERSION}"
    
    # Install Node.js dependencies
    if [ -d "node_modules" ]; then
        log_info "node_modules already exists, skipping npm install"
    else
        log_info "Installing Node.js dependencies from package.json..."
        npm install
    fi
    
    # Verify CDK installation
    log_info "Verifying CDK installation..."
    if npm list aws-cdk-lib &> /dev/null; then
        log_success "aws-cdk-lib installed successfully"
    else
        log_error "aws-cdk-lib installation verification failed"
        exit 1
    fi
    
    log_success "CDK dependencies installed successfully!"
    log_success "All RAG Ingestion Stack dependencies installed successfully!"
}

main "$@"
