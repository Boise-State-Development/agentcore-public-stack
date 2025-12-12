#!/bin/bash
set -euo pipefail

# Script: Install Dependencies for App API
# Description: Installs Python dependencies for the App API service

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
    log_info "Installing App API dependencies..."
    
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
    
    # Check if Python is installed
    if ! command -v python3 &> /dev/null; then
        log_error "Python 3 is not installed. Please install Python 3.9 or higher."
        exit 1
    fi
    
    # Display Python version
    PYTHON_VERSION=$(python3 --version)
    log_info "Using ${PYTHON_VERSION}"
    
    # Upgrade pip
    log_info "Upgrading pip..."
    python3 -m pip install --upgrade pip
    
    # Install the package and its dependencies
    log_info "Installing dependencies from pyproject.toml..."
    python3 -m pip install -e .
    
    # Verify installation
    log_info "Verifying installation..."
    if python3 -c "import fastapi" 2>/dev/null; then
        log_success "FastAPI installed successfully"
    else
        log_error "FastAPI installation verification failed"
        exit 1
    fi
    
    if python3 -c "import uvicorn" 2>/dev/null; then
        log_success "Uvicorn installed successfully"
    else
        log_error "Uvicorn installation verification failed"
        exit 1
    fi
    
    log_success "App API dependencies installed successfully!"
    
    # ===========================================================
    # Install CDK Dependencies
    # ===========================================================
    
    log_info "Installing CDK dependencies..."
    cd "${PROJECT_ROOT}/infrastructure"
    
    if [ -d "node_modules" ]; then
        log_info "node_modules already exists, skipping npm install"
    else
        npm install
    fi
    
    log_success "CDK dependencies installed successfully"
}

main "$@"
