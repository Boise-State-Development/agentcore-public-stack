#!/bin/bash
set -euo pipefail

# Script: Run Playwright E2E Tests Against a Deployed Nightly Stack
# Description: Installs Playwright browsers, resolves the ALB URL, and runs
#              the full E2E suite using the CI-specific Playwright config.
#
# Required environment variables:
#   CDK_PROJECT_PREFIX    — CDK project prefix (e.g. nightly-develop)
#   CDK_AWS_REGION        — AWS region for CloudFormation lookups
#   ADMIN_USERNAME        — Cognito admin test account username
#   ADMIN_PASSWORD        — Cognito admin test account password
#   USER_USERNAME         — Cognito regular user test account username
#   USER_PASSWORD         — Cognito regular user test account password
#
# Optional environment variables:
#   CDK_ALB_SUBDOMAIN     — ALB subdomain (preferred over CF lookup)
#   CDK_HOSTED_ZONE_DOMAIN — Hosted zone domain (used with subdomain)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
FRONTEND_DIR="${PROJECT_ROOT}/frontend/ai.client"

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
# Resolve the base URL of the deployed stack
# ---------------------------------------------------------------------------
get_base_url() {
    # Prefer the custom HTTPS subdomain when configured
    if [ -n "${CDK_ALB_SUBDOMAIN:-}" ] && [ -n "${CDK_HOSTED_ZONE_DOMAIN:-}" ]; then
        echo "https://${CDK_ALB_SUBDOMAIN}.${CDK_HOSTED_ZONE_DOMAIN}"
        return 0
    fi

    local stack_name="${CDK_PROJECT_PREFIX}-InfrastructureStack"
    local alb_dns
    alb_dns=$(aws cloudformation describe-stacks \
        --stack-name "${stack_name}" \
        --query "Stacks[0].Outputs[?OutputKey=='AlbDnsName'].OutputValue" \
        --output text \
        --region "${CDK_AWS_REGION}")

    if [ -z "${alb_dns}" ]; then
        log_error "Could not retrieve ALB DNS name from stack ${stack_name}"
        return 1
    fi

    echo "https://${alb_dns}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    log_info "Starting Playwright E2E tests against deployed nightly stack..."

    # --- Validate required env vars ---
    local missing=()
    [ -z "${CDK_PROJECT_PREFIX:-}" ]  && missing+=("CDK_PROJECT_PREFIX")
    [ -z "${CDK_AWS_REGION:-}" ]      && missing+=("CDK_AWS_REGION")
    [ -z "${ADMIN_USERNAME:-}" ]      && missing+=("ADMIN_USERNAME")
    [ -z "${ADMIN_PASSWORD:-}" ]      && missing+=("ADMIN_PASSWORD")
    [ -z "${USER_USERNAME:-}" ]       && missing+=("USER_USERNAME")
    [ -z "${USER_PASSWORD:-}" ]       && missing+=("USER_PASSWORD")

    if [ ${#missing[@]} -gt 0 ]; then
        log_error "Missing required environment variables: ${missing[*]}"
        exit 1
    fi

    # --- Resolve base URL ---
    log_info "Resolving deployed stack URL..."
    local base_url
    base_url=$(get_base_url)
    log_info "Base URL: ${base_url}"

    # --- Verify frontend is reachable ---
    log_info "Verifying frontend is reachable..."
    local response_code
    response_code=$(curl -s -o /dev/null -w "%{http_code}" "${base_url}" --max-time 30 || echo "000")
    if [ "${response_code}" = "000" ]; then
        log_error "Frontend is not reachable at ${base_url} (connection failed)"
        exit 1
    fi
    log_info "Frontend responded with HTTP ${response_code}"

    # --- Change to frontend directory ---
    cd "${FRONTEND_DIR}"

    # --- Check node_modules ---
    if [ ! -d "node_modules" ]; then
        log_error "node_modules not found. Frontend dependencies must be installed first."
        exit 1
    fi

    # --- Install Playwright browsers ---
    log_info "Installing Playwright browsers (chromium only)..."
    npx playwright install --with-deps chromium

    # --- Run E2E tests ---
    log_info "Running Playwright E2E tests..."
    log_info "  Config: playwright.ci.config.ts"
    log_info "  Base URL: ${base_url}"

    export E2E_BASE_URL="${base_url}"
    export CI=true

    # Run tests — allow failure so we can still upload artifacts
    local exit_code=0
    npx playwright test --config=playwright.ci.config.ts || exit_code=$?

    if [ ${exit_code} -eq 0 ]; then
        log_success "All E2E tests passed!"
    else
        log_error "E2E tests failed with exit code: ${exit_code}"
    fi

    # Report location is always relative to the config file
    if [ -d "playwright-report" ]; then
        log_info "HTML report generated: playwright-report/index.html"
    fi

    return ${exit_code}
}

main "$@"
