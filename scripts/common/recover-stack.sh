#!/bin/bash
# Shared helper: recover a CloudFormation stack stuck in DELETE_FAILED state.
#
# Usage (source from any deploy script):
#   source scripts/common/recover-stack.sh
#   recover_delete_failed_stack "MyStack"
#
# If the stack is in DELETE_FAILED, this function:
#   1. Identifies the resources that blocked deletion
#   2. Retries the delete, retaining those resources
#   3. Waits for DELETE_COMPLETE
#
# If the stack is in any other state (or doesn't exist), this is a no-op.
#
# Required environment variables:
#   CDK_AWS_REGION — AWS region

recover_delete_failed_stack() {
    local stack_name="$1"
    local region="${CDK_AWS_REGION:?CDK_AWS_REGION must be set}"

    # Get current stack status (returns empty string if stack doesn't exist)
    local status
    status=$(aws cloudformation describe-stacks \
        --stack-name "${stack_name}" \
        --region "${region}" \
        --query "Stacks[0].StackStatus" \
        --output text 2>/dev/null || echo "")

    if [ "${status}" != "DELETE_FAILED" ]; then
        return 0
    fi

    echo "[WARN] Stack ${stack_name} is in DELETE_FAILED state — attempting recovery..."

    # Find resources that blocked deletion (still in DELETE_FAILED status)
    local retain_ids
    retain_ids=$(aws cloudformation list-stack-resources \
        --stack-name "${stack_name}" \
        --region "${region}" \
        --query "StackResourceSummaries[?ResourceStatus=='DELETE_FAILED'].LogicalResourceId" \
        --output text 2>/dev/null || echo "")

    if [ -n "${retain_ids}" ] && [ "${retain_ids}" != "None" ]; then
        # Build --retain-resources argument
        local retain_args=()
        for id in ${retain_ids}; do
            retain_args+=("${id}")
        done
        echo "[INFO] Retaining stuck resources: ${retain_args[*]}"
        aws cloudformation delete-stack \
            --stack-name "${stack_name}" \
            --region "${region}" \
            --retain-resources "${retain_args[@]}"
    else
        # No specific resources blocking — just retry the delete
        aws cloudformation delete-stack \
            --stack-name "${stack_name}" \
            --region "${region}"
    fi

    echo "[INFO] Waiting for stack deletion to complete..."
    aws cloudformation wait stack-delete-complete \
        --stack-name "${stack_name}" \
        --region "${region}" \
        --no-cli-pager

    echo "[INFO] Stack ${stack_name} deleted — deploy can proceed"
}
