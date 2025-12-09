#!/bin/bash
# Environment loader and configuration validator
# This script loads configuration from cdk.context.json and exports as environment variables
# Usage: source scripts/common/load-env.sh

set -euo pipefail

# Get the repository root directory
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CDK_DIR="${REPO_ROOT}/infrastructure"
CONTEXT_FILE="${CDK_DIR}/cdk.context.json"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Logging functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if context file exists
if [ ! -f "${CONTEXT_FILE}" ]; then
    log_error "Configuration file not found: ${CONTEXT_FILE}"
    log_error "Please create cdk.context.json in the infrastructure directory"
    return 1 2>/dev/null || exit 1
fi

log_info "Loading configuration from ${CONTEXT_FILE}"

# Check if jq is available
if ! command -v jq &> /dev/null; then
    log_warn "jq is not installed. Using basic parsing (less robust)"
    USE_JQ=false
else
    USE_JQ=true
fi

# Function to extract value from JSON using jq or basic parsing
get_json_value() {
    local key="$1"
    local file="$2"
    
    if [ "$USE_JQ" = true ]; then
        jq -r ".${key} // empty" "$file" 2>/dev/null || echo ""
    else
        # Basic fallback parsing (not recommended for production)
        grep "\"${key}\"" "$file" | head -1 | sed 's/.*: "\?\([^",]*\)"\?.*/\1/' | tr -d ' '
    fi
}

# Export core configuration
# Priority: Environment variables > cdk.context.json
export CDK_PROJECT_PREFIX="${CDK_PROJECT_PREFIX:-$(get_json_value "projectPrefix" "${CONTEXT_FILE}")}"
export CDK_AWS_REGION="${CDK_AWS_REGION:-$(get_json_value "awsRegion" "${CONTEXT_FILE}")}"
export CDK_VPC_CIDR="${CDK_VPC_CIDR:-$(get_json_value "vpcCidr" "${CONTEXT_FILE}")}"

# AWS Account - try multiple sources (env vars take precedence)
CDK_CONTEXT_ACCOUNT=$(get_json_value "awsAccount" "${CONTEXT_FILE}")
export CDK_AWS_ACCOUNT="${CDK_AWS_ACCOUNT:-${CDK_CONTEXT_ACCOUNT:-${CDK_DEFAULT_ACCOUNT:-${AWS_ACCOUNT_ID:-}}}}"

# Optional configuration
export CDK_DOMAIN_NAME="${CDK_DOMAIN_NAME:-$(get_json_value "domainName" "${CONTEXT_FILE}")}"
export CDK_ENABLE_ROUTE53="${CDK_ENABLE_ROUTE53:-$(get_json_value "enableRoute53" "${CONTEXT_FILE}")}"
export CDK_CERTIFICATE_ARN="${CDK_CERTIFICATE_ARN:-$(get_json_value "certificateArn" "${CONTEXT_FILE}")}"

# Set CDK environment variables for deployment
export CDK_DEFAULT_ACCOUNT="${CDK_AWS_ACCOUNT}"
export CDK_DEFAULT_REGION="${CDK_AWS_REGION}"

# Validate required configuration
validate_config() {
    local errors=0
    
    if [ -z "${CDK_PROJECT_PREFIX}" ]; then
        log_error "projectPrefix is required in cdk.context.json"
        errors=$((errors + 1))
    fi
    
    if [ -z "${CDK_AWS_REGION}" ]; then
        log_error "awsRegion is required in cdk.context.json"
        errors=$((errors + 1))
    fi
    
    if [ -z "${CDK_AWS_ACCOUNT}" ]; then
        log_error "AWS Account ID is required. Set it in cdk.context.json, CDK_DEFAULT_ACCOUNT, or AWS_ACCOUNT_ID"
        errors=$((errors + 1))
    fi
    
    if [ $errors -gt 0 ]; then
        log_error "Configuration validation failed with ${errors} error(s)"
        return 1
    fi
    
    return 0
}

# Validate configuration
if ! validate_config; then
    return 1 2>/dev/null || exit 1
fi

# Display loaded configuration
log_info "Configuration loaded successfully:"
log_info "  Project Prefix: ${CDK_PROJECT_PREFIX}"
log_info "  AWS Account:    ${CDK_AWS_ACCOUNT}"
log_info "  AWS Region:     ${CDK_AWS_REGION}"
log_info "  VPC CIDR:       ${CDK_VPC_CIDR}"

if [ -n "${CDK_DOMAIN_NAME}" ]; then
    log_info "  Domain Name:    ${CDK_DOMAIN_NAME}"
fi

# Check AWS credentials
if ! aws sts get-caller-identity &> /dev/null; then
    log_warn "AWS credentials not configured or invalid"
    log_warn "Run 'aws configure' or set AWS_PROFILE environment variable"
else
    CALLER_IDENTITY=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "unknown")
    if [ "${CALLER_IDENTITY}" != "${CDK_AWS_ACCOUNT}" ] && [ "${CALLER_IDENTITY}" != "unknown" ]; then
        log_warn "AWS credentials account (${CALLER_IDENTITY}) does not match configured account (${CDK_AWS_ACCOUNT})"
    else
        log_info "  AWS Identity:   ${CALLER_IDENTITY}"
    fi
fi

log_info "Environment variables exported and ready for deployment"
