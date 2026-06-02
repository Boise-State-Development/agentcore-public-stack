#!/usr/bin/env bash
#============================================================
# build-and-push-if-changed.sh — content-hash-aware Docker build.
#
# Computes a content hash of the inputs (via compute-content-hash.sh),
# checks whether ECR already has an image with that tag, and only
# runs `docker build` + `docker push` when the tag is missing.
#
# Usage:
#   build-and-push-if-changed.sh \
#     --service        app-api \
#     --dockerfile     backend/Dockerfile.app-api \
#     --source-dir     backend/src \
#     --manifest       backend/pyproject.toml \
#     --manifest       backend/uv.lock \
#     --ecr-repository 327491786490.dkr.ecr.us-west-2.amazonaws.com/ai-sbmt-api-app-api \
#     [--platform     linux/amd64]
#
# Required env:
#   AWS_REGION          (e.g., us-west-2)
#
# Output (stdout):
#   The image tag that ends up in ECR — whether freshly pushed or
#   already present. Callers should capture this.
#
# Logs (stderr):
#   Human-readable progress (so callers piping stdout get just the
#   tag and nothing else).
#============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPUTE_HASH="${SCRIPT_DIR}/compute-content-hash.sh"

SERVICE=""
DOCKERFILE=""
SOURCE_DIRS=()
ECR_REPO=""
PLATFORM=""
MANIFESTS=()

usage() {
    cat <<EOF >&2
Usage: $0 --service NAME --dockerfile PATH --ecr-repository URI \\
          [--source-dir DIR]... [--manifest PATH]... [--platform PLAT]
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --service)        SERVICE="$2"; shift 2 ;;
        --dockerfile)     DOCKERFILE="$2"; shift 2 ;;
        --source-dir)     SOURCE_DIRS+=("$2"); shift 2 ;;
        --manifest)       MANIFESTS+=("$2"); shift 2 ;;
        --ecr-repository) ECR_REPO="$2"; shift 2 ;;
        --platform)       PLATFORM="$2"; shift 2 ;;
        -h|--help)        usage ;;
        *)                echo "Unknown arg: $1" >&2; usage ;;
    esac
done

[[ -n "$SERVICE"    ]] || { echo "missing --service" >&2; usage; }
[[ -n "$DOCKERFILE" ]] || { echo "missing --dockerfile" >&2; usage; }
[[ ${#SOURCE_DIRS[@]} -gt 0 ]] || { echo "at least one --source-dir required" >&2; usage; }
[[ -n "$ECR_REPO"   ]] || { echo "missing --ecr-repository" >&2; usage; }
[[ -n "${AWS_REGION:-}" ]] || { echo "AWS_REGION env var required" >&2; exit 2; }

log() { echo "[$SERVICE] $*" >&2; }

# Repository name is the part after the last `/` of the URI.
REPO_NAME="${ECR_REPO##*/}"
# Registry is everything before that last `/`.
REGISTRY="${ECR_REPO%/*}"

# 1. Compute the content hash. This is the candidate image tag.
HASH_ARGS=( --dockerfile "$DOCKERFILE" )
for d in "${SOURCE_DIRS[@]}"; do
    HASH_ARGS+=( --source-dir "$d" )
done
for m in "${MANIFESTS[@]}"; do
    HASH_ARGS+=( --manifest "$m" )
done
log "Computing content hash..."
TAG="$(bash "$COMPUTE_HASH" "${HASH_ARGS[@]}")"
log "Content hash: $TAG"

# 2. Ensure the ECR repository exists. CDK only IMPORTS these repos
# (via ecr.Repository.fromRepositoryName), so they must be provisioned
# out-of-band. We follow the same pattern as scripts/common/promote-ecr-image.sh:
# describe-then-create with scanOnPush, AES256, and the standard tags.
if ! aws ecr describe-repositories \
        --region "$AWS_REGION" \
        --repository-names "$REPO_NAME" \
        --output text \
        --query 'repositories[0].repositoryName' \
        >/dev/null 2>&1; then
    log "Creating ECR repository: $REPO_NAME"
    aws ecr create-repository \
        --region "$AWS_REGION" \
        --repository-name "$REPO_NAME" \
        --image-scanning-configuration scanOnPush=true \
        --encryption-configuration encryptionType=AES256 \
        --tags "Key=Project,Value=${CDK_PROJECT_PREFIX:-${REPO_NAME%%-*}}" \
               "Key=ManagedBy,Value=GitHubActions" \
        --no-cli-pager >/dev/null
fi

# 3. Does ECR already have an image with this tag?
if aws ecr describe-images \
        --region "$AWS_REGION" \
        --repository-name "$REPO_NAME" \
        --image-ids "imageTag=$TAG" \
        --output text \
        --query 'imageDetails[0].imageDigest' \
        >/dev/null 2>&1; then
    log "Image $REPO_NAME:$TAG already in ECR — skipping build/push."
    echo "$TAG"
    exit 0
fi

log "Image $REPO_NAME:$TAG not in ECR — building and pushing."

# 4. Log in to ECR (idempotent — re-running just refreshes the auth token).
aws ecr get-login-password --region "$AWS_REGION" \
    | docker login --username AWS --password-stdin "$REGISTRY" >&2

# 5. Build. The Dockerfile pattern in this repo expects to run from the
# repo root with the dockerfile referenced relatively (paths inside the
# Dockerfile are repo-rooted COPY directives like `COPY backend/src ...`).
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "$REPO_ROOT"

BUILD_ARGS=( -f "$DOCKERFILE" -t "$ECR_REPO:$TAG" )
if [[ -n "$PLATFORM" ]]; then
    BUILD_ARGS+=( --platform "$PLATFORM" )
fi
docker build "${BUILD_ARGS[@]}" . >&2

# 6. Push.
docker push "$ECR_REPO:$TAG" >&2

log "Pushed $ECR_REPO:$TAG"
echo "$TAG"
