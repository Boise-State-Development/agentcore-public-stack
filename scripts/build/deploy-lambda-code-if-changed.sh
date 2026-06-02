#!/usr/bin/env bash
#============================================================
# deploy-lambda-code-if-changed.sh — content-hash-aware Lambda
# zip code deploy.
#
# Computes a content hash of a Lambda's source directory, compares
# it to a published value in SSM, and only runs `aws lambda
# update-function-code` if the hash has changed.
#
# This is the zip-Lambda equivalent of build-and-push-if-changed.sh
# (which handles Docker-image Lambdas + ECS task images). The two
# script shapes are intentionally parallel — both:
#   1. Compute a deterministic content hash of the inputs.
#   2. Compare to whatever's already in AWS (ECR / SSM-tracked).
#   3. Skip the deploy if nothing changed.
#   4. Wait for AWS to reach a steady state if a deploy was needed.
#
# Why a hash + SSM tracker instead of just always uploading?
#   Because PlatformStack's bootstrap-zip pattern depends on the
#   Lambda's `Code` property never appearing to drift in CFN's eyes.
#   `update-function-code` modifies the actual Lambda but doesn't
#   touch the CFN model; CFN's stored `Code: { S3Bucket, S3Key }`
#   stays at the bootstrap value forever. As long as we only call
#   `update-function-code` when the source actually changed, we
#   minimise the gap between live state and CFN state to "the live
#   code is whatever the workflow last shipped, the CFN model is
#   the bootstrap." (Drift detection would surface this if anyone
#   ran it manually, but normal stack updates leave the Lambda
#   alone.)
#
# Usage:
#   deploy-lambda-code-if-changed.sh \
#     --service        artifact-render \
#     --source-dir     backend/src/lambdas/artifact_render \
#     --function-name-ssm  /ai-sbmt-api/artifacts/render-function-name \
#     --code-hash-ssm      /ai-sbmt-api/artifacts/render-code-hash
#
# Required env:
#   AWS_REGION          (e.g., us-west-2)
#============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPUTE_HASH="${SCRIPT_DIR}/compute-content-hash.sh"

SERVICE=""
SOURCE_DIR=""
FUNCTION_NAME_SSM=""
CODE_HASH_SSM=""

usage() {
    cat <<EOF >&2
Usage: $0 --service NAME --source-dir DIR \\
          --function-name-ssm PATH --code-hash-ssm PATH
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --service)            SERVICE="$2"; shift 2 ;;
        --source-dir)         SOURCE_DIR="$2"; shift 2 ;;
        --function-name-ssm)  FUNCTION_NAME_SSM="$2"; shift 2 ;;
        --code-hash-ssm)      CODE_HASH_SSM="$2"; shift 2 ;;
        -h|--help)            usage ;;
        *)                    echo "Unknown arg: $1" >&2; usage ;;
    esac
done

[[ -n "$SERVICE"           ]] || { echo "missing --service" >&2; usage; }
[[ -n "$SOURCE_DIR"        ]] || { echo "missing --source-dir" >&2; usage; }
[[ -n "$FUNCTION_NAME_SSM" ]] || { echo "missing --function-name-ssm" >&2; usage; }
[[ -n "$CODE_HASH_SSM"     ]] || { echo "missing --code-hash-ssm" >&2; usage; }
[[ -n "${AWS_REGION:-}"    ]] || { echo "AWS_REGION env var required" >&2; exit 2; }
[[ -d "$SOURCE_DIR"        ]] || { echo "source-dir not found: $SOURCE_DIR" >&2; exit 2; }

log() { echo "[$SERVICE] $*" >&2; }

# 1. Compute content hash of the source directory.
# We reuse compute-content-hash.sh by giving it the source dir as
# both the dockerfile-equivalent (the handler entry point) and the
# source-dir. The script requires --dockerfile, so we pass the
# handler.py as the "manifest" (it's what CDK uses as the entry
# point, equivalent to a Dockerfile in the build sense).
HANDLER_FILE="${SOURCE_DIR}/handler.py"
[[ -f "$HANDLER_FILE" ]] || { echo "expected handler.py at $HANDLER_FILE" >&2; exit 2; }

