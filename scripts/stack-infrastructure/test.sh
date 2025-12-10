#!/bin/bash

#============================================================
# Infrastructure Stack - Test
# 
# Runs tests for the Infrastructure Stack CDK code.
#============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Source common utilities
source "${PROJECT_ROOT}/scripts/common/load-env.sh"

# ===========================================================
# Run CDK Tests
# ===========================================================

log_info "Running Infrastructure Stack tests..."
cd "${PROJECT_ROOT}/infrastructure"

# Check if test directory exists
if [ ! -d "test" ] || [ -z "$(ls -A test/*.test.* 2>/dev/null)" ]; then
    log_info "No tests found in test/ directory, skipping tests"
    exit 0
fi

# Run tests
npm test

log_success "Infrastructure Stack tests passed"
