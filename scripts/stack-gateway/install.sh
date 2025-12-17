#!/bin/bash
set -euo pipefail

# Install Dependencies for Gateway Stack
# Installs CDK and Python dependencies needed for Gateway deployment

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
INFRASTRUCTURE_DIR="${PROJECT_ROOT}/infrastructure"

# Source common environment loader
source "${PROJECT_ROOT}/scripts/common/load-env.sh"

log_success() {
    echo -e "\033[0;32m✓ $1\033[0m"
}

log_info "Installing Gateway Stack dependencies..."

# ============================================================
# Install CDK Dependencies
# ============================================================

log_info "Installing CDK dependencies..."
cd "${INFRASTRUCTURE_DIR}"

if [ ! -f "package.json" ]; then
    log_error "package.json not found in ${INFRASTRUCTURE_DIR}"
    exit 1
fi

npm ci --silent || {
    log_error "Failed to install CDK dependencies"
    exit 1
}

log_success "CDK dependencies installed"

# ============================================================
# Install Python Dependencies for Lambda
# ============================================================

log_info "Checking Python dependencies for Lambda..."

# Note: Lambda dependencies are packaged by CDK automatically from requirements.txt
# No manual installation needed for deployment
# For local testing, developers can manually install from backend/lambda-functions/google-search/requirements.txt

log_success "Gateway Stack dependencies installation complete"
