#!/bin/bash
set -euo pipefail

# Script: Bootstrap Data Seeding
# Description: Resolves resource names from SSM and invokes the Python seed script

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Source common environment loader
source "${PROJECT_ROOT}/scripts/common/load-env.sh"

log_success() {
    echo -e "\033[0;32m✓ $1\033[0m"
}

main() {
    log_info "Starting bootstrap data seeding..."

    local prefix="${CDK_PROJECT_PREFIX}"
    local region="${CDK_AWS_REGION}"

    # Resolve DynamoDB table names from SSM
    log_info "Resolving resource names from SSM Parameter Store..."

    export DDB_USER_QUOTAS_TABLE
    DDB_USER_QUOTAS_TABLE=$(aws ssm get-parameter \
        --name "/${prefix}/quota/user-quotas-table-name" \
        --region "${region}" \
        --query "Parameter.Value" \
        --output text)
    log_info "User quotas table: ${DDB_USER_QUOTAS_TABLE}"

    export DDB_MANAGED_MODELS_TABLE
    DDB_MANAGED_MODELS_TABLE=$(aws ssm get-parameter \
        --name "/${prefix}/admin/managed-models-table-name" \
        --region "${region}" \
        --query "Parameter.Value" \
        --output text)
    log_info "Managed models table: ${DDB_MANAGED_MODELS_TABLE}"

    export DDB_APP_ROLES_TABLE
    DDB_APP_ROLES_TABLE=$(aws ssm get-parameter \
        --name "/${prefix}/rbac/app-roles-table-name" \
        --region "${region}" \
        --query "Parameter.Value" \
        --output text)
    log_info "App roles table: ${DDB_APP_ROLES_TABLE}"

    export AWS_REGION="${region}"

    # Invoke the Python seed script
    log_info "Running seed script..."
    python3 "${PROJECT_ROOT}/backend/scripts/seed_bootstrap_data.py"

    log_success "Bootstrap data seeding complete!"
}

main "$@"
