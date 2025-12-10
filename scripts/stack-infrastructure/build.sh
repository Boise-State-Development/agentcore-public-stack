#!/bin/bash

#============================================================
# Infrastructure Stack - Build
# 
# Compiles TypeScript CDK code.
#============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Source common utilities
source "${PROJECT_ROOT}/scripts/common/load-env.sh"

# ===========================================================
# Build CDK TypeScript Code
# ===========================================================

log_info "Building Infrastructure Stack CDK code..."
cd "${PROJECT_ROOT}/infrastructure"

# Compile TypeScript
npm run build

log_success "Infrastructure Stack CDK code built successfully"
