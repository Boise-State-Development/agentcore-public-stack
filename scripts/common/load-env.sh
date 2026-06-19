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
BLUE='\033[0;34m'
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

log_config() {
    echo -e "${BLUE}[CONFIG]${NC} $1"
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
    context_params="${context_params} --context projectPrefix=\"${CDK_PROJECT_PREFIX}\""
    context_params="${context_params} --context awsAccount=\"${CDK_AWS_ACCOUNT}\""
    context_params="${context_params} --context awsRegion=\"${CDK_AWS_REGION}\""
    context_params="${context_params} --context appVersion=\"${CDK_APP_VERSION}\""
    
    # Optional parameters - only include if set
    if [ -n "${CDK_PRODUCTION:-}" ]; then
        context_params="${context_params} --context production=\"${CDK_PRODUCTION}\""
    fi
    
    if [ -n "${CDK_VPC_CIDR:-}" ]; then
        context_params="${context_params} --context vpcCidr=\"${CDK_VPC_CIDR}\""
    fi
    
    if [ -n "${CDK_HOSTED_ZONE_DOMAIN:-}" ]; then
        context_params="${context_params} --context infrastructureHostedZoneDomain=\"${CDK_HOSTED_ZONE_DOMAIN}\""
    fi
    
    if [ -n "${CDK_ALB_SUBDOMAIN:-}" ]; then
        context_params="${context_params} --context albSubdomain=\"${CDK_ALB_SUBDOMAIN}\""
    fi
    
    if [ -n "${CDK_CERTIFICATE_ARN:-}" ]; then
        context_params="${context_params} --context certificateArn=\"${CDK_CERTIFICATE_ARN}\""
    fi

    # Shared CloudFront certificate — fallback for the SPA, artifacts, and
    # mcp-sandbox origins when their section-specific ARN is unset (config.ts
    # resolves the precedence). A single us-east-1 wildcard ({domain}+*.{domain})
    # set here covers all three CloudFront origins.
    if [ -n "${CDK_CLOUDFRONT_CERTIFICATE_ARN:-}" ]; then
        context_params="${context_params} --context cloudfrontCertificateArn=\"${CDK_CLOUDFRONT_CERTIFICATE_ARN}\""
    fi
    
    if [ -n "${CDK_CORS_ORIGINS:-}" ]; then
        context_params="${context_params} --context corsOrigins=\"${CDK_CORS_ORIGINS}\""
    fi
    
    # App API optional parameters
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
    if [ -n "${CDK_INFERENCE_API_CORS_ORIGINS:-}" ]; then
        context_params="${context_params} --context inferenceApi.additionalCorsOrigins=\"${CDK_INFERENCE_API_CORS_ORIGINS}\""
    fi

    # Domain name — top-level context key (used by config.ts as config.domainName)
    if [ -n "${CDK_DOMAIN_NAME:-}" ]; then
        context_params="${context_params} --context domainName=\"${CDK_DOMAIN_NAME}\""
    fi
    if [ -n "${CDK_FRONTEND_CERTIFICATE_ARN:-}" ]; then
        context_params="${context_params} --context frontend.certificateArn=\"${CDK_FRONTEND_CERTIFICATE_ARN}\""
    fi
    if [ -n "${CDK_FRONTEND_BUCKET_NAME:-}" ]; then
        context_params="${context_params} --context frontend.bucketName=\"${CDK_FRONTEND_BUCKET_NAME}\""
    fi
    if [ -n "${CDK_FRONTEND_CLOUDFRONT_PRICE_CLASS:-}" ]; then
        context_params="${context_params} --context frontend.cloudFrontPriceClass=\"${CDK_FRONTEND_CLOUDFRONT_PRICE_CLASS}\""
    fi

    # RAG Ingestion optional parameters
    if [ -n "${CDK_RAG_LAMBDA_MEMORY:-}" ]; then
        context_params="${context_params} --context ragIngestion.lambdaMemorySize=\"${CDK_RAG_LAMBDA_MEMORY}\""
    fi
    if [ -n "${CDK_RAG_LAMBDA_TIMEOUT:-}" ]; then
        context_params="${context_params} --context ragIngestion.lambdaTimeout=\"${CDK_RAG_LAMBDA_TIMEOUT}\""
    fi

    # Artifacts optional parameters
    if [ -n "${CDK_ARTIFACTS_CERTIFICATE_ARN:-}" ]; then
        context_params="${context_params} --context artifacts.certificateArn=\"${CDK_ARTIFACTS_CERTIFICATE_ARN}\""
    fi
    if [ -n "${CDK_ARTIFACTS_RETENTION_DAYS:-}" ]; then
        context_params="${context_params} --context artifacts.retentionDays=\"${CDK_ARTIFACTS_RETENTION_DAYS}\""
    fi

    # MCP sandbox optional parameters. (extraFrameAncestors is an array — it is
    # NOT forwarded as a --context string here; supply it via the
    # CDK_MCP_SANDBOX_EXTRA_FRAME_ANCESTORS env var, which config.ts splits, or
    # as a real array in cdk.context.json. Same applies to the artifacts
    # extraFrameAncestors above.)
    if [ -n "${CDK_MCP_SANDBOX_CERTIFICATE_ARN:-}" ]; then
        context_params="${context_params} --context mcpSandbox.certificateArn=\"${CDK_MCP_SANDBOX_CERTIFICATE_ARN}\""
    fi

    echo "${context_params}"
}

