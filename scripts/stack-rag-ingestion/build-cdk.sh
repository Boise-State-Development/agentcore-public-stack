#!/bin/bash
# RAG Ingestion CDK build script - Compile TypeScript CDK code
# This script compiles the CDK infrastructure code for the RAG Ingestion stack

set -euo pipefail

# Get the repository root directory
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CDK_DIR="${REPO_ROOT}/infrastructure"

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

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

# Check if CDK directory exists
if [ ! -d "${CDK_DIR}" ]; then
    log_error "CDK directory not found: ${CDK_DIR}"
    exit 1
fi

# Check if node_modules exists
if [ ! -d "${CDK_DIR}/node_modules" ]; then
    log_error "node_modules not found. Please run install step first."
    exit 1
fi

log_info "Building CDK infrastructure code..."
log_info "CDK directory: ${CDK_DIR}"

# Change to CDK directory
cd "${CDK_DIR}"

# Clean previous build output
if [ -d "bin" ] && [ -d "lib" ]; then
    log_info "Cleaning previous JavaScript build output..."
    find bin lib -name "*.js" -type f -delete
    find bin lib -name "*.d.ts" -type f -delete
fi

# Compile TypeScript
log_info "Compiling TypeScript..."
npm run build

# Verify build output
if [ ! -f "bin/infrastructure.js" ]; then
    log_error "Build failed: bin/infrastructure.js not created"
    exit 1
fi

log_success "CDK infrastructure code compiled successfully!"
log_info "Output files:"
ls -lh bin/*.js lib/*.js 2>/dev/null || log_info "JavaScript files created in bin/ and lib/"
