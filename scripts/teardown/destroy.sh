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
#   Legacy multi-stack: all application stacks depend on InfrastructureStack;
#   application stacks are independent of each other.
#   Current single-stack: everything lives in PlatformStack.
#
# Strategy:
#   Phase 1: Destroy all (legacy) application stacks in parallel
#   Phase 2: Destroy the foundation stack(s) last — InfrastructureStack
#            (legacy) and/or PlatformStack (current single-stack)
#
# Implementation notes:
#   - Uses `aws cloudformation delete-stack` directly instead of
#     `cdk destroy`. The repo has been refactored from a 9-stack
#     architecture to a 2-stack one and now to a single-stack
#     (PlatformStack) one, but legacy deployments still have the old
#     CFN stacks. Those stack names are not present in the current CDK
#     synth, so `cdk destroy <legacy-name>` silently no-ops (it exits 0
#     but never calls CloudFormation). `aws cloudformation delete-stack`
#     deletes by the actual CFN stack name and works regardless of
#     whether the stack is in the current CDK code.
#   - Each stack delete is polled with `aws cloudformation wait
#     stack-delete-complete`, so we know whether the stack is
#     really gone before reporting success.
#   - On DELETE_FAILED we surface the stack status reason and the
#     specific resources that refused to delete (typically S3
#     buckets that still have objects, or resources with RETAIN
#     policies if you want them gone).
# ===========================================================

cd "${PROJECT_ROOT}/infrastructure"

# Build CDK context params (still used by load-env.sh's environment
# loading, even though we're no longer invoking the CDK CLI).
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

# Phase 2: Foundation stack(s) (must be last). The legacy architecture's
# foundation is InfrastructureStack (every legacy app stack depends on it);
# the current architecture's single stack is PlatformStack (contains the VPC
# and everything else). The two belong to different architectures and never
# coexist in practice, but both are listed so this one script tears down
# either layout. Each is guarded by stack_exists, so a non-existent
# foundation is simply skipped.
FOUNDATION_STACKS=(
    "InfrastructureStack"
    "PlatformStack"
)

log_info "============================================"
log_info "  TEARDOWN: Destroying all CDK stacks"
log_info "  Project: ${CDK_PROJECT_PREFIX}"
log_info "  Region:  ${CDK_AWS_REGION}"
log_info "  Account: ${CDK_AWS_ACCOUNT}"
log_info "============================================"

# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------

# Returns 0 if the CloudFormation stack exists, 1 otherwise.
# Suppresses all output; use the exit code only.
stack_exists() {
    local stack_name="$1"
    aws cloudformation describe-stacks \
        --stack-name "${stack_name}" \
        --region "${CDK_AWS_REGION}" \
        --output text \
        --query 'Stacks[0].StackName' \
        >/dev/null 2>&1
}

# Delete a CloudFormation stack by name and wait for completion.
# All output goes to ${log_file}; the caller decides what to surface
# based on the exit code. On DELETE_FAILED, also dumps the stack
# status reason and the resources that refused to delete.
destroy_stack() {
    local stack_name="$1"
    local log_file="$2"

    {
        echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] Calling delete-stack for ${stack_name}"
        if ! aws cloudformation delete-stack \
            --stack-name "${stack_name}" \
            --region "${CDK_AWS_REGION}"; then
            echo "delete-stack API call failed for ${stack_name}"
            return 1
        fi

        echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] Waiting for ${stack_name} to reach DELETE_COMPLETE..."
        if aws cloudformation wait stack-delete-complete \
            --stack-name "${stack_name}" \
            --region "${CDK_AWS_REGION}"; then
            echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] ${stack_name} deleted successfully"
            return 0
        fi

        echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] wait stack-delete-complete returned non-zero for ${stack_name}"
        echo
        echo "Current stack status:"
        aws cloudformation describe-stacks \
            --stack-name "${stack_name}" \
            --region "${CDK_AWS_REGION}" \
            --query 'Stacks[0].[StackStatus,StackStatusReason]' \
            --output text 2>&1 || true
        echo
        echo "Resources that refused to delete:"
        aws cloudformation describe-stack-events \
            --stack-name "${stack_name}" \
            --region "${CDK_AWS_REGION}" \
            --query 'StackEvents[?ResourceStatus==`DELETE_FAILED`].[LogicalResourceId,ResourceType,ResourceStatusReason]' \
            --output table 2>&1 || true
        return 1
    } > "${log_file}" 2>&1
}

