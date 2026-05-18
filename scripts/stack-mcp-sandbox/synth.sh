#!/bin/bash

#============================================================
# MCP Sandbox Stack - Synthesize
#
# Synthesizes the MCP Sandbox Stack CloudFormation template.
# Skips silently if CDK_MCP_SANDBOX_ENABLED is false so the workflow
# can run unconditionally before the feature is turned on.
#============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

source "${PROJECT_ROOT}/scripts/common/load-env.sh"

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

if [ "${CDK_MCP_SANDBOX_ENABLED:-false}" != "true" ]; then
    log_info "CDK_MCP_SANDBOX_ENABLED is not 'true' — MCP Sandbox Stack is disabled; skipping synth."
    exit 0
fi

log_info "Synthesizing MCP Sandbox Stack CloudFormation template..."
cd "${PROJECT_ROOT}/infrastructure"

if [ ! -d "node_modules" ]; then
    log_info "node_modules not found in CDK directory. Installing dependencies..."
    npm ci
fi

log_info "Running CDK synth for McpSandboxStack..."

CONTEXT_PARAMS=$(build_cdk_context_params)

eval "cdk synth McpSandboxStack ${CONTEXT_PARAMS} --output \"${PROJECT_ROOT}/infrastructure/cdk.out\""

log_success "MCP Sandbox Stack CloudFormation template synthesized successfully"

if [ -d "${PROJECT_ROOT}/infrastructure/cdk.out" ]; then
    log_info "Synthesized stacks:"
    ls -lh "${PROJECT_ROOT}/infrastructure/cdk.out"/*.template.json 2>/dev/null || log_info "No template files found"
fi
