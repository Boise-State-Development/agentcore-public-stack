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
    
    # Upgrade pip
    log_info "Upgrading pip..."
    python3 -m pip install --upgrade pip --quiet
    
    # Install ALL dependencies fresh
    log_info "Installing all dependencies (fresh install for debugging)..."
    python3 -m pip install -e ".[agentcore,dev]"
    
    # Verify installation
    log_info "Verifying installation..."
    python3 -c "import fastapi; import uvicorn; print('Core dependencies installed')"
    
    # Run tests
    log_info "Executing tests..."
    
    if [ ! -d "tests" ]; then
        log_info "No tests/ directory found. Skipping tests."
        log_success "App API tests completed successfully!"
        return 0
    fi
    
    # Set PYTHONPATH explicitly
    export PYTHONPATH="${BACKEND_DIR}/src:${PYTHONPATH:-}"
    log_info "PYTHONPATH=${PYTHONPATH}"
    
    # Set dummy AWS credentials for tests
    export AWS_DEFAULT_REGION=us-east-1
    export AWS_ACCESS_KEY_ID=testing
    export AWS_SECRET_ACCESS_KEY=testing
    
    # Test import directly
    log_info "Testing direct import..."
    python3 -c "from agents.strands_agent.quota.checker import QuotaChecker; print('Direct import works')"
    
    # Run pytest with import-mode=importlib
    log_info "Running pytest..."
    python3 -m pytest tests/ \
        -v \
        --tb=short \
        --color=yes \
        --disable-warnings
    
    log_success "App API tests completed successfully!"
}

main "$@"
