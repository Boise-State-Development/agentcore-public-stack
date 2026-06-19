#!/usr/bin/env bash
#============================================================
# build-one.sh — build & push a single backend Docker image,
# skipping the build/push when ECR already has the content hash.
#
# Usage:
#   build-one.sh <service>
#
# Where <service> is one of:
#   app-api | inference-api | rag-ingestion
#
# This script encapsulates the per-service spec (which Dockerfile,
# which source trees, which manifests, which platform) so that:
#
#   1. The CI workflow (.github/workflows/backend.yml) can have
#      one job per image — each job runs `bash scripts/build/build-one.sh
#      <service>` and the dashboard shows three independent build
#      jobs that succeed/fail/skip individually.
#   2. The local convenience script (scripts/build/build-all-images.sh)
#      stays a thin loop over the three services.
#
# Inputs that affect each image's content hash should match the
# Dockerfile's COPY surface as tightly as possible — broader source
# inputs cause more false-positive rebuilds. See compute-content-hash.sh
# for the hash semantics.
#
# Required env (resolved by scripts/common/load-env.sh):
#   CDK_PROJECT_PREFIX
#   CDK_AWS_REGION
#   CDK_AWS_ACCOUNT
#
# Optional env:
#   GITHUB_OUTPUT   set by GitHub Actions; if set, the resulting tag
#                   is published as `image_tag=<tag>` so the workflow
#                   surfaces it in the run summary.
#============================================================
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <service>" >&2
    echo "  service: app-api | inference-api | rag-ingestion" >&2
    exit 1
fi

SERVICE="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BUILD_AND_PUSH="${SCRIPT_DIR}/build-and-push-if-changed.sh"

source "${PROJECT_ROOT}/scripts/common/load-env.sh"

# build-and-push-if-changed.sh expects AWS_REGION (not CDK_AWS_REGION).
export AWS_REGION="${CDK_AWS_REGION}"
REGISTRY="${CDK_AWS_ACCOUNT}.dkr.ecr.${CDK_AWS_REGION}.amazonaws.com"

# ---------------------------------------------------------------
# Per-service spec.
#
# The hash inputs (DOCKERFILE + SOURCE_DIRS + MANIFESTS) should mirror
# the Dockerfile's COPY surface as tightly as possible:
#
# app-api / inference-api: the Dockerfile COPYs all of backend/src/
# plus pyproject.toml + uv.lock. Hashing the same is correct.
#
# rag-ingestion: the Dockerfile only COPYs three specific subtrees
# (documents/ingestion/, shared/__init__.py, shared/embeddings/),
# so the hash inputs are scoped to those. Earlier versions hashed
# the whole backend/src tree, which produced false-positive rebuilds
# whenever an unrelated file (e.g. agents/) changed. Tightening the
# inputs here saves a slow rag-ingestion rebuild (docling models +
# Rust toolchain) every time something else moves.
# ---------------------------------------------------------------
case "$SERVICE" in
    app-api)
        DOCKERFILE="backend/Dockerfile.app-api"
        SOURCE_DIRS=("backend/src")
        MANIFESTS=("backend/pyproject.toml" "backend/uv.lock")
        PLATFORM=""
        SSM_KEY="/${CDK_PROJECT_PREFIX}/app-api/image-tag"
        ;;
    inference-api)
        DOCKERFILE="backend/Dockerfile.inference-api"
        SOURCE_DIRS=("backend/src")
        MANIFESTS=("backend/pyproject.toml" "backend/uv.lock")
        # Inference API runs on AgentCore Runtime (arm64).
        PLATFORM="linux/arm64"
        SSM_KEY="/${CDK_PROJECT_PREFIX}/inference-api/image-tag"
        ;;
    rag-ingestion)
        DOCKERFILE="backend/Dockerfile.rag-ingestion"
        SOURCE_DIRS=(
            "backend/src/apis/app_api/documents/ingestion"
            "backend/src/apis/shared/embeddings"
        )
        # shared/__init__.py is a single file, hashed as a manifest.
        # The requirements.lock lives inside the first source-dir
        # already, so it doesn't need a separate --manifest entry.
        MANIFESTS=("backend/src/apis/shared/__init__.py")
        # The RAG ingestion Lambda is arm64 (see the rag-ingestion CDK
        # construct), and the Dockerfile installs arm64 torch wheels.
        # Build for arm64 — an amd64 image fails the arm64 Lambda at
        # init with Runtime.InvalidEntrypoint. The backend.yml
        # build-rag-ingestion job runs on a native ubuntu-24.04-arm
        # runner, so this is a native (non-emulated) build.
        PLATFORM="linux/arm64"
        SSM_KEY="/${CDK_PROJECT_PREFIX}/rag-ingestion/image-tag"
        ;;
    *)
        echo "Unknown service: $SERVICE" >&2
        echo "Expected one of: app-api | inference-api | rag-ingestion" >&2
        exit 1
        ;;
esac

REPO="${REGISTRY}/${CDK_PROJECT_PREFIX}-${SERVICE}"

# ---------------------------------------------------------------
# Build args
# ---------------------------------------------------------------
build_args=(
    --service "$SERVICE"
    --dockerfile "$DOCKERFILE"
    --ecr-repository "$REPO"
)
for d in "${SOURCE_DIRS[@]}"; do
    build_args+=( --source-dir "$d" )
done
for m in "${MANIFESTS[@]}"; do
    build_args+=( --manifest "$m" )
done
if [[ -n "$PLATFORM" ]]; then
    build_args+=( --platform "$PLATFORM" )
fi

cd "$PROJECT_ROOT"
log_info "Building ${SERVICE}..."

TAG="$(bash "$BUILD_AND_PUSH" "${build_args[@]}")"

log_info "${SERVICE}: ${TAG}"

# Publish to GITHUB_OUTPUT so the workflow surfaces the tag.
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "image_tag=${TAG}" >> "$GITHUB_OUTPUT"
fi

# Publish to SSM so the CDK constructs that read the URI at deploy
# time pick up the freshly-built image. The constructs use this value
# directly as the ECS TaskDefinition Image / AgentCore Runtime
# ContainerUri — so it must be the FULL ECR URI (including registry,
# repo, and tag), not just the tag. CFN validates this against the
# ECR URI regex on every deploy and rejects bare tags.
# `put-parameter --overwrite` is idempotent. Per the devops gotcha,
# --overwrite cannot be combined with --tags for an existing
# parameter, so we omit --tags.
SSM_VALUE="${REPO}:${TAG}"
aws ssm put-parameter \
    --region "${CDK_AWS_REGION}" \
    --name "${SSM_KEY}" \
    --value "${SSM_VALUE}" \
    --type String \
    --overwrite \
    --no-cli-pager >/dev/null

log_info "${SERVICE}: SSM ${SSM_KEY} = ${SSM_VALUE}"
