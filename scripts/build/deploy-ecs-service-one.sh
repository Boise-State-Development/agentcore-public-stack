#!/usr/bin/env bash
#============================================================
# deploy-ecs-service-one.sh — content-hash-aware ECS service code
# deploy for a single service. Per-service wrapper around
# deploy-ecs-service-if-changed.sh.
#
# Usage:
#   deploy-ecs-service-one.sh <service>
#
# Where <service> is one of:
#   app-api
#
# Pre-requisite: scripts/build/build-one.sh has already run for the
# same service in this workflow. It pushes the image to ECR and
# writes the content-hash tag to SSM at
# /{prefix}/{service}/image-tag — which this script reads.
#============================================================
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <service>" >&2
    echo "  service: app-api" >&2
    exit 1
fi

SERVICE="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DEPLOY="${SCRIPT_DIR}/deploy-ecs-service-if-changed.sh"

source "${PROJECT_ROOT}/scripts/common/load-env.sh"
export AWS_REGION="${CDK_AWS_REGION}"

REGISTRY="${CDK_AWS_ACCOUNT}.dkr.ecr.${CDK_AWS_REGION}.amazonaws.com"

# Per-service spec.
case "$SERVICE" in
    app-api)
        CLUSTER_NAME_SSM="/${CDK_PROJECT_PREFIX}/${SERVICE}/cluster-name"
        SERVICE_NAME_SSM="/${CDK_PROJECT_PREFIX}/${SERVICE}/service-name"
        TASK_DEF_FAMILY_SSM="/${CDK_PROJECT_PREFIX}/${SERVICE}/task-def-family"
        IMAGE_URI_SSM="/${CDK_PROJECT_PREFIX}/${SERVICE}/image-tag"
        ECR_REPO_URI="${REGISTRY}/${CDK_PROJECT_PREFIX}-${SERVICE}"
        ;;
    *)
        echo "Unknown service: $SERVICE" >&2
        echo "Expected one of: app-api" >&2
        exit 1
        ;;
esac

cd "$PROJECT_ROOT"
log_info "Deploying ${SERVICE} container image to ECS..."

TAG="$(bash "$DEPLOY" \
    --service "$SERVICE" \
    --cluster-name-ssm "$CLUSTER_NAME_SSM" \
    --service-name-ssm "$SERVICE_NAME_SSM" \
    --task-def-family-ssm "$TASK_DEF_FAMILY_SSM" \
    --image-uri-ssm "$IMAGE_URI_SSM" \
    --ecr-repo-uri "$ECR_REPO_URI")"

log_info "${SERVICE}: ${TAG}"

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "image_tag=${TAG}" >> "$GITHUB_OUTPUT"
fi
