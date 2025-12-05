#!/bin/bash
set -euo pipefail

# Script: Run Tests for App API
# Description: Runs Python tests for the App API service

# Get the directory of this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BACKEND_DIR="${PROJECT_ROOT}/backend"

# Source common utilities
source "${PROJECT_ROOT}/scripts/common/load-env.sh"

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
    log_info "Running App API tests..."
    
    # Change to backend directory
    cd "${BACKEND_DIR}"
    
    # Check if pytest is installed
    if ! python3 -m pytest --version &> /dev/null; then
        log_info "pytest not found, installing..."
        python3 -m pip install pytest pytest-asyncio pytest-cov
    fi
    
    # Run tests
    log_info "Executing tests..."
    
    # Set PYTHONPATH to include src directory
    export PYTHONPATH="${BACKEND_DIR}/src:${PYTHONPATH:-}"
    
    # Run pytest with coverage if tests directory exists
    if [ -d "tests" ]; then
        log_info "Running tests from tests/ directory..."
        python3 -m pytest tests/ \
            -v \
            --tb=short \
            --color=yes \
            --disable-warnings
    else
        log_info "No tests/ directory found. Running basic import check..."
        
        # Basic import check for App API
        if python3 -c "
import sys
sys.path.insert(0, '${BACKEND_DIR}/src')
from apis.app_api.main import app
print('✓ App API imports successfully')
" 2>&1; then
            log_success "App API imports successfully"
        else
            log_error "App API import check failed"
            exit 1
        fi
        
        # Check health endpoint module
        if python3 -c "
import sys
sys.path.insert(0, '${BACKEND_DIR}/src')
from apis.app_api.health import router
print('✓ Health endpoint module imports successfully')
" 2>&1; then
            log_success "Health endpoint module imports successfully"
        else
            log_error "Health endpoint import check failed"
            exit 1
        fi
        
        log_info "Note: No test files found. Consider adding tests in backend/tests/"
    fi
    
    log_success "App API tests completed successfully!"
}

main "$@"