# ---------------------------------------------------------------
# Phase 1: Destroy application stacks in parallel
# ---------------------------------------------------------------
log_info ""
log_info "Phase 1: Destroying application stacks in parallel..."

PIDS=()
STACK_NAMES=()
LOG_DIR=$(mktemp -d)
SKIPPED_STACKS=()

for STACK in "${PARALLEL_STACKS[@]}"; do
    FULL_STACK_NAME="${CDK_PROJECT_PREFIX}-${STACK}"

    # Skip stacks that don't exist in CloudFormation. This avoids
    # spending time waiting for a stack that was never deployed
    # (or has already been torn down).
    if ! stack_exists "${FULL_STACK_NAME}"; then
        log_info "  Skipping ${FULL_STACK_NAME} (does not exist in CloudFormation)"
        SKIPPED_STACKS+=("${FULL_STACK_NAME}")
        continue
    fi

    log_info "  Starting destroy: ${FULL_STACK_NAME}"

    destroy_stack \
        "${FULL_STACK_NAME}" \
        "${LOG_DIR}/${STACK}.log" &
    PIDS+=($!)
    STACK_NAMES+=("${STACK}")
done

# Wait for all parallel destroys and collect results
FAILED_STACKS=()
for i in "${!PIDS[@]}"; do
    if wait "${PIDS[$i]}"; then
        log_success "Destroyed ${CDK_PROJECT_PREFIX}-${STACK_NAMES[$i]}"
    else
        FAILED_STACKS+=("${CDK_PROJECT_PREFIX}-${STACK_NAMES[$i]}")
        log_warn "Failed to destroy ${CDK_PROJECT_PREFIX}-${STACK_NAMES[$i]}"
        if [ -f "${LOG_DIR}/${STACK_NAMES[$i]}.log" ]; then
            log_warn "  Tail of log:"
            tail -n 30 "${LOG_DIR}/${STACK_NAMES[$i]}.log" \
                | sed 's/^/    /'
        fi
    fi
done

# ---------------------------------------------------------------
# Phase 2: Destroy foundation stack
# ---------------------------------------------------------------
log_info ""
log_info "Phase 2: Destroying foundation stack(s)..."

for FOUNDATION_STACK in "${FOUNDATION_STACKS[@]}"; do
    FULL_STACK_NAME="${CDK_PROJECT_PREFIX}-${FOUNDATION_STACK}"

    if ! stack_exists "${FULL_STACK_NAME}"; then
        log_info "  Skipping ${FULL_STACK_NAME} (does not exist in CloudFormation)"
        SKIPPED_STACKS+=("${FULL_STACK_NAME}")
        continue
    fi

    log_info "  Destroying ${FULL_STACK_NAME}..."
    FOUNDATION_LOG="${LOG_DIR}/${FOUNDATION_STACK}.log"
    if destroy_stack \
        "${FULL_STACK_NAME}" \
        "${FOUNDATION_LOG}"; then
        log_success "Destroyed ${FULL_STACK_NAME}"
    else
        FAILED_STACKS+=("${FULL_STACK_NAME}")
        log_warn "Failed to destroy ${FULL_STACK_NAME}"
        if [ -f "${FOUNDATION_LOG}" ]; then
            log_warn "  Tail of log:"
            tail -n 30 "${FOUNDATION_LOG}" | sed 's/^/    /'
        fi
    fi
done

# Cleanup
rm -rf "${LOG_DIR}"

# ---------------------------------------------------------------
# Summary
# ---------------------------------------------------------------
echo ""
log_info "============================================"
if [ ${#SKIPPED_STACKS[@]} -gt 0 ]; then
    log_info "Skipped (did not exist):"
    for STACK in "${SKIPPED_STACKS[@]}"; do
        log_info "  - ${STACK}"
    done
fi
if [ ${#FAILED_STACKS[@]} -eq 0 ]; then
    log_success "All existing stacks destroyed successfully!"
else
    log_warn "The following stacks failed to destroy:"
    for STACK in "${FAILED_STACKS[@]}"; do
        log_warn "  - ${STACK}"
    done
    log_info "============================================"
    exit 1
fi
log_info "============================================"
