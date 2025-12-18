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
    
    # Check if pytest is installed
    if ! python3 -m pytest --version &> /dev/null; then
        log_info "pytest not found, installing..."
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
        
        # Debug: Check what Python can see
        log_info "Debugging Python import paths..."
        PYTHONPATH="${BACKEND_DIR}/src:${PYTHONPATH:-}" python3 -c "
import sys
print('Python sys.path:')
for p in sys.path:
    print(f'  {p}')
print()
print('Checking if agents module exists:')
import os
agents_path = '${BACKEND_DIR}/src/agents'
print(f'  Path exists: {os.path.exists(agents_path)}')
if os.path.exists(agents_path):
    print(f'  Contents: {os.listdir(agents_path)}')
    quota_path = '${BACKEND_DIR}/src/agents/strands_agent/quota'
    if os.path.exists(quota_path):
        print(f'  Quota contents: {os.listdir(quota_path)}')
print()
print('Attempting import:')
try:
    from agents.strands_agent.quota.checker import QuotaChecker
    print('  SUCCESS: Import worked!')
except Exception as e:
    print(f'  FAILED: {e}')
    import traceback
    traceback.print_exc()
"
        
        # Run pytest with explicit PYTHONPATH
        # Must set it inline with the command to ensure it's available during collection
        log_info "PYTHONPATH: ${BACKEND_DIR}/src"
        PYTHONPATH="${BACKEND_DIR}/src:${PYTHONPATH:-}" python3 -m pytest tests/ \
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
