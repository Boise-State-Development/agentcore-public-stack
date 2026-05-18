#!/bin/bash

#============================================================
# MCP Sandbox Stack - Test CDK
#
# Validates the synthesized CloudFormation template via cdk diff.
#============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

source "${PROJECT_ROOT}/scripts/common/load-env.sh"

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

if [ "${CDK_MCP_SANDBOX_ENABLED:-false}" != "true" ]; then
    log_info "CDK_MCP_SANDBOX_ENABLED is not 'true' — MCP Sandbox Stack is disabled; skipping test."
    exit 0
fi

log_info "Validating synthesized CloudFormation template..."
cd "${PROJECT_ROOT}/infrastructure"

if [ ! -d "cdk.out" ] || [ ! -f "cdk.out/McpSandboxStack.template.json" ]; then
    log_error "Synthesized template not found. Run synth.sh first."
    exit 1
fi

log_info "Running cdk diff to compare synthesized template with deployed stack..."
cdk diff McpSandboxStack --app "cdk.out/"

log_success "CloudFormation template validation completed"
