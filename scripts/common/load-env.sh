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

# Helper function to conditionally add CDK context parameters
# Usage: add_context_param "contextKey" "${ENV_VAR_NAME}"
# Only adds --context if the environment variable is set and non-empty
add_context_param() {
    local context_key="$1"
    local env_var_value="$2"
    
    # Only output context parameter if value is set and non-empty
    if [ -n "${env_var_value}" ]; then
        echo "--context ${context_key}=\"${env_var_value}\""
    fi
}

# Helper function to build all context parameters for CDK commands
# Returns a string of --context parameters for required and optional configs
# Only includes optional parameters if their environment variables are set
build_cdk_context_params() {
    local context_params=""
    
    # Required parameters - always include (will fail validation if empty)
    context_params="${context_params} --context environment=\"${DEPLOY_ENVIRONMENT}\""
    context_params="${context_params} --context projectPrefix=\"${CDK_PROJECT_PREFIX}\""
    context_params="${context_params} --context awsAccount=\"${CDK_AWS_ACCOUNT}\""
    context_params="${context_params} --context awsRegion=\"${CDK_AWS_REGION}\""
    
    # Optional parameters - only include if set
    if [ -n "${CDK_VPC_CIDR:-}" ]; then
        context_params="${context_params} --context vpcCidr=\"${CDK_VPC_CIDR}\""
    fi
    
    if [ -n "${CDK_HOSTED_ZONE_DOMAIN:-}" ]; then
        context_params="${context_params} --context infrastructureHostedZoneDomain=\"${CDK_HOSTED_ZONE_DOMAIN}\""
    fi
    
    if [ -n "${CDK_ALB_SUBDOMAIN:-}" ]; then
        context_params="${context_params} --context albSubdomain=\"${CDK_ALB_SUBDOMAIN}\""
    fi
    
    # App API optional parameters
    if [ -n "${CDK_APP_API_ENABLED:-}" ]; then
        context_params="${context_params} --context appApi.enabled=\"${CDK_APP_API_ENABLED}\""
    fi
    if [ -n "${CDK_APP_API_CPU:-}" ]; then
        context_params="${context_params} --context appApi.cpu=\"${CDK_APP_API_CPU}\""
    fi
    if [ -n "${CDK_APP_API_MEMORY:-}" ]; then
        context_params="${context_params} --context appApi.memory=\"${CDK_APP_API_MEMORY}\""
    fi
    if [ -n "${CDK_APP_API_DESIRED_COUNT:-}" ]; then
        context_params="${context_params} --context appApi.desiredCount=\"${CDK_APP_API_DESIRED_COUNT}\""
    fi
    if [ -n "${CDK_APP_API_MAX_CAPACITY:-}" ]; then
        context_params="${context_params} --context appApi.maxCapacity=\"${CDK_APP_API_MAX_CAPACITY}\""
    fi
    
    # Inference API optional parameters
    if [ -n "${CDK_INFERENCE_API_ENABLED:-}" ]; then
        context_params="${context_params} --context inferenceApi.enabled=\"${CDK_INFERENCE_API_ENABLED}\""
    fi
    if [ -n "${CDK_INFERENCE_API_CPU:-}" ]; then
        context_params="${context_params} --context inferenceApi.cpu=\"${CDK_INFERENCE_API_CPU}\""
    fi
    if [ -n "${CDK_INFERENCE_API_MEMORY:-}" ]; then
        context_params="${context_params} --context inferenceApi.memory=\"${CDK_INFERENCE_API_MEMORY}\""
    fi
    if [ -n "${CDK_INFERENCE_API_DESIRED_COUNT:-}" ]; then
        context_params="${context_params} --context inferenceApi.desiredCount=\"${CDK_INFERENCE_API_DESIRED_COUNT}\""
    fi
    if [ -n "${CDK_INFERENCE_API_MAX_CAPACITY:-}" ]; then
        context_params="${context_params} --context inferenceApi.maxCapacity=\"${CDK_INFERENCE_API_MAX_CAPACITY}\""
    fi
    if [ -n "${CDK_INFERENCE_API_ENABLE_GPU:-}" ]; then
        context_params="${context_params} --context inferenceApi.enableGpu=\"${CDK_INFERENCE_API_ENABLE_GPU}\""
    fi
    
    # Inference API environment variables
    if [ -n "${ENV_INFERENCE_API_ENABLE_AUTHENTICATION:-}" ]; then
        context_params="${context_params} --context inferenceApi.enableAuthentication=\"${ENV_INFERENCE_API_ENABLE_AUTHENTICATION}\""
    fi
    if [ -n "${ENV_INFERENCE_API_LOG_LEVEL:-}" ]; then
        context_params="${context_params} --context inferenceApi.logLevel=\"${ENV_INFERENCE_API_LOG_LEVEL}\""
    fi
    if [ -n "${ENV_INFERENCE_API_UPLOAD_DIR:-}" ]; then
        context_params="${context_params} --context inferenceApi.uploadDir=\"${ENV_INFERENCE_API_UPLOAD_DIR}\""
    fi
    if [ -n "${ENV_INFERENCE_API_OUTPUT_DIR:-}" ]; then
        context_params="${context_params} --context inferenceApi.outputDir=\"${ENV_INFERENCE_API_OUTPUT_DIR}\""
    fi
    if [ -n "${ENV_INFERENCE_API_GENERATED_IMAGES_DIR:-}" ]; then
        context_params="${context_params} --context inferenceApi.generatedImagesDir=\"${ENV_INFERENCE_API_GENERATED_IMAGES_DIR}\""
    fi
    if [ -n "${ENV_INFERENCE_API_API_URL:-}" ]; then
        context_params="${context_params} --context inferenceApi.apiUrl=\"${ENV_INFERENCE_API_API_URL}\""
    fi
    if [ -n "${ENV_INFERENCE_API_FRONTEND_URL:-}" ]; then
        context_params="${context_params} --context inferenceApi.frontendUrl=\"${ENV_INFERENCE_API_FRONTEND_URL}\""
    fi
    if [ -n "${ENV_INFERENCE_API_CORS_ORIGINS:-}" ]; then
        context_params="${context_params} --context inferenceApi.corsOrigins=\"${ENV_INFERENCE_API_CORS_ORIGINS}\""
    fi
    if [ -n "${ENV_INFERENCE_API_TAVILY_API_KEY:-}" ]; then
        context_params="${context_params} --context inferenceApi.tavilyApiKey=\"${ENV_INFERENCE_API_TAVILY_API_KEY}\""
    fi
    if [ -n "${ENV_INFERENCE_API_NOVA_ACT_API_KEY:-}" ]; then
        context_params="${context_params} --context inferenceApi.novaActApiKey=\"${ENV_INFERENCE_API_NOVA_ACT_API_KEY}\""
    fi
    
    # Gateway optional parameters
    if [ -n "${CDK_GATEWAY_ENABLED:-}" ]; then
        context_params="${context_params} --context gateway.enabled=\"${CDK_GATEWAY_ENABLED}\""
    fi
    if [ -n "${CDK_GATEWAY_API_TYPE:-}" ]; then
        context_params="${context_params} --context gateway.apiType=\"${CDK_GATEWAY_API_TYPE}\""
    fi
    if [ -n "${CDK_GATEWAY_THROTTLE_RATE_LIMIT:-}" ]; then
        context_params="${context_params} --context gateway.throttleRateLimit=\"${CDK_GATEWAY_THROTTLE_RATE_LIMIT}\""
    fi
    if [ -n "${CDK_GATEWAY_THROTTLE_BURST_LIMIT:-}" ]; then
        context_params="${context_params} --context gateway.throttleBurstLimit=\"${CDK_GATEWAY_THROTTLE_BURST_LIMIT}\""
    fi
    if [ -n "${CDK_GATEWAY_ENABLE_WAF:-}" ]; then
        context_params="${context_params} --context gateway.enableWaf=\"${CDK_GATEWAY_ENABLE_WAF}\""
    fi
    if [ -n "${CDK_GATEWAY_LOG_LEVEL:-}" ]; then
        context_params="${context_params} --context gateway.logLevel=\"${CDK_GATEWAY_LOG_LEVEL}\""
    fi
    
    # Frontend optional parameters
    if [ -n "${CDK_FRONTEND_DOMAIN_NAME:-}" ]; then
        context_params="${context_params} --context frontend.domainName=\"${CDK_FRONTEND_DOMAIN_NAME}\""
    fi
    if [ -n "${CDK_FRONTEND_ENABLE_ROUTE53:-}" ]; then
        context_params="${context_params} --context frontend.enableRoute53=\"${CDK_FRONTEND_ENABLE_ROUTE53}\""
    fi
    if [ -n "${CDK_FRONTEND_CERTIFICATE_ARN:-}" ]; then
        context_params="${context_params} --context frontend.certificateArn=\"${CDK_FRONTEND_CERTIFICATE_ARN}\""
    fi
    if [ -n "${CDK_FRONTEND_ENABLED:-}" ]; then
        context_params="${context_params} --context frontend.enabled=\"${CDK_FRONTEND_ENABLED}\""
    fi
    if [ -n "${CDK_FRONTEND_BUCKET_NAME:-}" ]; then
        context_params="${context_params} --context frontend.bucketName=\"${CDK_FRONTEND_BUCKET_NAME}\""
    fi
    if [ -n "${CDK_FRONTEND_CLOUDFRONT_PRICE_CLASS:-}" ]; then
        context_params="${context_params} --context frontend.cloudFrontPriceClass=\"${CDK_FRONTEND_CLOUDFRONT_PRICE_CLASS}\""
    fi
    
    echo "${context_params}"
}


