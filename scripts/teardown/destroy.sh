#!/bin/bash

#============================================================
# Teardown - Destroy All CDK Stacks
#
# Destroys all CDK stacks in reverse deployment order.
# Infrastructure stack is destroyed last since all others depend on it.
# Empties each stack's DESTROY buckets first (see "Buckets" below).
#
# Usage: bash scripts/teardown/destroy.sh [--dry-run]
#
#   --dry-run  Read only. Lists each existing stack, the buckets it owns
#              and which of them would be emptied, then stops. Deletes
#              nothing and skips the Managed Knowledge Base step.
#============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

DRY_RUN=false
for arg in "$@"; do
    case "${arg}" in
        --dry-run) DRY_RUN=true ;;
        *)
            echo "Unknown argument: ${arg}" >&2
            echo "Usage: bash scripts/teardown/destroy.sh [--dry-run]" >&2
            exit 2
            ;;
    esac
done

# Source common utilities
source "${PROJECT_ROOT}/scripts/common/load-env.sh"

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

# load-env.sh treats jq as optional; the bucket step parses CloudFormation
# templates and S3 listings with it, so here it is required.
if ! command -v jq >/dev/null 2>&1; then
    log_error "jq is required (bash scripts/common/install-deps.sh installs it)"
    exit 1
fi

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
#
# Buckets:
#   The stack does not empty its own buckets. CDK's `autoDeleteObjects`
#   would, at the cost of one custom resource per bucket plus a provider
#   Lambda and role, about 15 of PlatformStack's 500-resource limit
#   (docs/specs/platform-stack-resource-budget.md §4). This script does
#   it instead, before each stack is deleted:
#   - The buckets come from the stack's own resources
#     (list-stack-resources, AWS::S3::Bucket), never a name pattern, so
#     another environment's bucket in the same account is never touched.
#   - Only a bucket whose DeletionPolicy in the deployed template is
#     Delete is emptied. With retainDataOnDelete every bucket is Retain,
#     CloudFormation leaves it standing, and this script leaves its
#     objects alone. Reading the deployed template rather than local
#     config means a stale cdk.context.json cannot change the answer.
#   - Every object version and delete marker goes, because a versioned
#     bucket with only noncurrent versions still refuses deletion.
#   - Log delivery (ALB, CloudFront) can write a new object after the
#     bucket is emptied and before its writer is deleted. On a
#     DELETE_FAILED whose failed resources include a bucket, the stack's
#     buckets are emptied again and the delete is retried once; by then
#     the writers are gone.
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
if [ "${DRY_RUN}" = "true" ]; then
    log_info "  TEARDOWN (DRY RUN): nothing will be deleted"
else
    log_info "  TEARDOWN: Destroying all CDK stacks"
fi
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

# Scratch space for templates and S3 listings, which are too large to pass
# as command-line arguments (Linux caps one argument at 128 KiB).
BUCKET_WORK_DIR=$(mktemp -d)
trap 'rm -rf "${BUCKET_WORK_DIR}"' EXIT

