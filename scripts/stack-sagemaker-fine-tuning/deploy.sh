#!/bin/bash
set -euo pipefail

# Script: Deploy SageMaker Fine-Tuning Infrastructure
# Description: Deploys CDK infrastructure for SageMaker fine-tuning
#              (DynamoDB tables, S3 bucket, IAM role, security group)

# Get the directory of this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
INFRASTRUCTURE_DIR="${PROJECT_ROOT}/infrastructure"

# Source common utilities
source "${PROJECT_ROOT}/scripts/common/load-env.sh"

# Logging functions
log_info() {
    echo "[INFO] $1"
}

log_error() {
    echo "[ERROR] $1" >&2
}

log_success() {
    echo "[SUCCESS] $1"
}

main() {
    # Check if stack is enabled
    if [ "${CDK_FINE_TUNING_ENABLED}" != "true" ] && [ "${CDK_FINE_TUNING_ENABLED}" != "1" ]; then
        log_info "SageMaker Fine-Tuning stack is disabled (CDK_FINE_TUNING_ENABLED=${CDK_FINE_TUNING_ENABLED:-<not set>}). Skipping deploy."
        exit 0
    fi

    log_info "Deploying SageMaker Fine-Tuning Stack..."

    # Configuration already loaded by sourcing load-env.sh

    # Validate required environment variables
    if [ -z "${CDK_AWS_ACCOUNT}" ]; then
        log_error "CDK_AWS_ACCOUNT is not set"
        exit 1
    fi

    if [ -z "${CDK_AWS_REGION}" ]; then
        log_error "CDK_AWS_REGION is not set"
        exit 1
    fi

    # Change to infrastructure directory
    cd "${INFRASTRUCTURE_DIR}"

    # Check if node_modules exists
    if [ ! -d "node_modules" ]; then
        log_info "node_modules not found in CDK directory. Installing dependencies..."
        npm ci
    fi

    # Bootstrap CDK if needed (idempotent operation)
    # Note: Run from project root to avoid loading CDK app context
    log_info "Ensuring CDK is bootstrapped..."
    cd "${PROJECT_ROOT}"
    npx cdk bootstrap "aws://${CDK_AWS_ACCOUNT}/${CDK_AWS_REGION}" \
        || log_info "CDK already bootstrapped or bootstrap failed (continuing anyway)"
    cd infrastructure/

    # Check if pre-synthesized template exists
    if [ -d "cdk.out" ] && [ -f "cdk.out/SageMakerFineTuningStack.template.json" ]; then
        log_info "Using pre-synthesized CloudFormation template from cdk.out/"
        CDK_APP="cdk.out/"
    else
        log_info "No pre-synthesized template found. CDK will synthesize during deployment."
        CDK_APP=""
    fi

    # Deploy CDK stack
    log_info "Deploying SageMakerFineTuningStack with CDK..."

    # Use CDK_REQUIRE_APPROVAL env var with fallback to never
    REQUIRE_APPROVAL="${CDK_REQUIRE_APPROVAL:-never}"

    if [ -n "${CDK_APP}" ]; then
        # Deploy using pre-synthesized template
        npx cdk deploy SageMakerFineTuningStack \
            --app "${CDK_APP}" \
            --require-approval ${REQUIRE_APPROVAL} \
            --outputs-file "${PROJECT_ROOT}/cdk-outputs-sagemaker-fine-tuning.json"
    else
        # Deploy with context parameters (will synthesize first)
        # Build context parameters using shared helper function
        CONTEXT_PARAMS=$(build_cdk_context_params)

        # Execute CDK deploy with context parameters
        eval "npx cdk deploy SageMakerFineTuningStack --require-approval ${REQUIRE_APPROVAL} ${CONTEXT_PARAMS} --outputs-file \"${PROJECT_ROOT}/cdk-outputs-sagemaker-fine-tuning.json\""
    fi

    log_success "CDK deployment completed successfully"

    log_success "SageMaker Fine-Tuning Stack deployment completed successfully!"
    log_info ""
    log_info "Next steps:"
    log_info "  1. Verify DynamoDB tables (fine-tuning-jobs, fine-tuning-access) in AWS Console"
    log_info "  2. Verify S3 bucket (fine-tuning-data) with CORS configuration"
    log_info "  3. Check SageMaker execution role and security group"
    log_info "  4. Deploy App API stack to connect fine-tuning endpoints"
}

main "$@"
