#!/usr/bin/env bash
# Mint an API key from a locally running app-api and stash it for the TUI.
#
#   docker exec agentcore-dev bash -lc 'scripts/local-dev/mint-api-key.sh'
#
# How this works without a browser: POST /auth/api-keys is a *session*
# authenticated route, and SKIP_AUTH=true makes that dependency return a fake
# admin whose identity comes from SKIP_AUTH_USER_ID. So the local backend can
# mint a real key — written to the real api-keys table — for that user.
#
# The key is written to a 0600 file and never echoed. Two consequences worth
# knowing:
#   * The raw value is shown by the API exactly once; this file is the only copy.
#   * Creating a key REVOKES any existing key for that user. If SKIP_AUTH_USER_ID
#     is a human who uses the web UI's API keys, this breaks their key.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${REPO_ROOT}/backend/src/.env"
BASE_URL="${APP_API_URL:-http://127.0.0.1:8000}"
KEY_NAME="${KEY_NAME:-local-tui}"
KEY_FILE="${AGENTCORE_KEY_FILE:-${HOME}/.config/agentcore-tui/local-api-key}"

if ! grep -qE '^SKIP_AUTH=true' "${ENV_FILE}" 2>/dev/null; then
    echo "[ERROR] SKIP_AUTH=true is not set in ${ENV_FILE}." >&2
    echo "        Without it, POST /auth/api-keys needs a browser OIDC session." >&2
    exit 1
fi

target_user="$(grep -E '^SKIP_AUTH_USER_ID=' "${ENV_FILE}" | cut -d= -f2- || true)"
echo "[INFO] minting key '${KEY_NAME}' for user '${target_user:-local-dev}' via ${BASE_URL}"
echo "[WARN] this revokes any existing API key for that user"

response="$(curl -sS -m 30 -X POST "${BASE_URL}/auth/api-keys" \
    -H 'Content-Type: application/json' \
    -d "{\"name\":\"${KEY_NAME}\"}")"

# Extract the `key` field without printing the response (which contains it).
raw_key="$(printf '%s' "${response}" | sed -E 's/.*"key"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')"
key_id="$(printf '%s' "${response}" | sed -E 's/.*"key_id"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')"

if [ -z "${raw_key}" ] || [ "${raw_key}" = "${response}" ]; then
    echo "[ERROR] could not parse a key from the response." >&2
    echo "        Field names seen: $(printf '%s' "${response}" | tr ',' '\n' | sed -E 's/[{}"]//g; s/:.*//' | tr '\n' ' ')" >&2
    exit 1
fi

mkdir -p "$(dirname "${KEY_FILE}")"
printf '%s' "${raw_key}" > "${KEY_FILE}"
chmod 600 "${KEY_FILE}"
unset raw_key response

echo "[OK]   key_id ${key_id}"
echo "[OK]   value written to ${KEY_FILE} (0600, not echoed)"
echo "[NEXT] scripts/local-dev/run-tui.sh"
