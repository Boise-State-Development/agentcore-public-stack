#!/bin/bash
set -euo pipefail

# Script: Teardown Nightly Stack
# Description: Empties S3 buckets and destroys all CDK stacks for nightly deployment

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

# Empty S3 bucket
empty_bucket() {
    local bucket_name="$1"
    
    log_info "Emptying bucket: ${bucket_name}"
    
    # Check if bucket exists
    if ! aws s3api head-bucket --bucket "${bucket_name}" 2>/dev/null; then
        log_warn "Bucket ${bucket_name} does not exist, skipping"
        return 0
    fi
    
    # Delete all object versions and delete markers
    aws s3api list-object-versions \
        --bucket "${bucket_name}" \
        --output json \
        --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' \
        | jq -r '.Objects[]? | "\(.Key)\t\(.VersionId)"' \
        | while IFS=$'\t' read -r key version_id; do
            aws s3api delete-object --bucket "${bucket_name}" --key "${key}" --version-id "${version_id}" || true
        done
    
    aws s3api list-object-versions \
        --bucket "${bucket_name}" \
        --output json \
        --query '{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}}' \
        | jq -r '.Objects[]? | "\(.Key)\t\(.VersionId)"' \
        | while IFS=$'\t' read -r key version_id; do
            aws s3api delete-object --bucket "${bucket_name}" --key "${key}" --version-id "${version_id}" || true
        done
    
    # Delete all objects (non-versioned)
    aws s3 rm "s3://${bucket_name}" --recursive || true
    
    log_success "Bucket ${bucket_name} emptied"
}

# Find and empty all S3 buckets with nightly prefix
empty_nightly_buckets() {
    log_info "Finding S3 buckets with prefix: ${CDK_PROJECT_PREFIX}"
    
    local buckets=$(aws s3api list-buckets \
        --query "Buckets[?starts_with(Name, '${CDK_PROJECT_PREFIX}')].Name" \
        --output text)
    
    if [ -z "${buckets}" ]; then
        log_info "No S3 buckets found with prefix ${CDK_PROJECT_PREFIX}"
        return 0
    fi
    
    log_info "Found buckets: ${buckets}"
    
    for bucket in ${buckets}; do
        empty_bucket "${bucket}"
    done
    
    log_success "All nightly S3 buckets emptied"
}

# Force delete Secrets Manager secrets (bypasses 7-day recovery window)
force_delete_secrets() {
    log_info "Force-deleting Secrets Manager secrets with prefix: ${CDK_PROJECT_PREFIX}"

    local secret_names=(
        "${CDK_PROJECT_PREFIX}-auth-secret"
        "${CDK_PROJECT_PREFIX}-oauth-client-secrets"
        "${CDK_PROJECT_PREFIX}-auth-provider-secrets"
    )

    for secret_name in "${secret_names[@]}"; do
        log_info "Force-deleting secret: ${secret_name}"
        aws secretsmanager delete-secret \
            --secret-id "${secret_name}" \
            --force-delete-without-recovery \
            --region "${CDK_AWS_REGION}" 2>/dev/null && \
            log_success "Secret ${secret_name} force-deleted" || \
            log_warn "Secret ${secret_name} not found or already deleted, skipping"
    done
}

