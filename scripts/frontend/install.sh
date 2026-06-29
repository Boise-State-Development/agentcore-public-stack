#!/bin/bash
# scripts/frontend/install.sh — install Angular frontend + CDK infrastructure deps.
#
# Ported from the pre-#396 scripts/stack-frontend/install.sh during the
# platform-as-bootstrap refactor (the stack-* script dirs were removed and
# repo-shape.test.ts now forbids recreating them). Used by nightly.yml's
# install-frontend job, which caches frontend/ai.client/node_modules.
#
# Installs, in order:
#   1. Angular frontend dependencies (npm ci)
#   2. CDK infrastructure dependencies (npm ci)

set -euo pipefail

# Get the repository root directory
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FRONTEND_DIR="${REPO_ROOT}/frontend/ai.client"
INFRA_DIR="${REPO_ROOT}/infrastructure"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m' # No Color

# Logging functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

# ===========================================================
# Install Frontend Dependencies
# ===========================================================

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
    log_error "package-lock.json not found. Cannot run npm ci."
    exit 1
fi

log_success "Frontend dependencies installed successfully!"

# Display Angular CLI version if available
if [ -f "node_modules/.bin/ng" ]; then
    log_info "Angular CLI version: $(./node_modules/.bin/ng version --version 2>/dev/null || echo 'unknown')"
fi

# ===========================================================
# Install CDK Dependencies
# ===========================================================

log_info "Installing CDK dependencies..."
cd "${INFRA_DIR}"

if [ -f "package-lock.json" ]; then
    log_info "Running npm ci (clean install from package-lock.json)..."
    npm ci
else
    log_error "package-lock.json not found. Cannot run npm ci."
    exit 1
fi

log_success "CDK dependencies installed successfully"