# Validate required CDK_* variables
validate_required_vars() {
    local errors=0
    
    if [ -z "${CDK_PROJECT_PREFIX:-}" ]; then
        log_error "CDK_PROJECT_PREFIX is required"
        log_error "  Set this environment variable to your desired resource name prefix"
        log_error "  Example: export CDK_PROJECT_PREFIX='mycompany-agentcore'"
        errors=$((errors + 1))
    fi
    
    if [ -z "${CDK_AWS_ACCOUNT:-}" ]; then
        log_error "CDK_AWS_ACCOUNT is required"
        log_error "  Set this to your 12-digit AWS account ID"
        log_error "  Example: export CDK_AWS_ACCOUNT='123456789012'"
        errors=$((errors + 1))
    fi
    
    if [ -z "${CDK_AWS_REGION:-}" ]; then
        log_error "CDK_AWS_REGION is required"
        log_error "  Set this to your target AWS region"
        log_error "  Example: export CDK_AWS_REGION='us-west-2'"
        errors=$((errors + 1))
    fi
    
    if [ $errors -gt 0 ]; then
        log_error "Configuration validation failed with ${errors} error(s)"
        return 1
    fi
    
    return 0
}

# Export app version from VERSION file (priority: env var > VERSION file)
export CDK_APP_VERSION="${CDK_APP_VERSION:-$(tr -d '[:space:]' < "${REPO_ROOT}/VERSION" 2>/dev/null || echo 'unknown')}"

# Export core configuration with defaults
# Priority: Environment variables > cdk.context.json > defaults
export CDK_PROJECT_PREFIX="${CDK_PROJECT_PREFIX:-$(get_json_value "projectPrefix" "${CONTEXT_FILE}")}"
export CDK_AWS_REGION="${CDK_AWS_REGION:-$(get_json_value "awsRegion" "${CONTEXT_FILE}")}"
export CDK_PRODUCTION="${CDK_PRODUCTION:-$(get_json_value "production" "${CONTEXT_FILE}")}"
export CDK_VPC_CIDR="${CDK_VPC_CIDR:-$(get_json_value "vpcCidr" "${CONTEXT_FILE}")}"
export CDK_HOSTED_ZONE_DOMAIN="${CDK_HOSTED_ZONE_DOMAIN:-$(get_json_value "infrastructureHostedZoneDomain" "${CONTEXT_FILE}")}"
export CDK_ALB_SUBDOMAIN="${CDK_ALB_SUBDOMAIN:-$(get_json_value "albSubdomain" "${CONTEXT_FILE}")}"
export CDK_CERTIFICATE_ARN="${CDK_CERTIFICATE_ARN:-$(get_json_value "certificateArn" "${CONTEXT_FILE}")}"

# Behavior flags — env var > context file (no hardcoded defaults)
export CDK_RETAIN_DATA_ON_DELETE="${CDK_RETAIN_DATA_ON_DELETE:-$(get_json_value "retainDataOnDelete" "${CONTEXT_FILE}")}"

# Shared CORS origins — env var > context file (no hardcoded defaults)
export CDK_CORS_ORIGINS="${CDK_CORS_ORIGINS:-$(get_json_value "corsOrigins" "${CONTEXT_FILE}")}"

# File upload configuration — env var > context file (no hardcoded defaults)
export CDK_FILE_UPLOAD_MAX_SIZE_MB="${CDK_FILE_UPLOAD_MAX_SIZE_MB:-$(get_json_value "fileUpload.maxFileSizeBytes" "${CONTEXT_FILE}")}"

# RAG Ingestion configuration
export CDK_RAG_LAMBDA_MEMORY="${CDK_RAG_LAMBDA_MEMORY:-$(get_json_value "ragIngestion.lambdaMemorySize" "${CONTEXT_FILE}")}"
export CDK_RAG_LAMBDA_TIMEOUT="${CDK_RAG_LAMBDA_TIMEOUT:-$(get_json_value "ragIngestion.lambdaTimeout" "${CONTEXT_FILE}")}"

