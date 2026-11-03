#!/bin/bash
set -euo pipefail

# Script: Clear a Failed Nightly Stack
# Description: Before a nightly deploy, finish tearing down a PlatformStack that
# an earlier teardown left in DELETE_FAILED. CloudFormation refuses to update a
# stack in that state, so without this the deploy fails and so does every
# nightly after it until someone deletes the stack by hand (2026-11-02).
#
# Ephemeral nightly prefixes only: this deletes the stack, so it refuses any
# prefix that doesn't start with "nightly-".

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -z "${CDK_PROJECT_PREFIX:-}" ] || [ -z "${CDK_AWS_REGION:-}" ]; then
    echo "[ERROR] CDK_PROJECT_PREFIX and CDK_AWS_REGION are required" >&2
    exit 1
fi

case "${CDK_PROJECT_PREFIX}" in
    nightly-*) ;;
    *)
        echo "[ERROR] Refusing to clear ${CDK_PROJECT_PREFIX}: not a nightly prefix" >&2
        exit 1
        ;;
esac

stack_name="${CDK_PROJECT_PREFIX}-PlatformStack"
status=$(aws cloudformation describe-stacks \
    --stack-name "${stack_name}" \
    --region "${CDK_AWS_REGION}" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "NONE")

if [ "${status}" != "DELETE_FAILED" ]; then
    echo "[INFO] ${stack_name}: ${status}; nothing to clear"
    exit 0
fi

echo "[WARN] ${stack_name} is DELETE_FAILED from an earlier run; finishing its teardown before deploying"
bash "${SCRIPT_DIR}/teardown.sh"
