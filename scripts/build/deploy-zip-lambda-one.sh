#!/usr/bin/env bash
#============================================================
# deploy-zip-lambda-one.sh — content-hash-aware zip-Lambda code
# deploy for a single Lambda.
#
# Mirrors build-one.sh's per-service spec pattern, but for
# zip-based Lambdas where CDK ships a stable bootstrap and the
# real handler code is deployed via `aws lambda update-function-code`.
#
# Usage:
#   deploy-zip-lambda-one.sh <service>
#
# Where <service> is one of:
#   artifact-render
#
# (Future zip-Lambdas — Gateway tool Lambdas in mcp-servers, etc. —
# would add a case-branch here.)
#============================================================
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <service>" >&2
    echo "  service: artifact-render" >&2
    exit 1
fi

SERVICE="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DEPLOY="${SCRIPT_DIR}/deploy-lambda-code-if-changed.sh"

source "${PROJECT_ROOT}/scripts/common/load-env.sh"
export AWS_REGION="${CDK_AWS_REGION}"

# Per-service spec.
case "$SERVICE" in
    artifact-render)
        SOURCE_DIR="backend/src/lambdas/artifact_render"
        FUNCTION_NAME_SSM="/${CDK_PROJECT_PREFIX}/artifacts/render-function-name"
        CODE_HASH_SSM="/${CDK_PROJECT_PREFIX}/artifacts/render-code-hash"
        CODE_SHA256_SSM="/${CDK_PROJECT_PREFIX}/artifacts/render-code-sha256"
        ;;
    *)
        echo "Unknown service: $SERVICE" >&2
        echo "Expected one of: artifact-render" >&2
        exit 1
        ;;
esac

cd "$PROJECT_ROOT"
log_info "Deploying ${SERVICE} code..."

HASH="$(bash "$DEPLOY" \
    --service "$SERVICE" \
    --source-dir "$SOURCE_DIR" \
    --function-name-ssm "$FUNCTION_NAME_SSM" \
    --code-hash-ssm "$CODE_HASH_SSM" \
    --code-sha256-ssm "$CODE_SHA256_SSM")"

log_info "${SERVICE}: ${HASH}"

# Publish to GITHUB_OUTPUT so the workflow surfaces the hash.
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "code_hash=${HASH}" >> "$GITHUB_OUTPUT"
fi
