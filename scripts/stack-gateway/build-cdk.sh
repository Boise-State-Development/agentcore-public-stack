#!/bin/bash
set -euo pipefail

# Build CDK Code for Gateway Stack
# Compiles TypeScript CDK code to JavaScript

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
INFRASTRUCTURE_DIR="${PROJECT_ROOT}/infrastructure"

# Source common environment loader
source "${PROJECT_ROOT}/scripts/common/load-env.sh"

log_info "Building Gateway Stack CDK code..."

# ============================================================
# Build TypeScript CDK Code
# ============================================================

cd "${INFRASTRUCTURE_DIR}"

if [ ! -f "tsconfig.json" ]; then
    log_error "tsconfig.json not found in ${INFRASTRUCTURE_DIR}"
    exit 1
fi

# Compile TypeScript
npx tsc || {
    log_error "TypeScript compilation failed"
    exit 1
}

log_success "Gateway Stack CDK code built successfully"
