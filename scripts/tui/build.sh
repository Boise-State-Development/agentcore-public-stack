#!/usr/bin/env bash
# Build the terminal client (aps-tui) as a wheel and sdist, with checksums.
#
# The TUI is not published to a package index. It is attached to the GitHub
# Release for the repo version (see .github/workflows/release.yml), and users
# install the wheel by URL:
#
#   uv tool install https://github.com/<org>/<repo>/releases/download/v<ver>/aps_tui-<ver>-py3-none-any.whl
#
# The wheel is the same for every deployment: nothing in it names a host. The
# base URL is given at `aps-tui login --base-url ...`.
#
# Usage: scripts/tui/build.sh [--check]
#   --check   also run the test suite, lint and type check before building
#
# Output: tui/dist/aps_tui-<ver>-py3-none-any.whl, aps_tui-<ver>.tar.gz, SHA256SUMS

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TUI_DIR="${REPO_ROOT}/tui"
CHECK=false
[ "${1:-}" = "--check" ] && CHECK=true

command -v uv >/dev/null || { echo "error: uv is required (https://docs.astral.sh/uv/)" >&2; exit 1; }

cd "${TUI_DIR}"

VERSION="$(tr -d '[:space:]' < "${REPO_ROOT}/VERSION")"
MANIFEST_VERSION="$(uv version --short)"
if [ "${VERSION}" != "${MANIFEST_VERSION}" ]; then
    echo "error: VERSION is ${VERSION} but tui/pyproject.toml is ${MANIFEST_VERSION}; run scripts/common/sync-version.sh" >&2
    exit 1
fi

if [ "${CHECK}" = true ]; then
    echo "==> tests, lint, types"
    uv sync --all-extras --all-groups --quiet
    uv run pytest -q -p no:cacheprovider
    uv run ruff check src tests scripts
    uv run black --check src tests scripts
    uv run mypy src
fi

echo "==> building aps-tui ${VERSION}"
rm -rf dist
uv build --quiet

echo "==> smoke test: install the wheel into an empty environment"
WHEEL="$(ls dist/aps_tui-*.whl)"
REPORTED="$(uv run --isolated --no-project --with "${WHEEL}" aps-tui --version)"
[ "${REPORTED}" = "aps-tui ${VERSION}" ] || { echo "error: wheel reports '${REPORTED}'" >&2; exit 1; }

(cd dist && sha256sum aps_tui-* > SHA256SUMS)
echo "==> built:"
ls -1 dist/aps_tui-* dist/SHA256SUMS