# Artifacts configuration
export CDK_ARTIFACTS_CERTIFICATE_ARN="${CDK_ARTIFACTS_CERTIFICATE_ARN:-$(get_json_value "artifacts.certificateArn" "${CONTEXT_FILE}")}"
export CDK_ARTIFACTS_RETENTION_DAYS="${CDK_ARTIFACTS_RETENTION_DAYS:-$(get_json_value "artifacts.retentionDays" "${CONTEXT_FILE}")}"

# Cognito configuration (optional — defaults to projectPrefix for domain prefix)
export CDK_COGNITO_DOMAIN_PREFIX="${CDK_COGNITO_DOMAIN_PREFIX:-$(get_json_value "cognito.domainPrefix" "${CONTEXT_FILE}")}"

# AWS Account - try multiple sources (env vars take precedence)
CDK_CONTEXT_ACCOUNT=$(get_json_value "awsAccount" "${CONTEXT_FILE}")
export CDK_AWS_ACCOUNT="${CDK_AWS_ACCOUNT:-${CDK_CONTEXT_ACCOUNT:-${CDK_DEFAULT_ACCOUNT:-${AWS_ACCOUNT_ID:-}}}}"

# Set CDK environment variables for deployment
export CDK_DEFAULT_ACCOUNT="${CDK_AWS_ACCOUNT}"
export CDK_DEFAULT_REGION="${CDK_AWS_REGION}"

# Validate required configuration. Some callers (notably the
# frontend build, which produces a static Angular bundle and never
# touches AWS) can opt out by setting LOAD_ENV_SKIP_AWS_VALIDATION=1
# before sourcing this script.
if [ "${LOAD_ENV_SKIP_AWS_VALIDATION:-false}" != "true" ] && [ "${LOAD_ENV_SKIP_AWS_VALIDATION:-0}" != "1" ]; then
    if ! validate_required_vars; then
        return 1 2>/dev/null || exit 1
    fi
fi

# Validate configuration
validate_config() {
    local errors=0
    
    # Validate AWS Account ID format (12 digits)
    if [ -n "${CDK_AWS_ACCOUNT}" ] && ! [[ "${CDK_AWS_ACCOUNT}" =~ ^[0-9]{12}$ ]]; then
        log_error "Invalid AWS account ID: '${CDK_AWS_ACCOUNT}'"
        log_error "  Expected a 12-digit number"
        errors=$((errors + 1))
    fi
    
    # Validate boolean flags
    if [ -n "${CDK_RETAIN_DATA_ON_DELETE}" ] && ! [[ "${CDK_RETAIN_DATA_ON_DELETE}" =~ ^(true|false|1|0)$ ]]; then
        log_error "Invalid CDK_RETAIN_DATA_ON_DELETE value: '${CDK_RETAIN_DATA_ON_DELETE}'"
        log_error "  Expected 'true', 'false', '1', or '0'"
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

# Display loaded configuration (skip in quiet mode for CI noise reduction)
if [ "${LOAD_ENV_QUIET:-false}" != "true" ]; then
    log_info "📋 Configuration loaded successfully:"
    log_config "  Project Prefix: ${CDK_PROJECT_PREFIX}"
    log_config "  AWS Account:    ${CDK_AWS_ACCOUNT}"
    log_config "  AWS Region:     ${CDK_AWS_REGION}"
    log_config "  App Version:    ${CDK_APP_VERSION}"
    log_config "  Production:     ${CDK_PRODUCTION:-true}"
    log_config "  VPC CIDR:       ${CDK_VPC_CIDR:-<not set>}"
    log_config "  Retain Data:    ${CDK_RETAIN_DATA_ON_DELETE}"
    log_config "  CORS Origins:   ${CDK_CORS_ORIGINS}"

    if [ -n "${CDK_HOSTED_ZONE_DOMAIN:-}" ]; then
        log_config "  Hosted Zone:    ${CDK_HOSTED_ZONE_DOMAIN}"
    fi

    if [ -n "${CDK_ALB_SUBDOMAIN:-}" ]; then
        log_config "  ALB Subdomain:  ${CDK_ALB_SUBDOMAIN}.${CDK_HOSTED_ZONE_DOMAIN}"
    fi

    if [ -n "${CDK_CERTIFICATE_ARN:-}" ]; then
        log_config "  Certificate:    ${CDK_CERTIFICATE_ARN:0:50}..." # Truncate for display
        log_config "  HTTPS Enabled:  Yes"
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
            log_config "  AWS Identity:   ${CALLER_IDENTITY}"
        fi
    fi

    log_info "✅ Environment variables exported and ready for deployment"
else
    log_info "✅ Environment loaded (${CDK_PROJECT_PREFIX} / ${CDK_AWS_REGION} / v${CDK_APP_VERSION})"
fi
