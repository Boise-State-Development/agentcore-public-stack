#!/bin/bash

###############################################################################
# AWS CDK Multi-Stack Deployment Orchestration Script
# 
# Description: Full-pipeline local deployment orchestration (Build → Test → Deploy)
# Usage: ./deploy.sh [OPTIONS]
#
# Options:
#   --dry-run          Preview deployment without executing
#   --skip-tests       Skip test steps (faster deployment)
#   --continue-on-error Continue even if a step fails
#   --verbose, -v      Show full command output
#   --help, -h         Show help message
#
# Stacks:
#   1. Infrastructure Stack - VPC, ALB, ECS Cluster, Security Groups
#   2. App API Stack - Application API on Fargate
#   3. Inference API Stack - AgentCore Runtime with Memory & Tools
#   4. Gateway Stack - MCP Gateway with Lambda tools
#   5. Frontend Stack - S3 + CloudFront + Route53
#
###############################################################################

set -euo pipefail

# Detect project root
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PROJECT_ROOT

# Source environment configuration
source "${PROJECT_ROOT}/scripts/common/load-env.sh"

# Define logging functions locally
log_success() {
    echo -e "\033[0;32m✓ $1\033[0m"
}

log_warning() {
    echo -e "\033[1;33m⚠ $1\033[0m"
}

log_error() {
    echo -e "\033[0;31m✗ $1\033[0m"
}

log_info() {
    echo -e "\033[0;34mℹ $1\033[0m"
}

log_header() {
    echo ""
    echo "═══════════════════════════════════════════════════════════════"
    echo "  $1"
    echo "═══════════════════════════════════════════════════════════════"
    echo ""
}

debug_log() {
    if [ "$VERBOSE" = true ]; then
        echo "[DEBUG] $1" >&2
    fi
}

###############################################################################
# Interactive configuration prompts
###############################################################################
get_cdk_context_value() {
    local json_path=$1
    local cdk_context_file="${PROJECT_ROOT}/infrastructure/cdk.context.json"
    
    if [ -f "$cdk_context_file" ]; then
        # Use jq if available, otherwise fallback to grep/sed
        if command -v jq &> /dev/null; then
            jq -r "$json_path // empty" "$cdk_context_file" 2>/dev/null || echo ""
        else
            # Simple fallback for basic paths (won't work for nested objects)
            local key=$(echo "$json_path" | sed 's/^\.//; s/\./ /g')
            grep -o "\"${key}\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" "$cdk_context_file" | sed 's/.*: *"\(.*\)".*/\1/' || echo ""
        fi
    else
        echo ""
    fi
}

prompt_for_config() {
    local var_name=$1
    local prompt_text=$2
    local json_path=$3
    
    # First check environment variable
    local current_value="${!var_name:-}"
    
    # If not set, check cdk.context.json
    if [ -z "$current_value" ] && [ -n "$json_path" ]; then
        current_value=$(get_cdk_context_value "$json_path")
    fi
    
    if [ -n "$current_value" ]; then
        read -p "${prompt_text} [${current_value}]: " user_input
        if [ -n "$user_input" ]; then
            export "${var_name}=${user_input}"
        else
            # Keep the current value (from env or cdk.context.json)
            export "${var_name}=${current_value}"
        fi
    else
        read -p "${prompt_text} [empty]: " user_input
        if [ -n "$user_input" ]; then
            export "${var_name}=${user_input}"
        fi
    fi
}

configure_infrastructure() {
    log_header "Configure Infrastructure Stack"
    
    echo "Configure Infrastructure settings (press Enter to keep current/default values):"
    echo ""
    
    prompt_for_config "CDK_PROJECT_PREFIX" "Project Prefix (lowercase, alphanumeric, 2-21 chars)" ".projectPrefix"
    prompt_for_config "CDK_AWS_ACCOUNT" "AWS Account ID" ".awsAccount"
    prompt_for_config "CDK_AWS_REGION" "AWS Region" ".awsRegion"
    prompt_for_config "CDK_VPC_CIDR" "VPC CIDR Block" ".vpcCidr"
    prompt_for_config "CDK_HOSTED_ZONE_DOMAIN" "Hosted Zone Domain (optional)" ".infrastructureHostedZoneDomain"
    
    echo ""
    log_success "Configuration updated"
    echo ""
}

configure_app_api() {
    log_header "Configure App API Stack"
    
    echo "Configure App API settings (press Enter to keep current/default values):"
    echo ""
    
    prompt_for_config "CDK_APP_API_ENABLED" "Enable App API Stack (true/false)" ".appApi.enabled"
    prompt_for_config "CDK_APP_API_CPU" "CPU units (256, 512, 1024, 2048, 4096)" ".appApi.cpu"
    prompt_for_config "CDK_APP_API_MEMORY" "Memory in MB (512, 1024, 2048, 4096, 8192)" ".appApi.memory"
    prompt_for_config "CDK_APP_API_DESIRED_COUNT" "Desired task count" ".appApi.desiredCount"
    prompt_for_config "CDK_APP_API_MAX_CAPACITY" "Max auto-scaling capacity" ".appApi.maxCapacity"
    
    echo ""
    log_success "Configuration updated"
    echo ""
}

