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

# Angular's @angular/build:application builder (default since v17)
# emits the SPA into a `browser/` subdirectory of outputPath. The
# index.html, main bundle, and asset hashes all live under
# dist/ai.client/browser/, NOT dist/ai.client/. Syncing the parent
# would put index.html at s3://bucket/browser/index.html and serve
# 403 Access Denied to anyone hitting `/` (CloudFront's
# defaultRootObject = index.html resolves to /index.html, no key).
SRC="$SCRIPT_DIR/../../frontend/ai.client/dist/ai.client/browser/"

# Content-hashed bundle names, e.g. main-ZMUT4FS2.js, chunk-4DOJ3VBG.js,
# styles-DIRZRL6Y.css. `outputHashing: "all"` gives every builder output an
# 8-character uppercase hash, so the NAME changes whenever the bytes do.
#
# The pattern is deliberately narrow — a hash, not merely a `.js` suffix.
# `public/` is copied verbatim and is NOT hashed, and it contains
# `audio/pcm-capture.worklet.js`: a rule keyed on the extension would pin
# that worklet, unversioned, for a year. Anything this pattern fails to
# match simply falls through to the revalidate pass below, so the failure
# direction is a slower asset, never a stuck one.
HASHED_JS='*-[A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9].js'
HASHED_CSS='*-[A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9].css'

# Pass 1 — the hashed bundles, pinned hard. Safe precisely because the name
# is a function of the content: a changed bundle is a different object, so
# nothing can serve a stale one under a name someone already holds.
#
# No --delete here; that belongs to the pass that sees the whole tree.
echo "Syncing hashed bundles to s3://${BUCKET_NAME}... (immutable)"
aws s3 sync "$SRC" "s3://${BUCKET_NAME}/" --region "$AWS_REGION" \
  --exclude "*" --include "$HASHED_JS" --include "$HASHED_CSS" \
  --cache-control "public, max-age=31536000, immutable"

# Pass 2 — everything else, above all index.html, which must be revalidated
# on every load. Served with NO Cache-Control (the bug this replaces),
# browsers fall back to HEURISTIC freshness — roughly 10% of the document's
# age when they cached it — so a tab that loaded the shell before a deploy
# could keep using it, and the old hashed bundles it names, for hours. The
# deploy looks like it silently did not happen. `no-cache` means "cache it,
# but check first": the revalidation is a 304 on a 32KB file.
#
# ORDER MATTERS. This pass has no excludes, so it also considers the hashed
# bundles — but `aws s3 sync` re-uploads only when size differs or the local
# file is newer, and pass 1 just uploaded them, so they are skipped and keep
# their immutable header. Running these two the other way round would stamp
# every bundle `no-cache`.
#
# Sweeping the whole tree is also what keeps --delete honest: filters apply
# to the delete evaluation too, so excluding the bundles here would exempt
# stale ones from the prune and the bucket would grow forever.
echo "Syncing index.html and unhashed assets... (revalidate)"
aws s3 sync "$SRC" "s3://${BUCKET_NAME}/" --delete --region "$AWS_REGION" \
  --cache-control "no-cache"

echo "Invalidating CloudFront distribution ${DISTRIBUTION_ID}..."
aws cloudfront create-invalidation \
  --distribution-id "$DISTRIBUTION_ID" \
  --paths "/*" --query 'Invalidation.Id' --output text

echo "Frontend deploy complete."
