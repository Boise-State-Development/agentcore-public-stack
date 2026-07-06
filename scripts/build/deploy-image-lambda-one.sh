#!/usr/bin/env bash
#============================================================
# deploy-image-lambda-one.sh — content-hash-aware image-Lambda code
# deploy for a single Lambda. Per-service wrapper around
# deploy-image-lambda-if-changed.sh.
#
# Usage:
#   deploy-image-lambda-one.sh <service>
#
# Where <service> is one of:
#   rag-ingestion | kb-sync-dispatcher | kb-sync-worker |
#   scheduled-runs-dispatcher | scheduled-runs-worker
#
# kb-sync-dispatcher/kb-sync-worker (and scheduled-runs-dispatcher/
# scheduled-runs-worker) are pairs of Lambda functions sharing a single
# image; their handlers differ only via ImageConfig.Command, which
# `update-function-code` doesn't touch.
#
# Pre-requisite: scripts/build/build-one.sh has already run for the
# same service in this workflow. It pushes the image to ECR and
# writes the content-hash tag to SSM at
# /{prefix}/{service}/image-tag — which this script reads.
#============================================================
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <service>" >&2
    echo "  service: rag-ingestion | kb-sync-dispatcher | kb-sync-worker | scheduled-runs-dispatcher | scheduled-runs-worker" >&2
    exit 1
fi

SERVICE="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DEPLOY="${SCRIPT_DIR}/deploy-image-lambda-if-changed.sh"

source "${PROJECT_ROOT}/scripts/common/load-env.sh"
export AWS_REGION="${CDK_AWS_REGION}"

REGISTRY="${CDK_AWS_ACCOUNT}.dkr.ecr.${CDK_AWS_REGION}.amazonaws.com"

# Per-service spec.
case "$SERVICE" in
    rag-ingestion)
        FUNCTION_NAME_SSM="/${CDK_PROJECT_PREFIX}/rag/ingestion-function-name"
        IMAGE_URI_SSM="/${CDK_PROJECT_PREFIX}/${SERVICE}/image-tag"
        ECR_REPO_URI="${REGISTRY}/${CDK_PROJECT_PREFIX}-${SERVICE}"
        ;;
    kb-sync-dispatcher)
        FUNCTION_NAME_SSM="/${CDK_PROJECT_PREFIX}/kb-sync/dispatcher-function-name"
        IMAGE_URI_SSM="/${CDK_PROJECT_PREFIX}/kb-sync/image-tag"
        ECR_REPO_URI="${REGISTRY}/${CDK_PROJECT_PREFIX}-kb-sync"
        ;;
    kb-sync-worker)
        FUNCTION_NAME_SSM="/${CDK_PROJECT_PREFIX}/kb-sync/worker-function-name"
        IMAGE_URI_SSM="/${CDK_PROJECT_PREFIX}/kb-sync/image-tag"
        ECR_REPO_URI="${REGISTRY}/${CDK_PROJECT_PREFIX}-kb-sync"
        ;;
    scheduled-runs-dispatcher)
        FUNCTION_NAME_SSM="/${CDK_PROJECT_PREFIX}/scheduled-runs/dispatcher-function-name"
        IMAGE_URI_SSM="/${CDK_PROJECT_PREFIX}/scheduled-runs/image-tag"
        ECR_REPO_URI="${REGISTRY}/${CDK_PROJECT_PREFIX}-scheduled-runs"
        ;;
    scheduled-runs-worker)
        FUNCTION_NAME_SSM="/${CDK_PROJECT_PREFIX}/scheduled-runs/worker-function-name"
        IMAGE_URI_SSM="/${CDK_PROJECT_PREFIX}/scheduled-runs/image-tag"
        ECR_REPO_URI="${REGISTRY}/${CDK_PROJECT_PREFIX}-scheduled-runs"
        ;;
    *)
        echo "Unknown service: $SERVICE" >&2
        echo "Expected one of: rag-ingestion | kb-sync-dispatcher | kb-sync-worker | scheduled-runs-dispatcher | scheduled-runs-worker" >&2
        exit 1
        ;;
esac

cd "$PROJECT_ROOT"

# First-deploy grace: the function-name SSM param is published by
# PlatformStack when the Lambda's construct first deploys. A backend
# run that lands before that platform deploy (e.g. the PR introducing
# a new Lambda) has an image in ECR but no function to point at it —
# skip gracefully; the next backend run after the platform deploy
# completes the swap.
if ! aws ssm get-parameter --region "$AWS_REGION" --name "$FUNCTION_NAME_SSM" >/dev/null 2>&1; then
    log_warn "SSM ${FUNCTION_NAME_SSM} not found — ${SERVICE} not yet provisioned by PlatformStack; skipping code deploy."
    if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
        echo "image_tag=skipped-not-provisioned" >> "$GITHUB_OUTPUT"
    fi
    exit 0
fi

log_info "Deploying ${SERVICE} container image to Lambda..."

TAG="$(bash "$DEPLOY" \
    --service "$SERVICE" \
    --function-name-ssm "$FUNCTION_NAME_SSM" \
    --image-uri-ssm "$IMAGE_URI_SSM" \
    --ecr-repo-uri "$ECR_REPO_URI")"

log_info "${SERVICE}: ${TAG}"

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "image_tag=${TAG}" >> "$GITHUB_OUTPUT"
fi