configure_inference_api() {
    log_header "Configure Inference API Stack"
    
    echo "Configure Inference API (AgentCore Runtime) settings (press Enter to keep current/default values):"
    echo ""
    
    prompt_for_config "CDK_INFERENCE_API_ENABLED" "Enable Inference API Stack (true/false)" ".inferenceApi.enabled"
    prompt_for_config "CDK_INFERENCE_API_CPU" "CPU units (256, 512, 1024, 2048, 4096)" ".inferenceApi.cpu"
    prompt_for_config "CDK_INFERENCE_API_MEMORY" "Memory in MB (512, 1024, 2048, 4096, 8192)" ".inferenceApi.memory"
    prompt_for_config "CDK_INFERENCE_API_ENABLE_GPU" "Enable GPU support (true/false)" ".inferenceApi.enableGpu"
    prompt_for_config "ENV_INFERENCE_API_LOG_LEVEL" "Log level (DEBUG, INFO, WARNING, ERROR)" ".inferenceApi.logLevel"
    prompt_for_config "ENV_INFERENCE_API_ENABLE_AUTHENTICATION" "Enable authentication (true/false)" ".inferenceApi.enableAuthentication"
    prompt_for_config "ENV_INFERENCE_API_TAVILY_API_KEY" "Tavily API Key (optional)" ".inferenceApi.tavilyApiKey"
    prompt_for_config "ENV_INFERENCE_API_NOVA_ACT_API_KEY" "Nova Act API Key (optional)" ".inferenceApi.novaActApiKey"
    
    echo ""
    log_success "Configuration updated"
    echo ""
}

configure_gateway() {
    log_header "Configure Gateway Stack"
    
    echo "Configure MCP Gateway settings (press Enter to keep current/default values):"
    echo ""
    
    prompt_for_config "CDK_GATEWAY_ENABLED" "Enable Gateway Stack (true/false)" ".gateway.enabled"
    prompt_for_config "CDK_GATEWAY_API_TYPE" "API Type (REST/HTTP)" ".gateway.apiType"
    prompt_for_config "CDK_GATEWAY_THROTTLE_RATE_LIMIT" "Throttle rate limit (requests/second)" ".gateway.throttleRateLimit"
    prompt_for_config "CDK_GATEWAY_THROTTLE_BURST_LIMIT" "Throttle burst limit" ".gateway.throttleBurstLimit"
    prompt_for_config "CDK_GATEWAY_ENABLE_WAF" "Enable WAF protection (true/false)" ".gateway.enableWaf"
    prompt_for_config "CDK_GATEWAY_LOG_LEVEL" "Log level (INFO, DEBUG, ERROR)" ".gateway.logLevel"
    
    echo ""
    log_success "Configuration updated"
    echo ""
}

configure_frontend() {
    log_header "Configure Frontend Stack"
    
    echo "Configure Frontend (Angular + S3 + CloudFront) settings (press Enter to keep current/default values):"
    echo ""
    
    prompt_for_config "CDK_FRONTEND_ENABLED" "Enable Frontend Stack (true/false)" ".frontend.enabled"
    prompt_for_config "CDK_FRONTEND_DOMAIN_NAME" "Custom domain name (optional)" ".frontend.domainName"
    prompt_for_config "CDK_FRONTEND_ENABLE_ROUTE53" "Enable Route53 integration (true/false)" ".frontend.enableRoute53"
    prompt_for_config "CDK_FRONTEND_CERTIFICATE_ARN" "ACM Certificate ARN (optional)" ".frontend.certificateArn"
    prompt_for_config "CDK_FRONTEND_BUCKET_NAME" "S3 bucket name (optional, auto-generated if empty)" ".frontend.bucketName"
    prompt_for_config "CDK_FRONTEND_CLOUDFRONT_PRICE_CLASS" "CloudFront price class (PriceClass_100/PriceClass_200/PriceClass_All)" ".frontend.cloudFrontPriceClass"
    
    echo ""
    log_success "Configuration updated"
    echo ""
}

# Global dry-run flag
DRY_RUN=false
SKIP_TESTS=false
CONTINUE_ON_ERROR=false
VERBOSE=false

# Timing variables
TOTAL_START_TIME=0
STACK_START_TIME=0

# Deployment results tracking
declare -A DEPLOYMENT_RESULTS
declare -A DEPLOYMENT_URLS

###############################################################################
# ASCII Art Banner
###############################################################################
show_banner() {
    clear
    cat << 'EOF'
╔═══════════════════════════════════════════════════════════════════════════╗
║                                                                           ║
║     █████╗  ██████╗ ███████╗███╗   ██╗████████╗ ██████╗ ██████╗ ██████╗   ║
║    ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝██╔════╝██╔═══██╗██╔══██╗  ║
║    ███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║   ██║     ██║   ██║██████╔╝  ║
║    ██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║   ██║     ██║   ██║██╔══██╗  ║
║    ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║   ╚██████╗╚██████╔╝██║  ██║  ║
║    ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝    ╚═════╝ ╚═════╝ ╚═╝  ╚═╝  ║
║                                                                           ║
║           AWS CDK Multi-Stack Deployment Orchestration v2.0               ║
║                  Full Build → Test → Deploy Pipeline                      ║
║                                                                           ║
╚═══════════════════════════════════════════════════════════════════════════╝

EOF
}

