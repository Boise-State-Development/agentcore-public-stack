#!/bin/bash
# Install npm dependencies for infrastructure/ (CDK).
#
# Single source of truth for the `npm ci` step that previously lived
# inline in scripts/{platform,backend}/{synth,deploy}.sh. Centralising
# it (a) removes four duplicate copies and (b) lets us keep the
# lockfile-check + reproducible-install policy in one auditable place.
#
# Idempotent: skips the install if node_modules/.installed is present
# AND the lockfile hash matches. Caches the lockfile sha256 inside
# the .installed marker so a lockfile change forces a reinstall.
#
# Usage:
#   scripts/cdk/install.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# shellcheck source=../common/load-env.sh
source "${PROJECT_ROOT}/scripts/common/load-env.sh" >/dev/null 2>&1 || true

INFRA_DIR="${PROJECT_ROOT}/infrastructure"
LOCKFILE="${INFRA_DIR}/package-lock.json"
MARKER="${INFRA_DIR}/node_modules/.installed"

log_info()  { echo "[INFO]  $1"; }
log_error() { echo "[ERROR] $1" >&2; }

if [ ! -f "${LOCKFILE}" ]; then
    log_error "Missing package-lock.json at ${LOCKFILE}"
    log_error "Refusing to run \`npm install\` — npm ci requires a committed lockfile."
    log_error "Run \`scripts/common/sync-version.sh\` (or commit a lockfile) first."
    exit 1
fi

LOCKFILE_HASH="$(sha256sum "${LOCKFILE}" | cut -d' ' -f1)"

if [ -f "${MARKER}" ] && [ "$(cat "${MARKER}")" = "${LOCKFILE_HASH}" ]; then
    log_info "infrastructure/ deps already installed for this package-lock.json — skipping."
    exit 0
fi

log_info "Installing infrastructure/ npm deps via \`npm ci --prefer-offline\`..."
cd "${INFRA_DIR}"
npm ci --prefer-offline

mkdir -p node_modules
echo "${LOCKFILE_HASH}" > "${MARKER}"

log_info "Done."
