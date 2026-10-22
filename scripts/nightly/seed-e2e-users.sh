#!/bin/bash
set -euo pipefail

# Script: Seed Cognito E2E Test Users
# Description: Creates (or confirms) the user and admin test accounts in the
#              nightly Cognito User Pool so that Playwright auth setup tests
#              can log in. Idempotent — safe to run multiple times.
#
# Required environment variables:
#   CDK_PROJECT_PREFIX  — CDK project prefix (resolves User Pool ID from SSM)
#   CDK_AWS_REGION      — AWS region
#   ADMIN_USERNAME      — Admin test account username
#   ADMIN_PASSWORD      — Admin test account password
#   USER_USERNAME       — Regular user test account username
#   USER_PASSWORD       — Regular user test account password

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()    { echo -e "${GREEN}[INFO]${NC} $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1" >&2; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }

# ---------------------------------------------------------------------------
# Ensure a Cognito user exists with a permanent password (CONFIRMED status).
# If the user already exists and is confirmed, this is a no-op.
# ---------------------------------------------------------------------------
ensure_user() {
    local user_pool_id="$1"
    local username="$2"
    local password="$3"
    local label="$4"

    log_info "  Ensuring ${label} user exists: ${username}"

    # Check if user already exists
    local user_status
    user_status=$(aws cognito-idp admin-get-user \
        --user-pool-id "${user_pool_id}" \
        --username "${username}" \
        --query "UserStatus" \
        --output text \
        --region "${CDK_AWS_REGION}" 2>/dev/null || echo "NOT_FOUND")

    if [ "${user_status}" = "NOT_FOUND" ]; then
        log_info "    User does not exist — creating..."
        aws cognito-idp admin-create-user \
            --user-pool-id "${user_pool_id}" \
            --username "${username}" \
            --message-action SUPPRESS \
            --region "${CDK_AWS_REGION}" \
            --no-cli-pager > /dev/null
        log_info "    User created (status: FORCE_CHANGE_PASSWORD)"
    else
        log_info "    User already exists (status: ${user_status})"
    fi

    # Set permanent password — moves user to CONFIRMED status regardless of
    # current state (FORCE_CHANGE_PASSWORD, RESET_REQUIRED, etc.)
    log_info "    Setting permanent password..."
    aws cognito-idp admin-set-user-password \
        --user-pool-id "${user_pool_id}" \
        --username "${username}" \
        --password "${password}" \
        --permanent \
        --region "${CDK_AWS_REGION}" \
        --no-cli-pager > /dev/null

    log_success "    ${label} user ready: ${username} (CONFIRMED)"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# RBAC for the regular E2E user.
#
# The nightly stack never runs the bootstrap data seeding, so its `default`
# role grants no tools. The turn-path smoke matrix (turn-smoke.sh) needs
# `calculator` and `ask_user_question`, so the regular user joins a Cognito
# group that the `e2e_user` app role maps to. The group name lands in the
# access token's `cognito:groups`, which the BFF login reads as the user's
# roles; the role itself is written through the shared repository by
# backend/scripts/seed_e2e_role.py so every mapping row the resolver reads
# is created together. Both steps are idempotent.
# ---------------------------------------------------------------------------
E2E_USER_GROUP="${E2E_USER_GROUP:-e2e-users}"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

ensure_group_membership() {
    local user_pool_id="$1"
    local username="$2"
    local group="$3"

    if ! aws cognito-idp get-group \
            --user-pool-id "${user_pool_id}" \
            --group-name "${group}" \
            --region "${CDK_AWS_REGION}" \
            --no-cli-pager > /dev/null 2>&1; then
        log_info "  Creating Cognito group ${group}..."
        aws cognito-idp create-group \
            --user-pool-id "${user_pool_id}" \
            --group-name "${group}" \
            --description "Nightly E2E test accounts (maps to the e2e_user app role)" \
            --region "${CDK_AWS_REGION}" \
            --no-cli-pager > /dev/null
    fi

    # admin-add-user-to-group is idempotent: re-adding a member is a no-op.
    aws cognito-idp admin-add-user-to-group \
        --user-pool-id "${user_pool_id}" \
        --username "${username}" \
        --group-name "${group}" \
        --region "${CDK_AWS_REGION}" \
        --no-cli-pager > /dev/null
    log_success "  ${username} is in group ${group}"
}

seed_e2e_role() {
    if ! command -v uv > /dev/null 2>&1; then
        log_warn "  uv not found; skipping the e2e_user role seed (tool rows of the turn smoke will FAIL)"
        return 0
    fi
    log_info "  Seeding the e2e_user app role (group ${E2E_USER_GROUP})..."
    (
        cd "${PROJECT_ROOT}/backend"
        AWS_REGION="${CDK_AWS_REGION}" uv run python scripts/seed_e2e_role.py \
            --prefix "${CDK_PROJECT_PREFIX}" \
            --region "${CDK_AWS_REGION}" \
            --group "${E2E_USER_GROUP}"
    )
}

main() {
    log_info "Seeding E2E test users in Cognito..."

    # Validate required env vars
    local missing=()
    [ -z "${CDK_PROJECT_PREFIX:-}" ] && missing+=("CDK_PROJECT_PREFIX")
    [ -z "${CDK_AWS_REGION:-}" ]     && missing+=("CDK_AWS_REGION")
    [ -z "${ADMIN_USERNAME:-}" ]     && missing+=("ADMIN_USERNAME")
    [ -z "${ADMIN_PASSWORD:-}" ]     && missing+=("ADMIN_PASSWORD")
    [ -z "${USER_USERNAME:-}" ]      && missing+=("USER_USERNAME")
    [ -z "${USER_PASSWORD:-}" ]      && missing+=("USER_PASSWORD")

    if [ ${#missing[@]} -gt 0 ]; then
        log_error "Missing required environment variables: ${missing[*]}"
        exit 1
    fi

    # Resolve User Pool ID from SSM
    local user_pool_id
    user_pool_id=$(aws ssm get-parameter \
        --name "/${CDK_PROJECT_PREFIX}/auth/cognito/user-pool-id" \
        --query "Parameter.Value" \
        --output text \
        --region "${CDK_AWS_REGION}")

    if [ -z "${user_pool_id}" ] || [ "${user_pool_id}" = "None" ]; then
        log_error "Could not resolve Cognito User Pool ID from SSM: /${CDK_PROJECT_PREFIX}/auth/cognito/user-pool-id"
        exit 1
    fi

    log_info "  User Pool ID: ${user_pool_id}"

    # Seed both accounts
    ensure_user "${user_pool_id}" "${USER_USERNAME}" "${USER_PASSWORD}" "regular"
    ensure_user "${user_pool_id}" "${ADMIN_USERNAME}" "${ADMIN_PASSWORD}" "admin"

    ensure_group_membership "${user_pool_id}" "${USER_USERNAME}" "${E2E_USER_GROUP}"
    seed_e2e_role

    log_success "E2E test users seeded successfully"
}

main "$@"