###############################################################################
# Parse command-line arguments
###############################################################################
parse_arguments() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --dry-run)
                DRY_RUN=true
                log_warning "DRY-RUN MODE: Commands will be displayed but not executed"
                shift
                ;;
            --skip-tests)
                SKIP_TESTS=true
                log_warning "SKIP-TESTS MODE: Test steps will be skipped"
                shift
                ;;
            --continue-on-error)
                CONTINUE_ON_ERROR=true
                log_warning "CONTINUE-ON-ERROR MODE: Will continue even if steps fail"
                shift
                ;;
            -v|--verbose)
                VERBOSE=true
                log_info "VERBOSE MODE: Full command output will be shown"
                shift
                ;;
            -h|--help)
                show_usage
                exit 0
                ;;
            *)
                log_error "Unknown option: $1"
                show_usage
                exit 1
                ;;
        esac
    done
}

###############################################################################
# Show usage information
###############################################################################
show_usage() {
    cat << EOF
Usage: ./deploy.sh [OPTIONS]

Full-pipeline deployment orchestration for AWS CDK stacks.
Runs: Install → Build → Test → Synth → Deploy (mimicking GitHub Actions)

OPTIONS:
    --dry-run              Show what would be deployed without executing
    --skip-tests           Skip test steps (faster deployment)
    --continue-on-error    Continue even if a step fails
    -v, --verbose          Show full command output
    -h, --help             Show this help message

STACKS:
    1. Infrastructure Stack - Foundation layer (VPC, ALB, ECS Cluster)
    2. App API Stack        - Application API on Fargate
    3. Inference API Stack  - AgentCore Runtime with Memory & Tools
    4. Gateway Stack        - MCP Gateway with Lambda tools
    5. Frontend Stack       - S3 + CloudFront + Route53

EXAMPLES:
    ./deploy.sh                     # Interactive menu with full pipeline
    ./deploy.sh --dry-run           # Preview without executing
    ./deploy.sh --skip-tests        # Skip test steps for faster deployment
    ./deploy.sh --verbose           # Show full command output

PIPELINE STEPS PER STACK:
    Infrastructure: install-deps → install → build → test → synth → deploy
    App API:        install-deps → install → build-docker → test-docker → 
                    test → build-cdk → synth → test-cdk → push-ecr → deploy
    Inference API:  install-deps → install → build-docker → test-docker → 
                    test → build-cdk → synth → test-cdk → push-ecr → deploy
    Gateway:        install-deps → install → build-cdk → synth → 
                    test-cdk → deploy → test
    Frontend:       install-deps → install → build → test → build-cdk → 
                    synth → test-cdk → deploy-cdk → deploy-assets

EOF
}

###############################################################################
# Environment validation
###############################################################################
validate_environment() {
    log_header "Validating Environment"
    
    local errors=0
    
    # Check required environment variables
    if [ -z "${CDK_AWS_ACCOUNT:-}" ]; then
        log_error "CDK_AWS_ACCOUNT is not set"
        ((errors++))
    else
        log_success "CDK_AWS_ACCOUNT: ${CDK_AWS_ACCOUNT}"
    fi
    
    if [ -z "${CDK_AWS_REGION:-}" ]; then
        log_error "CDK_AWS_REGION is not set"
        ((errors++))
    else
        log_success "CDK_AWS_REGION: ${CDK_AWS_REGION}"
    fi
    
    if [ -z "${CDK_PROJECT_PREFIX:-}" ]; then
        log_error "CDK_PROJECT_PREFIX is not set"
        ((errors++))
    else
        log_success "CDK_PROJECT_PREFIX: ${CDK_PROJECT_PREFIX}"
    fi
    
    # Check AWS credentials
    if ! aws sts get-caller-identity &>/dev/null; then
        log_error "AWS credentials are not configured or invalid"
        log_info "Run 'aws configure' or set AWS environment variables"
        ((errors++))
    else
        local caller_identity=$(aws sts get-caller-identity --query 'Arn' --output text 2>/dev/null || echo "Unknown")
        log_success "AWS credentials valid: ${caller_identity}"
    fi
    
    # Check if CDK is installed (optional - install-deps.sh will install it)
    if ! command -v cdk &>/dev/null; then
        log_warning "AWS CDK CLI not found (will be installed by install-deps.sh)"
    else
        local cdk_version=$(cdk --version 2>/dev/null || echo "Unknown")
        log_success "AWS CDK installed: ${cdk_version}"
    fi
    
    # Check if required directories exist
    if [ ! -d "${PROJECT_ROOT}/infrastructure" ]; then
        log_error "Infrastructure directory not found: ${PROJECT_ROOT}/infrastructure"
        ((errors++))
    else
        log_success "Infrastructure directory found"
    fi
    
    if [ ! -d "${PROJECT_ROOT}/scripts" ]; then
        log_error "Scripts directory not found: ${PROJECT_ROOT}/scripts"
        ((errors++))
    else
        log_success "Scripts directory found"
    fi
    
    if [ $errors -gt 0 ]; then
        log_error "Environment validation failed with ${errors} error(s)"
        return 1
    fi
    
    log_success "Environment validation passed"
    return 0
}

###############################################################################
# Execute command with dry-run support
###############################################################################
execute_command() {
    local description=$1
    shift
    local command=("$@")
    
    if [ "$DRY_RUN" = true ]; then
        log_info "[DRY-RUN] ${description}"
        log_info "[DRY-RUN] Command: ${command[*]}"
    else
        log_info "${description}"
        "${command[@]}"
    fi
}

###############################################################################
# Timing functions
###############################################################################
start_timer() {
    debug_log "start_timer called"
    local timestamp=$(date +%s)
    debug_log "timestamp=${timestamp}"
    echo "${timestamp}"
}

