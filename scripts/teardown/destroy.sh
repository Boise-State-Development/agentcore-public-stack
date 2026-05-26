#!/bin/bash

#============================================================
# Teardown - Destroy All CDK Stacks
#
# Destroys all CDK stacks in reverse deployment order.
# Infrastructure stack is destroyed last since all others depend on it.
#
# Usage: bash scripts/teardown/destroy.sh
#============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Source common utilities
source "${PROJECT_ROOT}/scripts/common/load-env.sh"

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

# ===========================================================
# Destroy all stacks (parallel where possible)
#
# Dependency graph:
#   All application stacks depend on InfrastructureStack.
#   Application stacks are independent of each other.
#
# Strategy:
#   Phase 1: Destroy all application stacks in parallel
#   Phase 2: Destroy InfrastructureStack (foundation)
# ===========================================================

cd "${PROJECT_ROOT}/infrastructure"

# Ensure dependencies are installed
if [ ! -d "node_modules" ]; then
    log_info "Installing CDK dependencies..."
    npm ci
fi

# Build CDK context params
CDK_CONTEXT_PARAMS=$(build_cdk_context_params)

# Phase 1: All application stacks (independent, can run in parallel)
PARALLEL_STACKS=(
    "SageMakerFineTuningStack"
    "RagIngestionStack"
    "GatewayStack"
    "InferenceApiStack"
    "AppApiStack"
    "FrontendStack"
    "McpSandboxStack"
    "ArtifactsStack"
)

# Phase 2: Foundation stack (must be last)
FOUNDATION_STACK="InfrastructureStack"

log_info "============================================"
log_info "  TEARDOWN: Destroying all CDK stacks"
log_info "  Project: ${CDK_PROJECT_PREFIX}"
log_info "  Region:  ${CDK_AWS_REGION}"
log_info "  Account: ${CDK_AWS_ACCOUNT}"
log_info "============================================"

# --- Phase 1: Destroy application stacks in parallel ---
log_info ""
log_info "Phase 1: Destroying application stacks in parallel..."

PIDS=()
STACK_NAMES=()
LOG_DIR=$(mktemp -d)

for STACK in "${PARALLEL_STACKS[@]}"; do
    FULL_STACK_NAME="${CDK_PROJECT_PREFIX}-${STACK}"
    log_info "  Starting destroy: ${FULL_STACK_NAME}"

    (
        eval cdk destroy "${STACK}" \
            ${CDK_CONTEXT_PARAMS} \
            --force \
            --exclusively > "${LOG_DIR}/${STACK}.log" 2>&1
    ) &
    PIDS+=($!)
    STACK_NAMES+=("${STACK}")
done

# Wait for all parallel destroys and collect results
FAILED_STACKS=()
for i in "${!PIDS[@]}"; do
    if wait "${PIDS[$i]}"; then
        log_success "Destroyed ${CDK_PROJECT_PREFIX}-${STACK_NAMES[$i]}"
    else
        log_warn "Failed to destroy ${CDK_PROJECT_PREFIX}-${STACK_NAMES[$i]} (may not exist)"
        FAILED_STACKS+=("${CDK_PROJECT_PREFIX}-${STACK_NAMES[$i]}")
        # Show logs for failed stacks
        if [ -f "${LOG_DIR}/${STACK_NAMES[$i]}.log" ]; then
            log_warn "  Output: $(tail -5 "${LOG_DIR}/${STACK_NAMES[$i]}.log")"
        fi
    fi
done

# --- Phase 2: Destroy foundation stack ---
log_info ""
log_info "Phase 2: Destroying foundation stack..."
FULL_STACK_NAME="${CDK_PROJECT_PREFIX}-${FOUNDATION_STACK}"
log_info "  Destroying ${FULL_STACK_NAME}..."

if eval cdk destroy "${FOUNDATION_STACK}" \
    ${CDK_CONTEXT_PARAMS} \
    --force \
    --exclusively 2>&1; then
    log_success "Destroyed ${FULL_STACK_NAME}"
else
    log_warn "Failed to destroy ${FULL_STACK_NAME}"
    FAILED_STACKS+=("${FULL_STACK_NAME}")
fi

# Cleanup
rm -rf "${LOG_DIR}"

# --- Summary ---
echo ""
log_info "============================================"
if [ ${#FAILED_STACKS[@]} -eq 0 ]; then
    log_success "All stacks destroyed successfully!"
else
    log_warn "The following stacks failed to destroy (they may not have been deployed):"
    for STACK in "${FAILED_STACKS[@]}"; do
        log_warn "  - ${STACK}"
    done
fi
log_info "============================================"
