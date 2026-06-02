#!/usr/bin/env bash
#============================================================
# deploy-runtime-image-one.sh — content-hash-aware AgentCore
# Runtime image deploy for a single Runtime. Per-service wrapper
# around deploy-runtime-image-if-changed.sh.
#
# Usage:
#   deploy-runtime-image-one.sh <service>
#
# Where <service> is one of:
#   inference-api
#
# Pre-requisite: scripts/build/build-one.sh has already run for the
# same service in this workflow. It pushes the image to ECR and
# writes the content-hash tag to SSM at
# /{prefix}/{service}/image-tag — which this script reads.
#============================================================
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <service>" >&2
    echo "  service: inference-api" >&2
    exit 1
fi

SERVICE="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DEPLOY="${SCRIPT_DIR}/deploy-runtime-image-if-changed.sh"

source "${PROJECT_ROOT}/scripts/common/load-env.sh"
export AWS_REGION="${CDK_AWS_REGION}"

REGISTRY="${CDK_AWS_ACCOUNT}.dkr.ecr.${CDK_AWS_REGION}.amazonaws.com"

# Per-service spec.
case "$SERVICE" in
    inference-api)
        RUNTIME_ID_SSM="/${CDK_PROJECT_PREFIX}/inference-api/runtime-id"
        IMAGE_URI_SSM="/${CDK_PROJECT_PREFIX}/${SERVICE}/image-tag"
        ECR_REPO_URI="${REGISTRY}/${CDK_PROJECT_PREFIX}-${SERVICE}"
        ;;
    *)
        echo "Unknown service: $SERVICE" >&2
        echo "Expected one of: inference-api" >&2
        exit 1
        ;;
esac

cd "$PROJECT_ROOT"
log_info "Deploying ${SERVICE} container image to AgentCore Runtime..."

TAG="$(bash "$DEPLOY" \
    --service "$SERVICE" \
    --runtime-id-ssm "$RUNTIME_ID_SSM" \
    --image-uri-ssm "$IMAGE_URI_SSM" \
    --ecr-repo-uri "$ECR_REPO_URI")"

log_info "${SERVICE}: ${TAG}"

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "image_tag=${TAG}" >> "$GITHUB_OUTPUT"
fi
