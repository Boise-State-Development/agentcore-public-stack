#!/usr/bin/env bash
#============================================================
# build-all-images.sh — local convenience wrapper that builds &
# pushes all three backend Docker images, skipping unchanged ones.
#
# In CI, the workflow runs each service in its own job (so the
# dashboard shows three independent build statuses). For local
# development this script just loops over them sequentially —
# easier to invoke than three separate commands.
#
# All the per-service spec (Dockerfile, source trees, manifests,
# platform) lives in scripts/build/build-one.sh; this file is
# intentionally a thin loop so spec changes only happen in one
# place.
#
# Usage:
#   bash scripts/build/build-all-images.sh
#
# Required env (resolved by scripts/common/load-env.sh, which
# build-one.sh sources internally):
#   CDK_PROJECT_PREFIX, CDK_AWS_REGION, CDK_AWS_ACCOUNT
#============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ONE="${SCRIPT_DIR}/build-one.sh"

for service in app-api inference-api rag-ingestion; do
    bash "$BUILD_ONE" "$service"
done