# Delete all CloudWatch log groups containing the project prefix anywhere in the name
delete_cloudwatch_logs() {
    log_info "Deleting CloudWatch log groups containing: ${CDK_PROJECT_PREFIX}"

    # Log groups can be under /aws/ecs/, /aws/lambda/, etc. — prefix search won't catch them.
    # Paginate through all log groups and filter by project prefix in the name.
    local next_token=""
    local log_groups=()

    while true; do
        local response
        if [ -n "${next_token}" ]; then
            response=$(aws logs describe-log-groups \
                --next-token "${next_token}" \
                --limit 50 \
                --output json \
                --region "${CDK_AWS_REGION}" 2>/dev/null || echo '{}')
        else
            response=$(aws logs describe-log-groups \
                --limit 50 \
                --output json \
                --region "${CDK_AWS_REGION}" 2>/dev/null || echo '{}')
        fi

        local page_groups
        page_groups=$(echo "${response}" | jq -r \
            --arg prefix "${CDK_PROJECT_PREFIX}" \
            '.logGroups[]?.logGroupName | select(contains($prefix))' 2>/dev/null || true)

        while IFS= read -r group; do
            [ -n "${group}" ] && log_groups+=("${group}")
        done <<< "${page_groups}"

        next_token=$(echo "${response}" | jq -r '.nextToken // empty' 2>/dev/null || true)
        [ -z "${next_token}" ] && break
    done

    if [ ${#log_groups[@]} -eq 0 ]; then
        log_info "No CloudWatch log groups found containing ${CDK_PROJECT_PREFIX}"
        return 0
    fi

    log_info "Found ${#log_groups[@]} log group(s) to delete"

    for log_group in "${log_groups[@]}"; do
        log_info "Deleting log group: ${log_group}"
        aws logs delete-log-group \
            --log-group-name "${log_group}" \
            --region "${CDK_AWS_REGION}" 2>/dev/null && \
            log_success "Deleted ${log_group}" || \
            log_warn "Failed to delete ${log_group}, skipping"
    done

    log_success "CloudWatch log group cleanup complete"
}

# Delete S3 Vector Buckets (not visible via standard s3api list-buckets)
delete_vector_buckets() {
    log_info "Finding S3 Vector Buckets with prefix: ${CDK_PROJECT_PREFIX}"

    local vector_buckets
    vector_buckets=$(aws s3vectors list-vector-buckets \
        --region "${CDK_AWS_REGION}" \
        --output json \
        --query "vectorBuckets[?starts_with(vectorBucketName, '${CDK_PROJECT_PREFIX}')].vectorBucketName" \
        2>/dev/null | jq -r '.[]?' || true)

    if [ -z "${vector_buckets}" ]; then
        log_info "No S3 Vector Buckets found with prefix ${CDK_PROJECT_PREFIX}"
        return 0
    fi

    log_info "Found vector buckets: ${vector_buckets}"

    while IFS= read -r vbucket; do
        [ -z "${vbucket}" ] && continue
        log_info "Deleting vector bucket: ${vbucket}"
        aws s3vectors delete-vector-bucket \
            --vector-bucket-name "${vbucket}" \
            --region "${CDK_AWS_REGION}" 2>/dev/null && \
            log_success "Vector bucket ${vbucket} deleted" || \
            log_warn "Failed to delete vector bucket ${vbucket}, skipping"
    done <<< "${vector_buckets}"

    log_success "All S3 Vector Buckets deleted"
}

# Logical IDs of the resources the stack's latest delete attempt left behind.
failed_resources() {
    local stack_name="$1"
    aws cloudformation list-stack-resources \
        --stack-name "${stack_name}" \
        --region "${CDK_AWS_REGION}" \
        --query 'StackResourceSummaries[?ResourceStatus==`DELETE_FAILED`].LogicalResourceId' \
        --output text 2>/dev/null | tr '\t' '\n' | sed '/^$/d' || true
}

# Print why a delete failed, so the run is debuggable.
report_delete_failure() {
    local stack_name="$1"
    log_error "Failed to delete ${stack_name}. Current status:"
    aws cloudformation describe-stacks \
        --stack-name "${stack_name}" \
        --region "${CDK_AWS_REGION}" \
        --query 'Stacks[0].[StackStatus,StackStatusReason]' \
        --output text 2>&1 || true
    log_error "Resources that refused to delete:"
    aws cloudformation describe-stack-events \
        --stack-name "${stack_name}" \
        --region "${CDK_AWS_REGION}" \
        --query 'StackEvents[?ResourceStatus==`DELETE_FAILED`].[LogicalResourceId,ResourceType,ResourceStatusReason]' \
        --output table 2>&1 || true
}

# One delete-stack call plus its wait. A delete-stack on a DELETE_FAILED stack
# retries only the resources that failed, so this is also the retry.
delete_stack_once() {
    local stack_name="$1"
    shift
    if ! aws cloudformation delete-stack \
        --stack-name "${stack_name}" \
        --region "${CDK_AWS_REGION}" "$@"; then
        log_error "delete-stack API call failed for ${stack_name}"
        return 1
    fi
    log_info "Waiting for ${stack_name} to reach DELETE_COMPLETE..."
    aws cloudformation wait stack-delete-complete \
        --stack-name "${stack_name}" \
        --region "${CDK_AWS_REGION}"
}

# Delete a resource CloudFormation was told to retain, through Cloud Control so
# one call covers every resource type. Its names are fixed per prefix, so a
# retained table or bucket left in place would fail the next nightly's create.
delete_retained_resource() {
    local resource_type="$1"
    local physical_id="$2"
    log_info "Deleting retained ${resource_type} ${physical_id} via Cloud Control..."
    local token
    if ! token=$(aws cloudcontrol delete-resource \
        --type-name "${resource_type}" \
        --identifier "${physical_id}" \
        --region "${CDK_AWS_REGION}" \
        --query 'ProgressEvent.RequestToken' --output text 2>&1); then
        log_warn "Cloud Control refused ${physical_id}: ${token}"
        return 1
    fi
    local status=""
    for _ in $(seq 1 60); do
        status=$(aws cloudcontrol get-resource-request-status \
            --request-token "${token}" \
            --region "${CDK_AWS_REGION}" \
            --query 'ProgressEvent.OperationStatus' --output text 2>/dev/null || echo "UNKNOWN")
        case "${status}" in
            SUCCESS) log_success "Deleted retained ${physical_id}"; return 0 ;;
            FAILED|CANCEL_COMPLETE) break ;;
        esac
        sleep 10
    done
    log_warn "Retained ${physical_id} not deleted (status ${status})"
    return 1
}