# Default to 'prod' environment if not set
export DEPLOY_ENVIRONMENT="${DEPLOY_ENVIRONMENT:-prod}"

# Export core configuration
# Priority: Environment variables > cdk.context.json
export CDK_AWS_REGION="${CDK_AWS_REGION:-$(get_json_value "awsRegion" "${CONTEXT_FILE}")}"
export CDK_PROJECT_PREFIX="${CDK_PROJECT_PREFIX:-$(get_json_value "projectPrefix" "${CONTEXT_FILE}")}"
export CDK_VPC_CIDR="${CDK_VPC_CIDR:-$(get_json_value "vpcCidr" "${CONTEXT_FILE}")}"
export CDK_HOSTED_ZONE_DOMAIN="${CDK_HOSTED_ZONE_DOMAIN:-$(get_json_value "infrastructureHostedZoneDomain" "${CONTEXT_FILE}")}"
export CDK_ALB_SUBDOMAIN="${CDK_ALB_SUBDOMAIN:-$(get_json_value "albSubdomain" "${CONTEXT_FILE}")}"

# AWS Account - try multiple sources (env vars take precedence)
CDK_CONTEXT_ACCOUNT=$(get_json_value "awsAccount" "${CONTEXT_FILE}")
export CDK_AWS_ACCOUNT="${CDK_AWS_ACCOUNT:-${CDK_CONTEXT_ACCOUNT:-${CDK_DEFAULT_ACCOUNT:-${AWS_ACCOUNT_ID:-}}}}"

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
log_info "  Environment:    ${DEPLOY_ENVIRONMENT}"
log_info "  Project Prefix: ${CDK_PROJECT_PREFIX}"
log_info "  AWS Account:    ${CDK_AWS_ACCOUNT}"
log_info "  AWS Region:     ${CDK_AWS_REGION}"
log_info "  VPC CIDR:       ${CDK_VPC_CIDR}"

if [ -n "${CDK_HOSTED_ZONE_DOMAIN:-}" ]; then
    log_info "  Hosted Zone:    ${CDK_HOSTED_ZONE_DOMAIN}"
fi

if [ -n "${CDK_ALB_SUBDOMAIN:-}" ]; then
    log_info "  ALB Subdomain:  ${CDK_ALB_SUBDOMAIN}.${CDK_HOSTED_ZONE_DOMAIN}"
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
