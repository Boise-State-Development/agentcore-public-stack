#!/bin/bash
# Frontend build script - Build Angular application for production
# This script builds the Angular application using production configuration

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

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

# Check if frontend directory exists
if [ ! -d "${FRONTEND_DIR}" ]; then
    log_error "Frontend directory not found: ${FRONTEND_DIR}"
    exit 1
fi

# Check if node_modules exists
if [ ! -d "${FRONTEND_DIR}/node_modules" ]; then
    log_error "node_modules not found. Please run install.sh first."
    log_error "Run: scripts/stack-frontend/install.sh"
    exit 1
fi

log_info "Building frontend application..."
log_info "Frontend directory: ${FRONTEND_DIR}"

# Change to frontend directory
cd "${FRONTEND_DIR}"

# Check if Angular CLI is available
if [ ! -f "node_modules/.bin/ng" ]; then
    log_error "Angular CLI not found in node_modules. Please run install.sh first."
    exit 1
fi

# Get build configuration from environment or use default
BUILD_CONFIG="${BUILD_CONFIG:-production}"
log_info "Build configuration: ${BUILD_CONFIG}"

# Clean previous build output (optional, but recommended)
if [ -d "dist" ]; then
    log_info "Cleaning previous build output..."
    rm -rf dist
fi

# Build the Angular application
log_info "Running: ng build --configuration ${BUILD_CONFIG}"
./node_modules/.bin/ng build --configuration "${BUILD_CONFIG}"

# Verify build output
if [ ! -d "dist" ]; then
    log_error "Build failed: dist directory not created"
    exit 1
fi

# Find the build output directory
# Angular may output to different locations depending on version:
# - dist/ai.client/browser/ (Angular 17+)
# - dist/ai.client/ (older versions)
# - dist/ (direct output)

if [ -f "dist/ai.client/browser/index.html" ]; then
    BUILD_OUTPUT_DIR="dist/ai.client/browser"
elif [ -f "dist/ai.client/index.html" ]; then
    BUILD_OUTPUT_DIR="dist/ai.client"
elif [ -f "dist/index.html" ]; then
    BUILD_OUTPUT_DIR="dist"
else
    log_error "Build failed: index.html not found in expected locations"
    log_error "Checked:"
    log_error "  - dist/ai.client/browser/index.html"
    log_error "  - dist/ai.client/index.html"
    log_error "  - dist/index.html"
    log_error ""
    log_error "Actual dist/ structure:"
    find dist -type f -name "index.html" || ls -laR dist/
    exit 1
fi

log_info "Build completed successfully!"
log_info "Build output: ${BUILD_OUTPUT_DIR}"

# Display build statistics
if [ -d "${BUILD_OUTPUT_DIR}" ]; then
    FILE_COUNT=$(find "${BUILD_OUTPUT_DIR}" -type f | wc -l)
    TOTAL_SIZE=$(du -sh "${BUILD_OUTPUT_DIR}" | cut -f1)
    log_info "Files created: ${FILE_COUNT}"
    log_info "Total size: ${TOTAL_SIZE}"
fi

# List main files
log_info "Main build files:"
ls -lh "${BUILD_OUTPUT_DIR}"/*.html 2>/dev/null || true
ls -lh "${BUILD_OUTPUT_DIR}"/*.js 2>/dev/null || true
ls -lh "${BUILD_OUTPUT_DIR}"/*.css 2>/dev/null || true