# Destroy the PlatformStack (single-stack architecture)
destroy_stacks() {
    # Single-stack architecture: every resource for a nightly deployment
    # lives in ${CDK_PROJECT_PREFIX}-PlatformStack. Delete it via
    # `aws cloudformation delete-stack` (not `cdk destroy`) so teardown
    # works without a CDK synth and deletes by the real CFN stack name —
    # `cdk destroy <name>` silently no-ops when the name isn't in the
    # current synth. Legacy multi-stack deployments are handled by
    # scripts/teardown/destroy.sh, not here.
    #
    # A stack left in DELETE_FAILED blocks the next nightly's deploy, so a
    # failure is retried, then the stuck resources are retained (which always
    # completes the stack delete) and deleted directly. Seen 2026-11-01: a
    # DynamoDB 500 on one table's delete left the stack DELETE_FAILED, and the
    # 2026-11-02 nightly could not deploy.
    local stack_name="${CDK_PROJECT_PREFIX}-PlatformStack"
    local attempts="${TEARDOWN_DELETE_ATTEMPTS:-3}"

    log_info "Destroying ${stack_name} via CloudFormation..."

    if ! aws cloudformation describe-stacks \
        --stack-name "${stack_name}" \
        --region "${CDK_AWS_REGION}" \
        --query 'Stacks[0].StackName' --output text >/dev/null 2>&1; then
        log_warn "Stack ${stack_name} does not exist, skipping"
        return 0
    fi

    local attempt
    for attempt in $(seq 1 "${attempts}"); do
        if delete_stack_once "${stack_name}"; then
            log_success "${stack_name} destroyed"
            return 0
        fi
        report_delete_failure "${stack_name}"
        if [ "${attempt}" -lt "${attempts}" ]; then
            log_warn "Delete attempt ${attempt}/${attempts} failed; retrying in 30s"
            sleep 30
        fi
    done

    # Still DELETE_FAILED: record what is stuck, retain it so the stack goes
    # away, then delete each retained resource directly.
    local logical_ids
    logical_ids=$(failed_resources "${stack_name}")
    if [ -z "${logical_ids}" ]; then
        log_error "${stack_name} did not delete and names no failed resource to retain"
        return 1
    fi

    local retained=()
    local logical_id
    while IFS= read -r logical_id; do
        local row
        row=$(aws cloudformation describe-stack-resource \
            --stack-name "${stack_name}" \
            --logical-resource-id "${logical_id}" \
            --region "${CDK_AWS_REGION}" \
            --query 'StackResourceDetail.[ResourceType,PhysicalResourceId]' \
            --output text 2>/dev/null || true)
        retained+=("${logical_id}"$'\t'"${row}")
    done <<< "${logical_ids}"

    log_warn "Retaining ${#retained[@]} stuck resource(s) to finish the stack delete"
    # shellcheck disable=SC2086 # one argument per logical id
    if ! delete_stack_once "${stack_name}" --retain-resources $(echo ${logical_ids}); then
        report_delete_failure "${stack_name}"
        return 1
    fi
    log_success "${stack_name} destroyed (with ${#retained[@]} resource(s) retained)"

    local leftover=0
    local entry
    {
        echo "### Nightly teardown retained resources"
        echo
        echo "| Logical id | Type | Physical id | Deleted |"
        echo "|---|---|---|---|"
    } >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
    for entry in "${retained[@]}"; do
        local lid rtype pid deleted="yes"
        IFS=$'\t' read -r lid rtype pid <<< "${entry}"
        if [ -z "${pid}" ] || [ "${pid}" = "None" ] || ! delete_retained_resource "${rtype}" "${pid}"; then
            deleted="**no**"
            leftover=$((leftover + 1))
        fi
        echo "| ${lid} | ${rtype} | ${pid} | ${deleted} |" >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
    done

    if [ "${leftover}" -gt 0 ]; then
        log_error "${leftover} retained resource(s) still exist; the next nightly's create may collide with them"
        return 1
    fi
    return 0
}

main() {
    log_info "Starting teardown of nightly deployment..."
    
    # Validate required environment variables
    if [ -z "${CDK_PROJECT_PREFIX:-}" ]; then
        log_error "CDK_PROJECT_PREFIX environment variable is required"
        exit 1
    fi
    
    if [ -z "${CDK_AWS_REGION:-}" ]; then
        log_error "CDK_AWS_REGION environment variable is required"
        exit 1
    fi
    
    log_info "Project prefix: ${CDK_PROJECT_PREFIX}"
    log_info "AWS region: ${CDK_AWS_REGION}"
    
    # Empty S3 buckets first
    empty_nightly_buckets

    # Delete S3 Vector Buckets (not listed by standard s3api, must use s3vectors API)
    delete_vector_buckets
    
    # Force-delete Secrets Manager secrets before CDK destroy
    # CloudFormation only schedules secrets for deletion (7-day recovery window),
    # which causes the next nightly deploy to fail with "already exists" errors.
    force_delete_secrets
    
    # Destroy CDK stacks
    destroy_stacks

    # Delete CloudWatch log groups after CDK destroy (CDK may recreate them during destroy)
    delete_cloudwatch_logs
    
    log_success "Nightly deployment teardown complete!"
}

main "$@"
