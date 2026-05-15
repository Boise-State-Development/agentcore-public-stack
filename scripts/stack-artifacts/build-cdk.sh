#!/bin/bash
set -euo pipefail

# Script: Build CDK Code for Artifacts Stack
# Description: Compiles TypeScript CDK code to JavaScript.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

log_info() { echo "[INFO] $1"; }
log_error() { echo "[ERROR] $1" >&2; }
log_success() { echo "[SUCCESS] $1"; }

main() {
    log_info "Building Artifacts Stack CDK code..."

    cd "${PROJECT_ROOT}/infrastructure"

    if [ ! -d "node_modules" ]; then
        log_error "node_modules not found. Run install.sh first."
        exit 1
    fi

    log_info "Compiling TypeScript..."
    npm run build

    log_success "Artifacts Stack CDK build completed"
}

main "$@"