log "Computing content hash of $SOURCE_DIR..."
HASH="$(bash "$COMPUTE_HASH" \
    --dockerfile "$HANDLER_FILE" \
    --source-dir "$SOURCE_DIR")"
log "Content hash: $HASH"

# 2. Compare with the published hash in SSM. Missing parameter
# (first deploy) counts as "changed".
PUBLISHED_HASH=""
if PUBLISHED_HASH="$(aws ssm get-parameter \
        --region "$AWS_REGION" \
        --name "$CODE_HASH_SSM" \
        --query 'Parameter.Value' \
        --output text 2>/dev/null)"; then
    log "Published hash: $PUBLISHED_HASH"
else
    log "No published hash yet (first deploy)."
    PUBLISHED_HASH=""
fi

if [[ "$HASH" == "$PUBLISHED_HASH" ]]; then
    log "Source unchanged since last deploy — skipping update-function-code."
    echo "$HASH"
    exit 0
fi

log "Source has changed — deploying."

# 3. Resolve the function name from SSM. The Lambda's name is
# CDK-auto-generated to avoid orphan-collisions, so we can't hard-
# code it here.
FUNCTION_NAME="$(aws ssm get-parameter \
    --region "$AWS_REGION" \
    --name "$FUNCTION_NAME_SSM" \
    --query 'Parameter.Value' \
    --output text)"
log "Function name: $FUNCTION_NAME"

# 4. Zip the source directory. We run from inside the dir so the
# zip entries are relative — Lambda extracts them at the runtime's
# working directory and `handler.handler` resolves correctly.
TMP_ZIP="$(mktemp -t "${SERVICE}-XXXXXX.zip")"
trap 'rm -f "$TMP_ZIP"' EXIT
# mktemp creates the file empty; zip would interpret an existing
# empty file as a malformed archive and exit 3. Remove it first
# so zip starts from a clean slate. The trap above still cleans
# up the new file zip writes.
rm -f "$TMP_ZIP"
log "Zipping source to $TMP_ZIP..."
(cd "$SOURCE_DIR" && zip -r -q -X "$TMP_ZIP" . -x '__pycache__/*' '*.pyc' '.DS_Store')

# 5. Wait for the function to be in a state where update-function-code
# is accepted. After CDK creates the function or any other update,
# AWS reports State=Active|LastUpdateStatus=Successful within seconds
# but the actual gate is LastUpdateStatus.
log "Waiting for function to be ready for update..."
aws lambda wait function-updated \
    --region "$AWS_REGION" \
    --function-name "$FUNCTION_NAME" >&2 || {
        # `wait function-updated` returns non-zero only on InvalidState,
        # which means the function doesn't exist or is in a fundamentally
        # broken state. Surface the real status to help debugging.
        aws lambda get-function-configuration \
            --region "$AWS_REGION" \
            --function-name "$FUNCTION_NAME" \
            --query '{State:State,LastUpdateStatus:LastUpdateStatus,StateReason:StateReason}' >&2 || true
        exit 3
    }

# 6. Update.
log "Calling aws lambda update-function-code..."
aws lambda update-function-code \
    --region "$AWS_REGION" \
    --function-name "$FUNCTION_NAME" \
    --zip-file "fileb://${TMP_ZIP}" \
    --no-cli-pager \
    --output text \
    --query 'FunctionArn' >/dev/null

# 7. Wait for the update to complete.
log "Waiting for update to settle..."
aws lambda wait function-updated \
    --region "$AWS_REGION" \
    --function-name "$FUNCTION_NAME" >&2

# 8. Publish the new hash so the next run can short-circuit.
log "Publishing new hash to $CODE_HASH_SSM..."
aws ssm put-parameter \
    --region "$AWS_REGION" \
    --name "$CODE_HASH_SSM" \
    --value "$HASH" \
    --type String \
    --overwrite \
    --no-cli-pager >/dev/null

log "Done. New code hash: $HASH"
echo "$HASH"
