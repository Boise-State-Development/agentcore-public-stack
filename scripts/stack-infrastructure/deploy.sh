#!/bin/bash

#============================================================
# Infrastructure Stack - Deploy
# 
# Deploys the Infrastructure Stack (VPC, ALB, ECS Cluster)
# 
# This stack MUST be deployed FIRST before any application stacks.
#============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Source common utilities
source "${PROJECT_ROOT}/scripts/common/load-env.sh"

# ===========================================================
# Deploy Infrastructure Stack
# ===========================================================

log_info "Deploying Infrastructure Stack..."
cd "${PROJECT_ROOT}/infrastructure"

# Ensure dependencies are installed
if [ ! -d "node_modules" ]; then
    log_info "node_modules not found in CDK directory. Installing dependencies..."
    npm install
fi

# Bootstrap CDK (if not already bootstrapped)
log_info "Ensuring CDK is bootstrapped..."
cdk bootstrap aws://${CDK_DEFAULT_ACCOUNT}/${CDK_DEFAULT_REGION} || log_info "CDK already bootstrapped or bootstrap failed (continuing anyway)"

# Deploy the Infrastructure Stack
log_info "Deploying InfrastructureStack..."
cdk deploy InfrastructureStack \
    --require-approval never \
    --outputs-file "${PROJECT_ROOT}/infrastructure/infrastructure-outputs.json"

log_success "Infrastructure Stack deployed successfully"

# Display stack outputs
log_info "Stack outputs saved to infrastructure/infrastructure-outputs.json"

if [ -f "${PROJECT_ROOT}/infrastructure/infrastructure-outputs.json" ]; then
    log_info "Infrastructure Stack Outputs:"
    cat "${PROJECT_ROOT}/infrastructure/infrastructure-outputs.json"
fi
