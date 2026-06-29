#!/bin/bash
# scripts/frontend/test.sh — run the Angular test suite (headless) WITH coverage.
#
# Ported from the pre-#396 scripts/stack-frontend/test.sh during the
# platform-as-bootstrap refactor (the stack-* script dirs were removed and
# repo-shape.test.ts now forbids recreating them). Used by nightly.yml's
# "Test Frontend with Coverage" job, which uploads frontend/ai.client/coverage/.

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
    log_error "Run: scripts/frontend/install.sh"
    exit 1
fi

log_info "Running frontend tests..."
log_info "Frontend directory: ${FRONTEND_DIR}"

# Change to frontend directory
cd "${FRONTEND_DIR}"

# Check if Angular CLI is available
if [ ! -f "node_modules/.bin/ng" ]; then
    log_error "Angular CLI not found in node_modules. Please run install.sh first."
    exit 1
fi

# Run tests in headless mode (no watch, single run)
# This is appropriate for CI/CD environments
log_info "Running: ng test --no-watch --coverage (filtering CSS warnings)"

# Set environment variable for CI
export CI=true

# Run tests with code coverage
# Angular uses Vitest which handles headless mode automatically in CI environments
# The CI=true environment variable triggers headless behavior
# Filter out jsdom CSS parsing warnings that clutter logs
# Use PIPESTATUS to preserve test exit code after grep filter
./node_modules/.bin/ng test --no-watch --coverage 2>&1 | grep -v "Could not parse CSS stylesheet"
TEST_EXIT_CODE=${PIPESTATUS[0]}

if [ ${TEST_EXIT_CODE} -eq 0 ]; then
    log_info "All tests passed successfully!"

    # Display coverage summary if available
    if [ -f "coverage/index.html" ]; then
        log_info "Coverage report generated: coverage/index.html"
    fi

    # Display coverage summary from terminal output
    if [ -d "coverage" ]; then
        COVERAGE_DIR=$(find coverage -mindepth 1 -maxdepth 1 -type d | head -1)
        if [ -n "${COVERAGE_DIR}" ] && [ -f "${COVERAGE_DIR}/coverage-summary.json" ]; then
            log_info "Coverage summary available in: ${COVERAGE_DIR}/coverage-summary.json"
        fi
    fi
else
    log_error "Tests failed with exit code: ${TEST_EXIT_CODE}"
    exit ${TEST_EXIT_CODE}
fi
