#!/bin/bash
set -euo pipefail

# Script: Install Dependencies for MCP Sandbox Stack
# Description: Installs Node.js dependencies for CDK synthesis and deployment.
# Note: the static proxy shell (assets/mcp-sandbox/) is plain HTML/JS bundled
# by CDK BucketDeployment — no Docker or pip step required.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

log_info() { echo "[INFO] $1"; }
log_error() { echo "[ERROR] $1" >&2; }
log_success() { echo "[SUCCESS] $1"; }

main() {
    log_info "Installing MCP Sandbox Stack dependencies..."

    cd "${PROJECT_ROOT}/infrastructure"

    if [ ! -f "package.json" ]; then
        log_error "package.json not found in ${PROJECT_ROOT}/infrastructure"
        exit 1
    fi

    if ! command -v node &> /dev/null; then
        log_error "Node.js is not installed. Please install Node.js 18 or higher."
        exit 1
    fi

    NODE_VERSION=$(node --version)
    log_info "Using Node.js ${NODE_VERSION}"

    if [ -f "package-lock.json" ]; then
        log_info "Running npm ci (clean install from package-lock.json)..."
        npm ci
    else
        log_error "package-lock.json not found. Cannot run npm ci."
        exit 1
    fi

    if npm list aws-cdk-lib &> /dev/null; then
        log_success "aws-cdk-lib installed successfully"
    else
        log_error "aws-cdk-lib installation verification failed"
        exit 1
    fi

    log_success "All MCP Sandbox Stack dependencies installed successfully!"
}

main "$@"