elapsed_time() {
    debug_log "elapsed_time called with start_time=$1"
    local start_time=$1
    local end_time=$(date +%s)
    debug_log "end_time=${end_time}"
    local elapsed=$((end_time - start_time))
    debug_log "elapsed=${elapsed}"
    
    local minutes=$((elapsed / 60))
    local seconds=$((elapsed % 60))
    
    if [ $minutes -gt 0 ]; then
        echo "${minutes}m ${seconds}s"
    else
        echo "${seconds}s"
    fi
}

###############################################################################
# Pipeline step execution with progress tracking
###############################################################################
execute_pipeline_step() {
    debug_log "=== execute_pipeline_step ENTRY ==="
    debug_log "Number of arguments: $#"
    debug_log "All arguments: $*"
    
    local step_num=$1
    local total_steps=$2
    local step_name=$3
    local script_path=$4
    
    debug_log "step_num=${step_num}"
    debug_log "total_steps=${total_steps}"
    debug_log "step_name=${step_name}"
    debug_log "script_path=${script_path}"
    
    log_header "Step ${step_num}/${total_steps}: ${step_name}"
    
    debug_log "About to call start_timer..."
    local step_start=$(start_timer)
    debug_log "step_start=${step_start}"
    
    if [ "$DRY_RUN" = true ]; then
        log_info "[DRY-RUN] Would execute: ${script_path}"
        debug_log "DRY_RUN mode, returning 0"
        return 0
    fi
    
    debug_log "Checking if script exists: ${script_path}"
    if [ ! -f "${script_path}" ]; then
        log_error "Script not found: ${script_path}"
        debug_log "Script does not exist!"
        if [ "$CONTINUE_ON_ERROR" = false ]; then
            debug_log "CONTINUE_ON_ERROR is false, returning 1"
            return 1
        else
            log_warning "Continuing despite error (--continue-on-error enabled)"
            debug_log "CONTINUE_ON_ERROR is true, returning 0"
            return 0
        fi
    fi
    
    debug_log "Script exists, about to execute..."
    # Execute script - always show script logs (stderr), optionally show debug output
    local exit_code=0
    bash "${script_path}" || exit_code=$?
    
    debug_log "Script execution completed with exit_code=${exit_code}"
    
    debug_log "Calculating elapsed time..."
    local step_elapsed=$(elapsed_time $step_start)
    debug_log "step_elapsed=${step_elapsed}"
    
    if [ $exit_code -eq 0 ]; then
        log_success "${step_name} completed in ${step_elapsed}"
        debug_log "Returning 0 (success)"
        return 0
    else
        log_error "${step_name} failed (exit code: ${exit_code})"
        if [ "$CONTINUE_ON_ERROR" = false ]; then
            debug_log "CONTINUE_ON_ERROR is false, returning 1"
            return 1
        else
            log_warning "Continuing despite error (--continue-on-error enabled)"
            debug_log "CONTINUE_ON_ERROR is true, returning 0"
            return 0
        fi
    fi
}

###############################################################################
# Stack deployment functions
###############################################################################

deploy_infrastructure() {
    # Prompt for configuration
    configure_infrastructure
    
    log_header "INFRASTRUCTURE STACK - Full Pipeline Deployment"
    
    debug_log "Starting deploy_infrastructure function"
    debug_log "PROJECT_ROOT=${PROJECT_ROOT}"
    debug_log "DRY_RUN=${DRY_RUN}"
    debug_log "SKIP_TESTS=${SKIP_TESTS}"
    
    debug_log "Calling start_timer..."
    STACK_START_TIME=$(start_timer)
    debug_log "start_timer returned: ${STACK_START_TIME}"
    
    local stack_name="Infrastructure"
    debug_log "Stack name set to: ${stack_name}"
    
    # Pipeline steps
    local total_steps=6
    local current_step=0
    debug_log "Initialized: total_steps=${total_steps}, current_step=${current_step}"
    
    # Step 1: Install system dependencies
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Install system dependencies (Node.js, CDK, Python, Docker)" \
        "${PROJECT_ROOT}/scripts/common/install-deps.sh" || return 1
    
    # Step 2: Install CDK dependencies
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Install CDK dependencies" \
        "${PROJECT_ROOT}/scripts/stack-infrastructure/install.sh" || return 1
    
    # Step 3: Build CDK (compile TypeScript)
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Build CDK stack (compile TypeScript)" \
        "${PROJECT_ROOT}/scripts/stack-infrastructure/build.sh" || return 1
    
    # Step 4: Synthesize CloudFormation
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Synthesize CloudFormation templates" \
        "${PROJECT_ROOT}/scripts/stack-infrastructure/synth.sh" || return 1
    
    # Step 5: Validate CloudFormation (test)
    if [ "$SKIP_TESTS" = false ]; then
        current_step=$((current_step + 1))
        execute_pipeline_step $current_step $total_steps \
            "Validate CloudFormation template" \
            "${PROJECT_ROOT}/scripts/stack-infrastructure/test.sh" || return 1
    else
        current_step=$((current_step + 1))
        log_warning "Step ${current_step}/${total_steps}: Tests skipped (--skip-tests enabled)"
    fi
    
    # Step 6: Deploy stack
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Deploy Infrastructure Stack to AWS" \
        "${PROJECT_ROOT}/scripts/stack-infrastructure/deploy.sh" || return 1
    
    local stack_elapsed=$(elapsed_time $STACK_START_TIME)
    log_success "Infrastructure Stack deployed successfully in ${stack_elapsed}"
    
    DEPLOYMENT_RESULTS[$stack_name]="SUCCESS"
    DEPLOYMENT_URLS[$stack_name]="VPC, ALB, ECS Cluster deployed"
    
    return 0
}

