#!/usr/bin/env bash
# scripts/frontend/deploy.sh — sync SPA build artifacts to S3 + invalidate CloudFront.
# Reads bucket name and distribution ID from SSM.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../common/load-env.sh"

# load-env.sh exports CDK_AWS_REGION; mirror it into AWS_REGION for
# the AWS CLI calls below. CI sets AWS_REGION at the job level, so
# this is mainly for local runs.
export AWS_REGION="${AWS_REGION:-${CDK_AWS_REGION}}"

: "${CDK_PROJECT_PREFIX:?CDK_PROJECT_PREFIX is required}"
: "${AWS_REGION:?AWS_REGION is required (export CDK_AWS_REGION)}"

BUCKET_NAME=$(aws ssm get-parameter \
  --name "/${CDK_PROJECT_PREFIX}/frontend/bucket-name" \
  --region "$AWS_REGION" \
  --query 'Parameter.Value' --output text)

DISTRIBUTION_ID=$(aws ssm get-parameter \
  --name "/${CDK_PROJECT_PREFIX}/frontend/distribution-id" \
  --region "$AWS_REGION" \
  --query 'Parameter.Value' --output text)

echo "Syncing to s3://${BUCKET_NAME}..."
# Angular's @angular/build:application builder (default since v17)
# emits the SPA into a `browser/` subdirectory of outputPath. The
# index.html, main bundle, and asset hashes all live under
# dist/ai.client/browser/, NOT dist/ai.client/. Syncing the parent
# would put index.html at s3://bucket/browser/index.html and serve
# 403 Access Denied to anyone hitting `/` (CloudFront's
# defaultRootObject = index.html resolves to /index.html, no key).
aws s3 sync "$SCRIPT_DIR/../../frontend/ai.client/dist/ai.client/browser/" \
  "s3://${BUCKET_NAME}/" --delete --region "$AWS_REGION"

echo "Invalidating CloudFront distribution ${DISTRIBUTION_ID}..."
aws cloudfront create-invalidation \
  --distribution-id "$DISTRIBUTION_ID" \
  --paths "/*" --query 'Invalidation.Id' --output text

echo "Frontend deploy complete."
