#!/bin/bash
set -euo pipefail

# Script: Sync VERSION file into all package manifests
# Usage:
#   bash scripts/common/sync-version.sh          # Write VERSION into manifests
#   bash scripts/common/sync-version.sh --check   # Check for drift (exit non-zero if out of sync)

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION_FILE="${REPO_ROOT}/VERSION"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

# Validate VERSION file
if [ ! -f "${VERSION_FILE}" ]; then
    echo -e "${RED}[ERROR]${NC} VERSION file not found at ${VERSION_FILE}"
    exit 1
fi

VERSION=$(tr -d '[:space:]' < "${VERSION_FILE}")

if [ -z "${VERSION}" ]; then
    echo -e "${RED}[ERROR]${NC} VERSION file is empty"
    exit 1
fi

if ! [[ "${VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9.]+)?$ ]]; then
    echo -e "${RED}[ERROR]${NC} VERSION '${VERSION}' does not match SemVer format"
    exit 1
fi

# Target manifests
PYPROJECT="${REPO_ROOT}/backend/pyproject.toml"
FE_PKG="${REPO_ROOT}/frontend/ai.client/package.json"
INFRA_PKG="${REPO_ROOT}/infrastructure/package.json"
README="${REPO_ROOT}/README.md"

CHECK_MODE=false
if [ "${1:-}" = "--check" ]; then
    CHECK_MODE=true
fi

errors=0

sync_or_check() {
    local file="$1"
    local current="$2"
    local label="$3"
    local expected="${4:-${VERSION}}"

    if [ "${current}" = "${expected}" ]; then
        echo -e "${GREEN}[OK]${NC} ${label}: ${current}"
    elif [ "${CHECK_MODE}" = true ]; then
        echo -e "${RED}[DRIFT]${NC} ${label}: ${current} (expected ${expected})"
        errors=$((errors + 1))
    fi
}

# Read current versions
PY_VER=$(grep -oP '^version\s*=\s*"\K[^"]+' "${PYPROJECT}" || echo "")
FE_VER=$(grep -oP '"version"\s*:\s*"\K[^"]+' "${FE_PKG}" | head -1 || echo "")
INFRA_VER=$(grep -oP '"version"\s*:\s*"\K[^"]+' "${INFRA_PKG}" | head -1 || echo "")
README_BADGE_VER=$(grep -oP 'badge/Release-v\K[^-][^?]*(?=-)' "${README}" | head -1 | sed 's/--/-/g' || echo "")
README_CURRENT_VER=$(grep -oP '\*\*Current release:\*\* v\K.*' "${README}" | tr -d '[:space:]' || echo "")

# uv.lock uses PEP 440 format (e.g., 1.0.0b16 instead of 1.0.0-beta.16)
UV_LOCK="${REPO_ROOT}/backend/uv.lock"
UV_LOCK_VER=""
if [ -f "${UV_LOCK}" ]; then
    UV_LOCK_VER=$(sed -n '/name = "agentcore-stack"/,/^\[/{ /^version = /p }' "${UV_LOCK}" | grep -oP '"\K[^"]+' || echo "")
fi
# Convert SemVer prerelease to PEP 440 for comparison (e.g., 1.0.0-beta.16 → 1.0.0b16)
PEP440_VERSION=$(echo "${VERSION}" | sed -E 's/-alpha\./a/; s/-beta\./b/; s/-rc\./rc/')

if [ "${CHECK_MODE}" = true ]; then
    echo "Checking manifests against VERSION=${VERSION}..."
    sync_or_check "${PYPROJECT}" "${PY_VER}" "backend/pyproject.toml"
    sync_or_check "${FE_PKG}" "${FE_VER}" "frontend/ai.client/package.json"
    sync_or_check "${INFRA_PKG}" "${INFRA_VER}" "infrastructure/package.json"
    sync_or_check "${README}" "${README_BADGE_VER}" "README.md (badge)"
    sync_or_check "${README}" "${README_CURRENT_VER}" "README.md (current release)"
    if [ -f "${UV_LOCK}" ]; then
        sync_or_check "${UV_LOCK}" "${UV_LOCK_VER}" "backend/uv.lock" "${PEP440_VERSION}"
    fi

    if [ ${errors} -gt 0 ]; then
        echo -e "\n${RED}[FAIL]${NC} ${errors} manifest(s) out of sync. Run: bash scripts/common/sync-version.sh"
        exit 1
    else
        echo -e "\n${GREEN}[PASS]${NC} All manifests in sync."
        exit 0
    fi
fi

# Sync mode — update all manifests
echo "Syncing VERSION=${VERSION} into manifests..."

sed -i "s/^version = \".*\"/version = \"${VERSION}\"/" "${PYPROJECT}"
echo -e "${GREEN}[UPDATED]${NC} backend/pyproject.toml"

# Use a temp file approach for JSON to avoid jq dependency issues
sed -i "0,/\"version\": \"[^\"]*\"/s/\"version\": \"[^\"]*\"/\"version\": \"${VERSION}\"/" "${FE_PKG}"
echo -e "${GREEN}[UPDATED]${NC} frontend/ai.client/package.json"

sed -i "0,/\"version\": \"[^\"]*\"/s/\"version\": \"[^\"]*\"/\"version\": \"${VERSION}\"/" "${INFRA_PKG}"
echo -e "${GREEN}[UPDATED]${NC} infrastructure/package.json"

# README.md: version badge and "Current release" text
# shields.io uses -- for literal hyphens in badge text
BADGE_VERSION=$(echo "${VERSION}" | sed 's/-/--/g')
sed -i "s|badge/Release-v[^?]*|badge/Release-v${BADGE_VERSION}-6366f1|" "${README}"
sed -i "s|\*\*Current release:\*\* v.*|\*\*Current release:\*\* v${VERSION}|" "${README}"
echo -e "${GREEN}[UPDATED]${NC} README.md (badge + current release)"

# Regenerate lockfiles so they reflect the new version
echo -e "\nRegenerating lockfiles..."

# Backend: uv.lock (reflects version from pyproject.toml)
if command -v uv &>/dev/null; then
    (cd "${REPO_ROOT}/backend" && uv lock)
    echo -e "${GREEN}[UPDATED]${NC} backend/uv.lock"
else
    echo -e "${RED}[SKIP]${NC} backend/uv.lock (uv not installed — run: curl -LsSf https://astral.sh/uv/install.sh | sh)"
fi

# Frontend: package-lock.json
npm install --package-lock-only --prefix "${REPO_ROOT}/frontend/ai.client" 2>/dev/null
echo -e "${GREEN}[UPDATED]${NC} frontend/ai.client/package-lock.json"

# Infrastructure: package-lock.json
npm install --package-lock-only --prefix "${REPO_ROOT}/infrastructure" 2>/dev/null
echo -e "${GREEN}[UPDATED]${NC} infrastructure/package-lock.json"

echo -e "\n${GREEN}[DONE]${NC} All manifests and lockfiles updated to ${VERSION}"