deploy_app_api() {
    # Prompt for configuration
    configure_app_api
    
    log_header "APP API STACK - Full Pipeline Deployment"
    
    STACK_START_TIME=$(start_timer)
    local stack_name="App API"
    
    # Pipeline steps
    local total_steps=10
    local current_step=0
    
    # Step 1: Install system dependencies
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Install system dependencies" \
        "${PROJECT_ROOT}/scripts/common/install-deps.sh" || return 1
    
    # Step 2: Install Python dependencies
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Install Python dependencies" \
        "${PROJECT_ROOT}/scripts/stack-app-api/install.sh" || return 1
    
    # Step 3: Build Docker image
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Build Docker image" \
        "${PROJECT_ROOT}/scripts/stack-app-api/build.sh" || return 1
    
    # Step 4: Test Docker container
    if [ "$SKIP_TESTS" = false ]; then
        current_step=$((current_step + 1))
        execute_pipeline_step $current_step $total_steps \
            "Test Docker container" \
            "${PROJECT_ROOT}/scripts/stack-app-api/test-docker.sh" || return 1
    else
        current_step=$((current_step + 1))
        log_warning "Step ${current_step}/${total_steps}: Docker tests skipped"
    fi
    
    # Step 5: Run Python unit tests
    if [ "$SKIP_TESTS" = false ]; then
        current_step=$((current_step + 1))
        execute_pipeline_step $current_step $total_steps \
            "Run Python unit tests" \
            "${PROJECT_ROOT}/scripts/stack-app-api/test.sh" || return 1
    else
        current_step=$((current_step + 1))
        log_warning "Step ${current_step}/${total_steps}: Unit tests skipped"
    fi
    
    # Step 6: Build CDK
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Build CDK stack (compile TypeScript)" \
        "${PROJECT_ROOT}/scripts/stack-app-api/build-cdk.sh" || return 1
    
    # Step 7: Synthesize CloudFormation
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Synthesize CloudFormation templates" \
        "${PROJECT_ROOT}/scripts/stack-app-api/synth.sh" || return 1
    
    # Step 8: Test CDK
    if [ "$SKIP_TESTS" = false ]; then
        current_step=$((current_step + 1))
        execute_pipeline_step $current_step $total_steps \
            "Validate CDK templates" \
            "${PROJECT_ROOT}/scripts/stack-app-api/test-cdk.sh" || return 1
    else
        current_step=$((current_step + 1))
        log_warning "Step ${current_step}/${total_steps}: CDK tests skipped"
    fi
    
    # Step 9: Push Docker image to ECR
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Push Docker image to ECR" \
        "${PROJECT_ROOT}/scripts/stack-app-api/push-to-ecr.sh" || return 1
    
    # Step 10: Deploy stack
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Deploy App API Stack to AWS" \
        "${PROJECT_ROOT}/scripts/stack-app-api/deploy.sh" || return 1
    
    local stack_elapsed=$(elapsed_time $STACK_START_TIME)
    log_success "App API Stack deployed successfully in ${stack_elapsed}"
    
    DEPLOYMENT_RESULTS[$stack_name]="SUCCESS"
    DEPLOYMENT_URLS[$stack_name]="Check ALB for App API endpoint"
    
    return 0
}

deploy_inference_api() {
    # Prompt for configuration
    configure_inference_api
    
    log_header "INFERENCE API STACK - Full Pipeline Deployment"
    
    STACK_START_TIME=$(start_timer)
    local stack_name="Inference API"
    
    # Pipeline steps
    local total_steps=10
    local current_step=0
    
    # Step 1: Install system dependencies
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Install system dependencies" \
        "${PROJECT_ROOT}/scripts/common/install-deps.sh" || return 1
    
    # Step 2: Install Python dependencies
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Install Python dependencies" \
        "${PROJECT_ROOT}/scripts/stack-inference-api/install.sh" || return 1
    
    # Step 3: Build ARM64 Docker image
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Build ARM64 Docker image (AgentCore Runtime)" \
        "${PROJECT_ROOT}/scripts/stack-inference-api/build.sh" || return 1
    
    # Step 4: Test Docker container
    if [ "$SKIP_TESTS" = false ]; then
        current_step=$((current_step + 1))
        execute_pipeline_step $current_step $total_steps \
            "Test ARM64 Docker container" \
            "${PROJECT_ROOT}/scripts/stack-inference-api/test-docker.sh" || return 1
    else
        current_step=$((current_step + 1))
        log_warning "Step ${current_step}/${total_steps}: Docker tests skipped"
    fi
    
    # Step 5: Run Python unit tests
    if [ "$SKIP_TESTS" = false ]; then
        current_step=$((current_step + 1))
        execute_pipeline_step $current_step $total_steps \
            "Run Python unit tests" \
            "${PROJECT_ROOT}/scripts/stack-inference-api/test.sh" || return 1
    else
        current_step=$((current_step + 1))
        log_warning "Step ${current_step}/${total_steps}: Unit tests skipped"
    fi
    
    # Step 6: Build CDK
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Build CDK stack (compile TypeScript)" \
        "${PROJECT_ROOT}/scripts/stack-inference-api/build-cdk.sh" || return 1
    
    # Step 7: Synthesize CloudFormation
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Synthesize CloudFormation templates" \
        "${PROJECT_ROOT}/scripts/stack-inference-api/synth.sh" || return 1
    
    # Step 8: Test CDK
    if [ "$SKIP_TESTS" = false ]; then
        current_step=$((current_step + 1))
        execute_pipeline_step $current_step $total_steps \
            "Validate CDK templates" \
            "${PROJECT_ROOT}/scripts/stack-inference-api/test-cdk.sh" || return 1
    else
        current_step=$((current_step + 1))
        log_warning "Step ${current_step}/${total_steps}: CDK tests skipped"
    fi
    
    # Step 9: Push Docker image to ECR
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Push ARM64 Docker image to ECR" \
        "${PROJECT_ROOT}/scripts/stack-inference-api/push-to-ecr.sh" || return 1
    
    # Step 10: Deploy stack
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Deploy Inference API Stack (AgentCore Runtime)" \
        "${PROJECT_ROOT}/scripts/stack-inference-api/deploy.sh" || return 1
    
    local stack_elapsed=$(elapsed_time $STACK_START_TIME)
    log_success "Inference API Stack deployed successfully in ${stack_elapsed}"
    
    DEPLOYMENT_RESULTS[$stack_name]="SUCCESS"
    DEPLOYMENT_URLS[$stack_name]="AgentCore Runtime endpoint available"
    
    return 0
}