# The stack's buckets as "<bucket name>\t<DeletionPolicy>" lines, read from
# the stack itself: list-stack-resources for the physical names, the
# deployed template for each one's DeletionPolicy (absent means Delete,
# CloudFormation's default). Buckets already deleted, or never created, are
# left out.
#
# ⚠️ Every step carries an explicit `|| return 1`: callers use this in a
# condition, which suspends `set -e` for the whole function, and a failed
# read must not look like a stack with no buckets.
stack_buckets() {
    local stack_name="$1"
    local template resources
    template=$(mktemp "${BUCKET_WORK_DIR}/template.XXXXXX") || return 1
    resources=$(mktemp "${BUCKET_WORK_DIR}/resources.XXXXXX") || return 1

    aws cloudformation get-template \
        --stack-name "${stack_name}" \
        --region "${CDK_AWS_REGION}" \
        --template-stage Processed \
        --query 'TemplateBody' \
        --output json > "${template}" || return 1
    aws cloudformation list-stack-resources \
        --stack-name "${stack_name}" \
        --region "${CDK_AWS_REGION}" \
        --query "StackResourceSummaries[?ResourceType=='AWS::S3::Bucket' && ResourceStatus!='DELETE_COMPLETE'].[LogicalResourceId,PhysicalResourceId]" \
        --output json > "${resources}" || return 1

    # The CLI decodes a JSON template body into an object; a YAML one stays
    # a string, which CDK never deploys.
    jq -r --slurpfile tpl "${template}" '
        ($tpl[0] | if type == "string" then fromjson else . end | .Resources) as $res
        | .[] | select(.[1] != null)
        | [.[1], ($res[.[0]].DeletionPolicy // "Delete")] | @tsv
    ' "${resources}" || return 1
}

# Delete every object version and delete marker in a bucket, 1,000 at a
# time. Each pass re-lists from the start, because the previous pass's keys
# are gone. Fails on the first batch S3 reports errors for (Object Lock,
# a denied delete), rather than spinning on keys it cannot remove.
empty_bucket() {
    local bucket="$1"
    local listing payload response count errors total=0

    if ! aws s3api head-bucket --bucket "${bucket}" >/dev/null 2>&1; then
        # Already deleted, or not readable. In the second case the stack
        # delete reports the bucket, which is the useful error.
        echo "    ${bucket}: not reachable (already deleted?), skipping"
        return 0
    fi

    listing=$(mktemp "${BUCKET_WORK_DIR}/listing.XXXXXX") || return 1
    payload=$(mktemp "${BUCKET_WORK_DIR}/payload.XXXXXX") || return 1
    response=$(mktemp "${BUCKET_WORK_DIR}/response.XXXXXX") || return 1

    while :; do
        aws s3api list-object-versions \
            --bucket "${bucket}" \
            --max-keys 1000 \
            --no-paginate \
            --output json > "${listing}" || return 1
        count=$(jq '[(.Versions // [])[], (.DeleteMarkers // [])[]] | length' "${listing}") || return 1
        [ "${count}" -eq 0 ] && break

        jq '{Objects: [(.Versions // [])[], (.DeleteMarkers // [])[] | {Key, VersionId}], Quiet: true}' \
            "${listing}" > "${payload}" || return 1
        aws s3api delete-objects \
            --bucket "${bucket}" \
            --delete "file://${payload}" \
            --output json > "${response}" || return 1

        errors=$(jq '(.Errors // []) | length' "${response}") || return 1
        if [ "${errors}" -ne 0 ]; then
            echo "    ${bucket}: S3 refused ${errors} of ${count} deletes; first refusals:"
            jq -r '.Errors[:5][] | "      \(.Key) (\(.VersionId // "-")): \(.Code) \(.Message)"' "${response}" || true
            return 1
        fi
        total=$((total + count))
    done

    echo "    ${bucket}: emptied (${total} object versions and delete markers removed)"
}

# Empty every bucket the stack owns whose DeletionPolicy is Delete; report
# the rest. In --dry-run, only reports. Returns non-zero if the buckets
# could not be listed or any one of them could not be emptied.
empty_stack_buckets() {
    local stack_name="$1"
    local listing bucket policy sample
    local buckets=()
    local policies=()

    if ! listing=$(stack_buckets "${stack_name}"); then
        echo "    could not list the buckets of ${stack_name}"
        return 1
    fi
    if [ -z "${listing}" ]; then
        echo "    ${stack_name} owns no buckets"
        return 0
    fi

    # Collected first so nothing inside the loop below can read the listing
    # from stdin.
    while IFS=$'\t' read -r bucket policy; do
        [ -n "${bucket}" ] || continue
        buckets+=("${bucket}")
        policies+=("${policy}")
    done <<< "${listing}"

    local i
    for i in "${!buckets[@]}"; do
        bucket="${buckets[$i]}"
        policy="${policies[$i]}"
        if [ "${policy}" != "Delete" ]; then
            echo "    ${bucket}: DeletionPolicy ${policy}, kept with its objects"
            continue
        fi
        if [ "${DRY_RUN}" = "true" ]; then
            sample=$(aws s3api list-object-versions \
                --bucket "${bucket}" --max-keys 1000 --no-paginate --output json 2>/dev/null \
                | jq '[(.Versions // [])[], (.DeleteMarkers // [])[]] | length' 2>/dev/null) \
                || sample="?"
            [ "${sample}" = "1000" ] && sample="1000+"
            echo "    ${bucket}: would be emptied (${sample} object versions and delete markers)"
            continue
        fi
        empty_bucket "${bucket}" || return 1
    done
}

# Number of the stack's buckets currently in DELETE_FAILED.
failed_bucket_count() {
    aws cloudformation list-stack-resources \
        --stack-name "$1" \
        --region "${CDK_AWS_REGION}" \
        --query "length(StackResourceSummaries[?ResourceType=='AWS::S3::Bucket' && ResourceStatus=='DELETE_FAILED'])" \
        --output text 2>/dev/null || echo 0
}

# Delete a CloudFormation stack by name and wait for completion.
# All output goes to ${log_file}; the caller decides what to surface
# based on the exit code. If the delete fails on a bucket (a log write
# that landed after the bucket was emptied), empties the stack's buckets
# again and retries once. On a final DELETE_FAILED, also dumps the stack
# status reason and the resources that refused to delete.
destroy_stack() {
    local stack_name="$1"
    local log_file="$2"
    local attempt

    {
        for attempt in 1 2; do
            echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] Calling delete-stack for ${stack_name} (attempt ${attempt})"
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

            [ "${attempt}" -eq 1 ] || break
            [ "$(failed_bucket_count "${stack_name}")" != "0" ] || break
            echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] A bucket refused to delete; emptying ${stack_name}'s buckets again"
            empty_stack_buckets "${stack_name}" || break
        done

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
# Phase 0: Runtime-created Managed Knowledge Bases
#
# Before any stack, because these are NOT CloudFormation children —
# the provisioning saga creates them at runtime, so `delete-stack`
# does not touch them and they would survive the teardown, still
# billing at $5.00/GB-month and invisible in the CFN console
# (Requirement 20.8).
#
# Ordering is load-bearing: the Bedrock service role lives in
# PlatformStack and Bedrock needs it to perform the delete. Taking the
# stack down first is a plausible route into DELETE_UNSUCCESSFUL, a
# terminal state that retrying does not clear (Requirement 13.5).
#
# A failure here therefore aborts the whole teardown rather than being
# logged and stepped over. Leaving paid resources behind is the one
# outcome worse than a teardown that stops and says why.
# ---------------------------------------------------------------
log_info ""
log_info "Phase 0: Runtime-created resources (before any stack)..."

if [ "${DRY_RUN}" = "true" ]; then
    log_info "  Dry run: skipping the knowledge base step, which deletes as it goes"
else
    if ! bash "${SCRIPT_DIR}/managed-kb.sh"; then
        log_warn "============================================"
        log_warn "Managed Knowledge Base teardown did not complete."
        log_warn "Stopping BEFORE any stack is deleted, because the Bedrock"
        log_warn "service role lives in PlatformStack and its knowledge bases"
        log_warn "cannot be deleted without it."
        log_warn "Resolve the knowledge bases listed above, then re-run."
        log_warn "============================================"
        exit 1
    fi
fi

# ---------------------------------------------------------------
# Phase 1: Destroy application stacks in parallel
# ---------------------------------------------------------------
log_info ""
log_info "Phase 1: Destroying application stacks in parallel..."

PIDS=()
STACK_NAMES=()
LOG_DIR=$(mktemp -d)
SKIPPED_STACKS=()
FAILED_STACKS=()

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

    log_info "  Buckets of ${FULL_STACK_NAME}:"
    if ! empty_stack_buckets "${FULL_STACK_NAME}"; then
        log_warn "  Could not empty the buckets of ${FULL_STACK_NAME}; not deleting it"
        FAILED_STACKS+=("${FULL_STACK_NAME}")
        continue
    fi
    if [ "${DRY_RUN}" = "true" ]; then
        log_info "  Would delete ${FULL_STACK_NAME}"
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

    log_info "  Buckets of ${FULL_STACK_NAME}:"
    if ! empty_stack_buckets "${FULL_STACK_NAME}"; then
        log_warn "  Could not empty the buckets of ${FULL_STACK_NAME}; not deleting it"
        FAILED_STACKS+=("${FULL_STACK_NAME}")
        continue
    fi
    if [ "${DRY_RUN}" = "true" ]; then
        log_info "  Would delete ${FULL_STACK_NAME}"
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
if [ ${#FAILED_STACKS[@]} -eq 0 ] && [ "${DRY_RUN}" = "true" ]; then
    log_success "Dry run complete. Nothing was deleted."
elif [ ${#FAILED_STACKS[@]} -eq 0 ]; then
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
