#!/usr/bin/env bash
#============================================================
# deploy-image-lambda-if-changed.sh — content-hash-aware Lambda
# image code deploy.
#
# Mirror of deploy-lambda-code-if-changed.sh but for DockerImage
# Lambdas. Wraps build-and-push-if-changed.sh (which produces an
# ECR image with a content-hash tag) and follows up with
# `aws lambda update-function-code --image-uri`.
#
# Usage:
#   deploy-image-lambda-if-changed.sh \
#     --service           rag-ingestion \
#     --function-name-ssm /ai-sbmt-api/rag/ingestion-function-name \
#     --image-uri-ssm     /ai-sbmt-api/rag-ingestion/image-tag \
#     --ecr-repo-uri      327491786490.dkr.ecr.us-west-2.amazonaws.com/ai-sbmt-api-rag-ingestion
#
# Required env:
#   AWS_REGION            (e.g., us-west-2)
#
# Pre-requisites:
#   build-and-push-if-changed.sh has already run for this service
#   and updated `--image-uri-ssm` with the latest content-hash tag.
#   This script reads that tag from SSM and points the Lambda at it.
#============================================================
set -euo pipefail

SERVICE=""
FUNCTION_NAME_SSM=""
IMAGE_URI_SSM=""
ECR_REPO_URI=""

usage() {
    cat <<EOF >&2
Usage: $0 --service NAME --function-name-ssm PATH \\
          --image-uri-ssm PATH --ecr-repo-uri URI
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --service)            SERVICE="$2"; shift 2 ;;
        --function-name-ssm)  FUNCTION_NAME_SSM="$2"; shift 2 ;;
        --image-uri-ssm)      IMAGE_URI_SSM="$2"; shift 2 ;;
        --ecr-repo-uri)       ECR_REPO_URI="$2"; shift 2 ;;
        -h|--help)            usage ;;
        *)                    echo "Unknown arg: $1" >&2; usage ;;
    esac
done

[[ -n "$SERVICE"           ]] || { echo "missing --service" >&2; usage; }
[[ -n "$FUNCTION_NAME_SSM" ]] || { echo "missing --function-name-ssm" >&2; usage; }
[[ -n "$IMAGE_URI_SSM"     ]] || { echo "missing --image-uri-ssm" >&2; usage; }
[[ -n "$ECR_REPO_URI"      ]] || { echo "missing --ecr-repo-uri" >&2; usage; }
[[ -n "${AWS_REGION:-}"    ]] || { echo "AWS_REGION env var required" >&2; exit 2; }

log() { echo "[$SERVICE] $*" >&2; }

# 1. Resolve the image tag from SSM (set by build-one.sh just before
# this script runs).
IMAGE_TAG="$(aws ssm get-parameter \
    --region "$AWS_REGION" \
    --name "$IMAGE_URI_SSM" \
    --query 'Parameter.Value' \
    --output text)"
NEW_IMAGE_URI="${ECR_REPO_URI}:${IMAGE_TAG}"
log "Target image URI: $NEW_IMAGE_URI"

# 2. Resolve the function name (CDK-auto-generated).
FUNCTION_NAME="$(aws ssm get-parameter \
    --region "$AWS_REGION" \
    --name "$FUNCTION_NAME_SSM" \
    --query 'Parameter.Value' \
    --output text)"
log "Function name: $FUNCTION_NAME"

# 3. Check what the Lambda is currently pointed at. If it's already
# the new URI, skip — same idempotency principle as the rest of the
# build-once / deploy-once pipeline.
CURRENT_IMAGE_URI="$(aws lambda get-function-configuration \
    --region "$AWS_REGION" \
    --function-name "$FUNCTION_NAME" \
    --query 'PackageType==`Image` && Code.ImageUri || `unknown`' \
    --output text 2>/dev/null || echo unknown)"

if [[ "$CURRENT_IMAGE_URI" == "$NEW_IMAGE_URI" ]]; then
    log "Lambda already on $NEW_IMAGE_URI — skipping update-function-code."
    echo "$IMAGE_TAG"
    exit 0
fi

log "Lambda currently on $CURRENT_IMAGE_URI — updating to $NEW_IMAGE_URI."

# 4. Wait for the Lambda to be in a ready state.
log "Waiting for function to be ready for update..."
aws lambda wait function-updated \
    --region "$AWS_REGION" \
    --function-name "$FUNCTION_NAME" >&2 || {
        aws lambda get-function-configuration \
            --region "$AWS_REGION" \
            --function-name "$FUNCTION_NAME" \
            --query '{State:State,LastUpdateStatus:LastUpdateStatus,StateReason:StateReason}' >&2 || true
        exit 3
    }

# 5. Update.
log "Calling aws lambda update-function-code..."
aws lambda update-function-code \
    --region "$AWS_REGION" \
    --function-name "$FUNCTION_NAME" \
    --image-uri "$NEW_IMAGE_URI" \
    --no-cli-pager \
    --output text \
    --query 'FunctionArn' >/dev/null

log "Waiting for update to settle..."
aws lambda wait function-updated \
    --region "$AWS_REGION" \
    --function-name "$FUNCTION_NAME" >&2

log "Done. Lambda now at $NEW_IMAGE_URI"
echo "$IMAGE_TAG"