deploy_gateway() {
    # Prompt for configuration
    configure_gateway
    
    log_header "GATEWAY STACK - Full Pipeline Deployment"
    
    STACK_START_TIME=$(start_timer)
    local stack_name="Gateway"
    
    # Pipeline steps
    local total_steps=7
    local current_step=0
    
    # Step 1: Install system dependencies
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Install system dependencies" \
        "${PROJECT_ROOT}/scripts/common/install-deps.sh" || return 1
    
    # Step 2: Install CDK dependencies
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Install CDK dependencies" \
        "${PROJECT_ROOT}/scripts/stack-gateway/install.sh" || return 1
    
    # Step 3: Build CDK
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Build CDK stack (compile TypeScript)" \
        "${PROJECT_ROOT}/scripts/stack-gateway/build-cdk.sh" || return 1
    
    # Step 4: Synthesize CloudFormation
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Synthesize CloudFormation (CDK packages Lambda automatically)" \
        "${PROJECT_ROOT}/scripts/stack-gateway/synth.sh" || return 1
    
    # Step 5: Test CDK
    if [ "$SKIP_TESTS" = false ]; then
        current_step=$((current_step + 1))
        execute_pipeline_step $current_step $total_steps \
            "Validate CDK templates" \
            "${PROJECT_ROOT}/scripts/stack-gateway/test-cdk.sh" || return 1
    else
        current_step=$((current_step + 1))
        log_warning "Step ${current_step}/${total_steps}: CDK tests skipped"
    fi
    
    # Step 6: Deploy stack
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Deploy Gateway Stack (MCP Gateway + Lambda)" \
        "${PROJECT_ROOT}/scripts/stack-gateway/deploy.sh" || return 1
    
    # Step 7: Test Gateway connectivity
    if [ "$SKIP_TESTS" = false ]; then
        current_step=$((current_step + 1))
        execute_pipeline_step $current_step $total_steps \
            "Test Gateway connectivity" \
            "${PROJECT_ROOT}/scripts/stack-gateway/test.sh" || return 1
    else
        current_step=$((current_step + 1))
        log_warning "Step ${current_step}/${total_steps}: Gateway tests skipped"
    fi
    
    local stack_elapsed=$(elapsed_time $STACK_START_TIME)
    log_success "Gateway Stack deployed successfully in ${stack_elapsed}"
    
    DEPLOYMENT_RESULTS[$stack_name]="SUCCESS"
    DEPLOYMENT_URLS[$stack_name]="MCP Gateway with Lambda tools deployed"
    
    return 0
}

