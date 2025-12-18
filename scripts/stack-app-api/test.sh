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
    log_info "Working directory: $(pwd)"
    
    # Install project in editable mode (dependencies should be cached)
    log_info "Installing project in editable mode..."
    python3 -m pip install -e ".[agentcore,dev]" --quiet
    
    # Check if pytest is installed (should be from cache or install step)
    if ! python3 -m pytest --version &> /dev/null; then
        log_info "pytest not found, installing test dependencies..."
        python3 -m pip install pytest pytest-asyncio pytest-cov
    fi
    
    # Run tests
    log_info "Executing tests..."
    
    # Run pytest with coverage if tests directory exists
    if [ -d "tests" ]; then
        log_info "Running tests from tests/ directory..."
        
        # Verify src directory exists
        if [ ! -d "${BACKEND_DIR}/src" ]; then
            log_error "src directory not found at ${BACKEND_DIR}/src"
            exit 1
        fi
        
        # Verify conftest.py exists
        if [ ! -f "${BACKEND_DIR}/tests/conftest.py" ]; then
            log_error "conftest.py not found at ${BACKEND_DIR}/tests/conftest.py"
            exit 1
        fi
        
        # Verify the quota modules exist
        if [ ! -f "${BACKEND_DIR}/src/agents/strands_agent/quota/checker.py" ]; then
            log_error "Quota checker module not found"
            exit 1
        fi
        
        # Export PYTHONPATH to ensure src is on the path
        export PYTHONPATH="${BACKEND_DIR}/src:${PYTHONPATH:-}"
        
        # Run pytest
        log_info "Running pytest with PYTHONPATH=${PYTHONPATH}"
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
