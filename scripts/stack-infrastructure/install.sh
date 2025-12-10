#!/bin/bash

#============================================================
# Infrastructure Stack - Install Dependencies
# 
# Installs CDK and project dependencies required for deployment.
#============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Source common utilities
source "${PROJECT_ROOT}/scripts/common/load-env.sh"

# ===========================================================
# Install CDK Dependencies
# ===========================================================

log_info "Installing CDK dependencies..."
cd "${PROJECT_ROOT}/infrastructure"

if [ -d "node_modules" ]; then
    log_info "node_modules already exists, skipping npm install"
else
    npm install
fi

log_success "CDK dependencies installed successfully"

# ===========================================================
# Install AWS CDK CLI (if not already installed)
# ===========================================================

if ! command -v cdk &> /dev/null; then
    log_info "AWS CDK CLI not found, installing..."
    npm install -g aws-cdk
    log_success "AWS CDK CLI installed successfully"
else
    log_info "AWS CDK CLI already installed: $(cdk --version)"
fi

log_success "Infrastructure stack dependencies installed"
