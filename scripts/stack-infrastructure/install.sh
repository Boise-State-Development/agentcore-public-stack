#!/bin/bash

#============================================================
# Infrastructure Stack - Install Dependencies
# 
# Installs CDK and project dependencies required for deployment.
#============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Simple logging functions (don't load full env yet)
log_info() {
    echo "[INFO] $1"
}

log_error() {
    echo "[ERROR] $1" >&2
}

log_success() {
    echo "[SUCCESS] $1"
}

# ===========================================================
# Install CDK Dependencies
# ===========================================================

log_info "Installing CDK dependencies..."
cd "${PROJECT_ROOT}/infrastructure"

if [ -f "package-lock.json" ]; then
    log_info "Running npm ci (clean install from package-lock.json)..."
    npm ci
else
    log_error "package-lock.json not found. Cannot run npm ci."
    exit 1
fi

log_success "CDK dependencies installed successfully"

# ===========================================================
# Install AWS CDK CLI (if not already installed)
# ===========================================================

if ! command -v cdk &> /dev/null; then
    log_info "AWS CDK CLI not found, installing..."
    npm install -g aws-cdk@2.1113.0
    log_success "AWS CDK CLI installed successfully"
else
    log_info "AWS CDK CLI already installed: $(cdk --version)"
fi

log_success "Infrastructure stack dependencies installed"
