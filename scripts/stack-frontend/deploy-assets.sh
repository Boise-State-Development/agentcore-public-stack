#!/bin/bash
# Frontend assets deployment script - Sync build files to S3 and invalidate CloudFront
# This script uploads the built Angular application to S3 and invalidates the CloudFront cache

set -euo pipefail

# Get the repository root directory
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FRONTEND_DIR="${REPO_ROOT}/frontend/ai.client"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Logging functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

# Load environment variables
log_info "Loading environment configuration..."
if [ -f "${REPO_ROOT}/scripts/common/load-env.sh" ]; then
    # shellcheck source=../common/load-env.sh
    source "${REPO_ROOT}/scripts/common/load-env.sh"
else
    log_error "Environment loader not found: ${REPO_ROOT}/scripts/common/load-env.sh"
    exit 1
fi

# Check if AWS CLI is installed
if ! command -v aws &> /dev/null; then
    log_error "AWS CLI is not installed. Please install it first."
    log_error "Run: scripts/common/install-deps.sh"
    exit 1
fi

# Check if build output exists
if [ ! -d "${FRONTEND_DIR}/dist" ]; then
    log_error "Build output not found. Please run build.sh first."
    log_error "Run: scripts/stack-frontend/build.sh"
    exit 1
fi

# Find the build output directory
BUILD_OUTPUT_DIR=$(find "${FRONTEND_DIR}/dist" -mindepth 1 -maxdepth 1 -type d | head -1)

if [ -z "${BUILD_OUTPUT_DIR}" ]; then
    log_error "Build output directory not found in ${FRONTEND_DIR}/dist/"
    exit 1
fi

log_info "Deploying frontend assets..."
log_info "Build output: ${BUILD_OUTPUT_DIR}"

# Retrieve bucket name and distribution ID from SSM Parameter Store
log_info "Retrieving deployment targets from SSM Parameter Store..."

BUCKET_NAME=$(aws ssm get-parameter \
    --name "/${CDK_PROJECT_PREFIX}/frontend/bucket-name" \
    --region ${CDK_AWS_REGION} \
    --query 'Parameter.Value' \
    --output text 2>/dev/null)

if [ -z "${BUCKET_NAME}" ] || [ "${BUCKET_NAME}" == "None" ]; then
    log_error "Could not retrieve S3 bucket name from SSM Parameter Store"
    log_error "Make sure the FrontendStack has been deployed first"
    log_error "Run: scripts/stack-frontend/deploy-cdk.sh"
    exit 1
fi

DISTRIBUTION_ID=$(aws ssm get-parameter \
    --name "/${CDK_PROJECT_PREFIX}/frontend/distribution-id" \
    --region ${CDK_AWS_REGION} \
    --query 'Parameter.Value' \
    --output text 2>/dev/null)

if [ -z "${DISTRIBUTION_ID}" ] || [ "${DISTRIBUTION_ID}" == "None" ]; then
    log_error "Could not retrieve CloudFront distribution ID from SSM Parameter Store"
    log_error "Make sure the FrontendStack has been deployed first"
    exit 1
fi

log_info "S3 Bucket: ${BUCKET_NAME}"
log_info "CloudFront Distribution: ${DISTRIBUTION_ID}"

# Sync files to S3
log_info "Syncing files to S3..."

aws s3 sync "${BUILD_OUTPUT_DIR}/" "s3://${BUCKET_NAME}/" \
    --region ${CDK_AWS_REGION} \
    --delete \
    --cache-control "public,max-age=31536000,immutable" \
    --exclude "*.html" \
    --exclude "*.json"

# Upload HTML and JSON files with different cache settings (short cache)
log_info "Uploading HTML and JSON files with no-cache headers..."

aws s3 sync "${BUILD_OUTPUT_DIR}/" "s3://${BUCKET_NAME}/" \
    --region ${CDK_AWS_REGION} \
    --cache-control "public,max-age=0,must-revalidate" \
    --exclude "*" \
    --include "*.html" \
    --include "*.json"

# Verify sync
S3_FILE_COUNT=$(aws s3 ls "s3://${BUCKET_NAME}/" --recursive --region ${CDK_AWS_REGION} | wc -l)
log_info "Files in S3 bucket: ${S3_FILE_COUNT}"

# Invalidate CloudFront cache
log_info "Creating CloudFront cache invalidation..."

INVALIDATION_ID=$(aws cloudfront create-invalidation \
    --distribution-id ${DISTRIBUTION_ID} \
    --paths "/*" \
    --query 'Invalidation.Id' \
    --output text)

if [ -n "${INVALIDATION_ID}" ]; then
    log_info "Invalidation created: ${INVALIDATION_ID}"
    log_info "Invalidation status: In Progress"
    
    # Optional: Wait for invalidation to complete (can take 5-15 minutes)
    if [ "${WAIT_FOR_INVALIDATION:-false}" == "true" ]; then
        log_info "Waiting for invalidation to complete (this may take several minutes)..."
        aws cloudfront wait invalidation-completed \
            --distribution-id ${DISTRIBUTION_ID} \
            --id ${INVALIDATION_ID}
        log_info "Invalidation completed!"
    else
        log_info "Not waiting for invalidation to complete (set WAIT_FOR_INVALIDATION=true to wait)"
    fi
else
    log_error "Failed to create CloudFront invalidation"
    exit 1
fi

# Display deployment information
WEBSITE_URL=$(aws ssm get-parameter \
    --name "/${CDK_PROJECT_PREFIX}/frontend/url" \
    --region ${CDK_AWS_REGION} \
    --query 'Parameter.Value' \
    --output text 2>/dev/null)

log_info "Frontend assets deployed successfully!"
log_info "Website URL: ${WEBSITE_URL}"
log_info ""
log_info "Note: CloudFront invalidation is in progress. Changes may take 5-15 minutes to propagate globally."
