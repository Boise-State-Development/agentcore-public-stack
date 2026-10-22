#!/bin/bash
set -euo pipefail

# Script: Smoke Test Deployed Nightly Stack
# Description: Validates health endpoints for App API and Inference API

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

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

# Resolve the app-api base URL as clients reach it: the SPA distribution's
# `/api/*` behavior. The ALB only admits CloudFront's origin-facing ranges
# (albRestrictToCloudFront), so a GitHub runner cannot reach it directly.
# Source is the SSM parameter the SPA distribution construct writes; it holds
# either the custom domain (bare) or `https://<id>.cloudfront.net`.
get_api_base_url() {
    local frontend_url
    frontend_url=$(aws ssm get-parameter \
        --name "/${CDK_PROJECT_PREFIX}/frontend/url" \
        --query "Parameter.Value" \
        --output text \
        --region "${CDK_AWS_REGION}" 2>/dev/null || true)
    if [ -z "${frontend_url}" ] || [ "${frontend_url}" = "None" ]; then
        log_error "Could not resolve the frontend URL from SSM (/${CDK_PROJECT_PREFIX}/frontend/url)"
        return 1
    fi
    case "${frontend_url}" in
        https://*|http://*) ;;
        *) frontend_url="https://${frontend_url}" ;;
    esac
    echo "${frontend_url%/}/api"
}

# Test health endpoint with retries
test_health_endpoint() {
    local url="$1"
    local name="$2"
    local max_retries="${3:-20}"
    local retry_interval="${4:-15}"
    
    log_info "Testing ${name}: ${url}"
    
    for i in $(seq 1 ${max_retries}); do
        local response_code=$(curl -s -o /dev/null -w "%{http_code}" "${url}" --max-time 30)
        
        if [ "${response_code}" = "200" ]; then
            log_success "${name} health check passed (HTTP ${response_code})"
            return 0
        fi
        
        if [ ${i} -lt ${max_retries} ]; then
            log_info "${name} not ready (HTTP ${response_code}), retrying in ${retry_interval}s... (${i}/${max_retries})"
            sleep ${retry_interval}
        else
            log_error "${name} health check failed after ${max_retries} attempts (last HTTP ${response_code})"
            return 1
        fi
    done
}

main() {
    log_info "Starting smoke tests for nightly deployment..."
    
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
    
    # Resolve the app-api base URL (CloudFront /api/* -> ALB -> app-api)
    log_info "Retrieving the app-api base URL from SSM..."
    API_BASE_URL=$(get_api_base_url)
    log_info "App API base URL: ${API_BASE_URL}"

    # Test App API health endpoint through CloudFront, the only path in
    test_health_endpoint "${API_BASE_URL}/health" "App API"
    APP_API_RESULT=$?
    
    # Test Inference API
    # Note: Inference API runs on AgentCore Runtime (managed Bedrock service),
    # not behind the ALB. It is not reachable via a public HTTP endpoint.
    # Skipping direct health check — runtime health is managed by AgentCore.
    log_info "Skipping Inference API health check (AgentCore Runtime — not exposed via ALB)"
    INFERENCE_API_RESULT=0
    
    # Check results
    if [ ${APP_API_RESULT} -eq 0 ] && [ ${INFERENCE_API_RESULT} -eq 0 ]; then
        log_success "All smoke tests passed!"
        exit 0
    else
        log_error "Some smoke tests failed"
        exit 1
    fi
}

main "$@"
