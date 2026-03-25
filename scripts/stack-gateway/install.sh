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

if [ ! -f "package-lock.json" ]; then
    log_error "package-lock.json not found. Cannot run npm ci."
    exit 1
fi

npm ci --silent || {
    log_error "Failed to install CDK dependencies"
    exit 1
}

log_success "CDK dependencies installed"

log_success "Gateway Stack dependencies installation complete"