deploy_frontend() {
    # Prompt for configuration
    configure_frontend
    
    log_header "FRONTEND STACK - Full Pipeline Deployment"
    
    STACK_START_TIME=$(start_timer)
    local stack_name="Frontend"
    
    # Pipeline steps
    local total_steps=9
    local current_step=0
    
    # Step 1: Install system dependencies
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Install system dependencies" \
        "${PROJECT_ROOT}/scripts/common/install-deps.sh" || return 1
    
    # Step 2: Install Angular dependencies
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Install Angular dependencies" \
        "${PROJECT_ROOT}/scripts/stack-frontend/install.sh" || return 1
    
    # Step 3: Build Angular application
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Build Angular application (production mode)" \
        "${PROJECT_ROOT}/scripts/stack-frontend/build.sh" || return 1
    
    # Step 4: Run Vitest tests
    if [ "$SKIP_TESTS" = false ]; then
        current_step=$((current_step + 1))
        execute_pipeline_step $current_step $total_steps \
            "Run Vitest tests" \
            "${PROJECT_ROOT}/scripts/stack-frontend/test.sh" || return 1
    else
        current_step=$((current_step + 1))
        log_warning "Step ${current_step}/${total_steps}: Tests skipped"
    fi
    
    # Step 5: Build CDK
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Build CDK stack (compile TypeScript)" \
        "${PROJECT_ROOT}/scripts/stack-frontend/build-cdk.sh" || return 1
    
    # Step 6: Synthesize CloudFormation
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Synthesize CloudFormation templates" \
        "${PROJECT_ROOT}/scripts/stack-frontend/synth.sh" || return 1
    
    # Step 7: Test CDK
    if [ "$SKIP_TESTS" = false ]; then
        current_step=$((current_step + 1))
        execute_pipeline_step $current_step $total_steps \
            "Validate CDK templates" \
            "${PROJECT_ROOT}/scripts/stack-frontend/test-cdk.sh" || return 1
    else
        current_step=$((current_step + 1))
        log_warning "Step ${current_step}/${total_steps}: CDK tests skipped"
    fi
    
    # Step 8: Deploy CDK stack
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Deploy Frontend Stack (S3 + CloudFront)" \
        "${PROJECT_ROOT}/scripts/stack-frontend/deploy-cdk.sh" || return 1
    
    # Step 9: Deploy assets
    current_step=$((current_step + 1))
    execute_pipeline_step $current_step $total_steps \
        "Deploy assets (sync to S3, invalidate CloudFront)" \
        "${PROJECT_ROOT}/scripts/stack-frontend/deploy-assets.sh" || return 1
    
    local stack_elapsed=$(elapsed_time $STACK_START_TIME)
    log_success "Frontend Stack deployed successfully in ${stack_elapsed}"
    
    DEPLOYMENT_RESULTS[$stack_name]="SUCCESS"
    DEPLOYMENT_URLS[$stack_name]="CloudFront distribution deployed"
    
    return 0
}

