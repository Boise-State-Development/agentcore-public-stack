#!/bin/bash
set -euo pipefail

# Script: Install Dependencies for Inference API
# Description: Installs Python dependencies for the Inference API service using uv

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
    log_info "Installing Inference API dependencies..."
    
    # Check if backend directory exists
    if [ ! -d "${BACKEND_DIR}" ]; then
        log_error "Backend directory not found: ${BACKEND_DIR}"
        exit 1
    fi
    
    # Change to backend directory
    cd "${BACKEND_DIR}"
    
    # Check if pyproject.toml exists
    if [ ! -f "pyproject.toml" ]; then
        log_error "pyproject.toml not found in ${BACKEND_DIR}"
        exit 1
    fi
    
    # Install uv if not present
    if ! command -v uv &> /dev/null; then
        log_info "Installing uv..."
        curl -LsSf https://astral.sh/uv/0.7.12/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
    fi
    
    # Display uv version
    UV_VERSION=$(uv --version)
    log_info "Using ${UV_VERSION}"
    
    # Install dependencies from lock file
    # --frozen: replay the lock file exactly, no resolution
    # --extra agentcore: include agentcore optional deps
    # --extra dev: include dev dependencies for testing
    log_info "Installing dependencies from uv.lock..."
    uv sync --frozen --extra agentcore --extra dev
    
    # Verify installation
    log_info "Verifying installation..."
    if uv run python -c "import fastapi" 2>/dev/null; then
        log_success "FastAPI installed successfully"
    else
        log_error "FastAPI installation verification failed"
        exit 1
    fi
    
    if uv run python -c "import uvicorn" 2>/dev/null; then
        log_success "Uvicorn installed successfully"
    else
        log_error "Uvicorn installation verification failed"
        exit 1
    fi
    
    log_success "Inference API dependencies installed successfully!"
    
    # ===========================================================
    # Install CDK Dependencies
    # ===========================================================
    
    log_info "Installing CDK dependencies..."
    cd "${PROJECT_ROOT}/infrastructure"
    
    if [ -f "package-lock.json" ]; then
        log_info "Running npm ci (clean install from package-lock.json)..."
        npm ci
    else
        log_error "package-lock.json not found. Cannot run npm ci."
        exit 1
    fi
    
    log_success "CDK dependencies installed successfully"
}

main "$@"
