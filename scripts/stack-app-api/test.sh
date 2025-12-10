#!/bin/bash
set -euo pipefail

# Script: Run Tests for App API
# Description: Runs Python tests for the App API service

# Get the directory of this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BACKEND_DIR="${PROJECT_ROOT}/backend"

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
        log_info "No tests/ directory found. Skipping tests."
    fi
    
    log_success "App API tests completed successfully!"
}

main "$@"