deploy_all() {
    log_header "DEPLOY ALL STACKS - Full Pipeline"
    
    TOTAL_START_TIME=$(start_timer)
    
    log_info "Deployment order: Infrastructure → App API → Inference API → Gateway → Frontend"
    echo ""
    
    if [ "$DRY_RUN" = false ]; then
        read -p "Are you sure you want to deploy all stacks? (y/N): " -n 1 -r
        echo ""
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            log_warning "Deployment cancelled by user"
            return 0
        fi
        echo ""
    fi
    
    # Deploy stacks in dependency order
    local failed_stacks=()
    
    # 1. Infrastructure (foundation)
    if ! deploy_infrastructure; then
        failed_stacks+=("Infrastructure")
        DEPLOYMENT_RESULTS["Infrastructure"]="FAILED"
        if [ "$CONTINUE_ON_ERROR" = false ]; then
            show_deployment_summary
            return 1
        fi
    fi
    
    echo ""
    read -p "Press Enter to continue to next stack..."
    echo ""
    
    # 2. App API (depends on Infrastructure)
    if ! deploy_app_api; then
        failed_stacks+=("App API")
        DEPLOYMENT_RESULTS["App API"]="FAILED"
        if [ "$CONTINUE_ON_ERROR" = false ]; then
            show_deployment_summary
            return 1
        fi
    fi
    
    echo ""
    read -p "Press Enter to continue to next stack..."
    echo ""
    
    # 3. Inference API (depends on Infrastructure)
    if ! deploy_inference_api; then
        failed_stacks+=("Inference API")
        DEPLOYMENT_RESULTS["Inference API"]="FAILED"
        if [ "$CONTINUE_ON_ERROR" = false ]; then
            show_deployment_summary
            return 1
        fi
    fi
    
    echo ""
    read -p "Press Enter to continue to next stack..."
    echo ""
    
    # 4. Gateway (independent, but Inference API integrates with it)
    if ! deploy_gateway; then
        failed_stacks+=("Gateway")
        DEPLOYMENT_RESULTS["Gateway"]="FAILED"
        if [ "$CONTINUE_ON_ERROR" = false ]; then
            show_deployment_summary
            return 1
        fi
    fi
    
    echo ""
    read -p "Press Enter to continue to next stack..."
    echo ""
    
    # 5. Frontend (independent)
    if ! deploy_frontend; then
        failed_stacks+=("Frontend")
        DEPLOYMENT_RESULTS["Frontend"]="FAILED"
        if [ "$CONTINUE_ON_ERROR" = false ]; then
            show_deployment_summary
            return 1
        fi
    fi
    
    # Show final summary
    show_deployment_summary
    
    if [ ${#failed_stacks[@]} -eq 0 ]; then
        return 0
    else
        return 1
    fi
}

###############################################################################
# Deployment summary table
###############################################################################
show_deployment_summary() {
    local total_elapsed=$(elapsed_time $TOTAL_START_TIME)
    
    echo ""
    log_header "DEPLOYMENT SUMMARY"
    
    cat << 'EOF'
┌─────────────────────────┬──────────┬────────────────────────────────────────┐
│ Stack                   │ Status   │ Details                                │
├─────────────────────────┼──────────┼────────────────────────────────────────┤
EOF
    
    # Infrastructure
    local status="${DEPLOYMENT_RESULTS[Infrastructure]:-NOT DEPLOYED}"
    local details="${DEPLOYMENT_URLS[Infrastructure]:-N/A}"
    printf "│ %-23s │ %-8s │ %-38s │\n" "Infrastructure" "$status" "$details"
    
    # App API
    status="${DEPLOYMENT_RESULTS[App API]:-NOT DEPLOYED}"
    details="${DEPLOYMENT_URLS[App API]:-N/A}"
    printf "│ %-23s │ %-8s │ %-38s │\n" "App API" "$status" "$details"
    
    # Inference API
    status="${DEPLOYMENT_RESULTS[Inference API]:-NOT DEPLOYED}"
    details="${DEPLOYMENT_URLS[Inference API]:-N/A}"
    printf "│ %-23s │ %-8s │ %-38s │\n" "Inference API" "$status" "$details"
    
    # Gateway
    status="${DEPLOYMENT_RESULTS[Gateway]:-NOT DEPLOYED}"
    details="${DEPLOYMENT_URLS[Gateway]:-N/A}"
    printf "│ %-23s │ %-8s │ %-38s │\n" "Gateway" "$status" "$details"
    
    # Frontend
    status="${DEPLOYMENT_RESULTS[Frontend]:-NOT DEPLOYED}"
    details="${DEPLOYMENT_URLS[Frontend]:-N/A}"
    printf "│ %-23s │ %-8s │ %-38s │\n" "Frontend" "$status" "$details"
    
    cat << 'EOF'
└─────────────────────────┴──────────┴────────────────────────────────────────┘
EOF
    
    echo ""
    log_info "Total deployment time: ${total_elapsed}"
    
    # Count successes and failures
    local success_count=0
    local failed_count=0
    
    for stack in "Infrastructure" "App API" "Inference API" "Gateway" "Frontend"; do
        if [ "${DEPLOYMENT_RESULTS[$stack]:-}" = "SUCCESS" ]; then
            ((success_count++))
        elif [ "${DEPLOYMENT_RESULTS[$stack]:-}" = "FAILED" ]; then
            ((failed_count++))
        fi
    done
    
    if [ $success_count -gt 0 ]; then
        log_success "Successfully deployed: ${success_count} stack(s)"
    fi
    
    if [ $failed_count -gt 0 ]; then
        log_error "Failed: ${failed_count} stack(s)"
    fi
    
    echo ""
}

###############################################################################
# Interactive menu
###############################################################################
show_menu() {
    show_banner
    
    # Mode indicators
    local mode_str=""
    if [ "$DRY_RUN" = true ]; then
        mode_str+="[DRY-RUN] "
    fi
    if [ "$SKIP_TESTS" = true ]; then
        mode_str+="[SKIP-TESTS] "
    fi
    if [ "$CONTINUE_ON_ERROR" = true ]; then
        mode_str+="[CONTINUE-ON-ERROR] "
    fi
    if [ "$VERBOSE" = true ]; then
        mode_str+="[VERBOSE] "
    fi
    
    if [ -n "$mode_str" ]; then
        echo -e "\033[1;33mActive Modes: ${mode_str}\033[0m"
        echo ""
    fi
    
    cat << EOF
Current Configuration:
  Project Prefix: ${CDK_PROJECT_PREFIX}
  AWS Account:    ${CDK_AWS_ACCOUNT}
  AWS Region:     ${CDK_AWS_REGION}

─────────────────────────────────────────────────────────────────────────────

📦 Stack Deployment Options (Full Build → Test → Deploy Pipeline):

  1) 🏗️  Infrastructure Stack     VPC, ALB, ECS Cluster, Security Groups
                                  (6 steps: install → build → test → synth → deploy)

  2) 🚀 App API Stack            Application API on Fargate
                                  (10 steps: install → build-docker → test → 
                                   build-cdk → synth → push-ecr → deploy)

  3) 🤖 Inference API Stack      AgentCore Runtime with Memory & Tools
                                  (10 steps: install → build-docker → test → 
                                   build-cdk → synth → push-ecr → deploy)

  4) 🌐 Gateway Stack            MCP Gateway with Lambda tools
                                  (7 steps: install → build-cdk → synth → 
                                   test → deploy → verify)

  5) 💻 Frontend Stack           Angular + S3 + CloudFront
                                  (9 steps: install → build → test → build-cdk → 
                                   synth → deploy-cdk → deploy-assets)

─────────────────────────────────────────────────────────────────────────────

  6) 🚢 Deploy All Stacks        Full deployment in dependency order
                                  (Infrastructure → App → Inference → Gateway → Frontend)

  7) ❌ Exit

─────────────────────────────────────────────────────────────────────────────

EOF
    
    read -p "Select an option (1-7): " choice
    echo ""
}

###############################################################################
# Main menu loop
###############################################################################
main_menu() {
    while true; do
        show_menu
        
        case $choice in
            1)
                deploy_infrastructure
                ;;
            2)
                deploy_app_api
                ;;
            3)
                deploy_inference_api
                ;;
            4)
                deploy_gateway
                ;;
            5)
                deploy_frontend
                ;;
            6)
                deploy_all
                ;;
            7)
                log_info "Exiting deployment orchestration"
                exit 0
                ;;
            *)
                log_error "Invalid option: ${choice}"
                ;;
        esac
        
        echo ""
        read -p "Press Enter to continue..."
    done
}

###############################################################################
# Main execution
###############################################################################
main() {
    # Parse command-line arguments
    parse_arguments "$@"
    
    # Initialize timer
    TOTAL_START_TIME=$(start_timer)
    
    # Show banner
    show_banner
    
    # Validate environment
    log_header "Environment Validation"
    if ! validate_environment; then
        log_error "Environment validation failed. Please fix the errors above."
        exit 1
    fi
    
    echo ""
    read -p "Press Enter to continue to deployment menu..."
    
    # Start interactive menu
    main_menu
}

# Run main function
main "$@"
