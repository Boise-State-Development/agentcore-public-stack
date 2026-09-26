#!/usr/bin/env bash
# scripts/frontend/build.sh — build the Angular SPA for one environment.
#
# SPA_BUILD_CONFIGURATION picks the angular.json build configuration, and with it
# the environment file (and so the front-end feature switches, see
# frontend/ai.client/src/environments/feature-flags.ts):
#   production  → environment.production.ts (default: prod, and any local build)
#   dev-deploy  → environment.development.ts (the deployed dev site)
# Each deploy workflow sets it to match the environment it deploys to.
# Preserves the gen-version.js prebuild fix from beta.27.
#
# This is a pure static-bundle build: it runs npm ci + ng build and
# never touches AWS. Tell load-env.sh to skip the AWS account / region
# validation so the build job doesn't have to plumb deploy-only env
# vars through the workflow's job-level env block.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LOAD_ENV_SKIP_AWS_VALIDATION=true
source "$SCRIPT_DIR/../common/load-env.sh"

cd "$SCRIPT_DIR/../../frontend/ai.client"
npm ci --prefer-offline

# Run gen-version.js explicitly (the npm prebuild hook doesn't fire
# when ng build is invoked directly by scripts).
node scripts/gen-version.js || true

CONFIGURATION="${SPA_BUILD_CONFIGURATION:-production}"
case "$CONFIGURATION" in
  production|dev-deploy) ;;
  *) echo "ERROR: SPA_BUILD_CONFIGURATION must be 'production' or 'dev-deploy' (got '$CONFIGURATION')" >&2; exit 1 ;;
esac
echo "Building the SPA with the '$CONFIGURATION' configuration"
npm run build -- --configuration "$CONFIGURATION"
