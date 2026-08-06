#!/usr/bin/env bash
# Run the TUI against a locally running app-api.
#
# Interactive, so it needs a TTY:
#   docker exec -it agentcore-dev bash -lc 'scripts/local-dev/run-tui.sh'
#
# Pass anything through to the CLI, e.g.:
#   docker exec agentcore-dev bash -lc 'scripts/local-dev/run-tui.sh status'
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KEY_FILE="${AGENTCORE_KEY_FILE:-${HOME}/.config/agentcore-tui/local-api-key}"

export AGENTCORE_BASE_URL="${AGENTCORE_BASE_URL:-http://127.0.0.1:8000}"

# Env var wins if already set (useful for pointing at a deployed environment).
if [ -z "${AGENTCORE_API_KEY:-}" ]; then
    if [ ! -f "${KEY_FILE}" ]; then
        echo "[ERROR] no API key. Run scripts/local-dev/mint-api-key.sh first," >&2
        echo "        or export AGENTCORE_API_KEY yourself." >&2
        exit 1
    fi
    AGENTCORE_API_KEY="$(cat "${KEY_FILE}")"
    export AGENTCORE_API_KEY
fi

# Textual needs a capable TERM; the container default is often bare.
export TERM="${TERM:-xterm-256color}"
export COLORTERM="${COLORTERM:-truecolor}"

cd "${REPO_ROOT}/tui"
exec uv run agentcore-tui "$@"
