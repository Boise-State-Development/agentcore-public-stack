#!/bin/bash

#============================================================
# MCP Sandbox Stack - Deploy
#
# Deploys the MCP Sandbox Stack (S3, CloudFront, Route53) that serves the
# MCP Apps sandbox-proxy shell at mcp-sandbox.{domain}.
#
# Deploy tier 1: reads no cross-stack SSM. Parallel-safe with Artifacts,
# RAG Ingestion, Gateway, and Fine-Tuning. Inert until the SPA wiring
# (PR #4) and MCP_APPS_HOST_ENABLED (PR #7) land.
#============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

source "${PROJECT_ROOT}/scripts/common/load-env.sh"
source "${PROJECT_ROOT}/scripts/common/recover-stack.sh"

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

if [ "${CDK_MCP_SANDBOX_ENABLED:-false}" != "true" ]; then
    log_info "CDK_MCP_SANDBOX_ENABLED is not 'true' — MCP Sandbox Stack is disabled; skipping deploy."
    exit 0
fi

log_info "Deploying MCP Sandbox Stack..."
cd "${PROJECT_ROOT}/infrastructure"

if [ ! -d "node_modules" ]; then
    log_info "node_modules not found in CDK directory. Installing dependencies..."
    npm ci
fi

# Recover from DELETE_FAILED state if a previous teardown left the stack broken.
recover_delete_failed_stack "${CDK_PROJECT_PREFIX}-McpSandboxStack"

CONTEXT_PARAMS=$(build_cdk_context_params)

# Prefer the pre-synthesized template when available (CI path) so deploy
# matches exactly what was reviewed in cdk diff.
if [ -d "cdk.out" ] && [ -f "cdk.out/McpSandboxStack.template.json" ]; then
    log_info "Using pre-synthesized template from cdk.out/"
    eval "cdk deploy McpSandboxStack ${CONTEXT_PARAMS} \
        --app \"cdk.out/\" \
        --require-approval never \
        --outputs-file mcp-sandbox-outputs.json"
else
    log_info "Synthesizing and deploying in one step..."
    eval "cdk deploy McpSandboxStack ${CONTEXT_PARAMS} \
        --require-approval never \
        --outputs-file mcp-sandbox-outputs.json"
fi

log_success "MCP Sandbox Stack deployed"

if [ -f "mcp-sandbox-outputs.json" ]; then
    log_info "Stack outputs:"
    cat mcp-sandbox-outputs.json
fi
