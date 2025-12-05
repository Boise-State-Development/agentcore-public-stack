#!/bin/bash
# Frontend install script - Install Angular dependencies
# This script installs all frontend dependencies using npm ci

set -euo pipefail

# Get the repository root directory
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FRONTEND_DIR="${REPO_ROOT}/frontend/ai.client"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Logging functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if frontend directory exists
if [ ! -d "${FRONTEND_DIR}" ]; then
    log_error "Frontend directory not found: ${FRONTEND_DIR}"
    exit 1
fi

# Check if package.json exists
if [ ! -f "${FRONTEND_DIR}/package.json" ]; then
    log_error "package.json not found in ${FRONTEND_DIR}"
    exit 1
fi

log_info "Installing frontend dependencies..."
log_info "Frontend directory: ${FRONTEND_DIR}"

# Change to frontend directory
cd "${FRONTEND_DIR}"

# Check if Node.js is installed
if ! command -v node &> /dev/null; then
    log_error "Node.js is not installed. Please install Node.js first."
    log_error "Run: scripts/common/install-deps.sh"
    exit 1
fi

# Check if npm is installed
if ! command -v npm &> /dev/null; then
    log_error "npm is not installed. Please install npm first."
    exit 1
fi

# Display Node.js and npm versions
log_info "Node.js version: $(node --version)"
log_info "npm version: $(npm --version)"

# Use npm ci for clean, reproducible builds
# npm ci is faster and more reliable than npm install in CI/CD environments
if [ -f "package-lock.json" ]; then
    log_info "Running npm ci (clean install from package-lock.json)..."
    npm ci
else
    log_info "No package-lock.json found. Running npm install..."
    log_info "Note: Consider committing package-lock.json for reproducible builds"
    npm install
fi

log_info "Frontend dependencies installed successfully!"

# Display Angular CLI version if available
if [ -f "node_modules/.bin/ng" ]; then
    log_info "Angular CLI version: $(./node_modules/.bin/ng version --version 2>/dev/null || echo 'unknown')"
fi
