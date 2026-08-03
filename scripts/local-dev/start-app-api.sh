#!/usr/bin/env bash
# Start app-api for local development, bound to loopback only.
#
# Run this INSIDE the dev container (it needs uv and the project venv):
#   docker exec -d agentcore-dev bash -lc 'scripts/local-dev/start-app-api.sh'
#
# Why loopback: local dev normally runs with SKIP_AUTH=true, which makes every
# session-authenticated route return a fake admin. `backend/src/apis/app_api/main.py`
# hardcodes host="0.0.0.0" when run as `python main.py`, so this script invokes
# uvicorn directly with --host 127.0.0.1 to keep that bypass unreachable from
# outside the container. Do not "fix" this to 0.0.0.0.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${REPO_ROOT}/backend/src/.env"
HOST="${APP_API_HOST:-127.0.0.1}"
PORT="${APP_API_PORT:-8000}"
LOG_FILE="${APP_API_LOG:-/tmp/app-api.log}"

if [ ! -f "${ENV_FILE}" ]; then
    echo "[ERROR] ${ENV_FILE} not found." >&2
    echo "        Copy backend/src/.env.example and fill in your environment's" >&2
    echo "        table names, or generate it from a deployed task definition:" >&2
    echo "        aws ecs describe-task-definition --task-definition <prefix>-app-api-task \\" >&2
    echo "          --query 'taskDefinition.containerDefinitions[0].environment[].[name,value]' --output text" >&2
    exit 1
fi

# Refuse to serve the SKIP_AUTH bypass on a non-loopback interface. app-api has
# its own CORS-based guard; this is the network-level counterpart.
if grep -qE '^SKIP_AUTH=true' "${ENV_FILE}" && [ "${HOST}" != "127.0.0.1" ] && [ "${HOST}" != "localhost" ]; then
    echo "[ERROR] SKIP_AUTH=true with APP_API_HOST=${HOST}." >&2
    echo "        That would expose an unauthenticated admin API. Refusing." >&2
    exit 1
fi

cd "${REPO_ROOT}/backend"
echo "[INFO] app-api -> http://${HOST}:${PORT}  (log: ${LOG_FILE})"
exec uv run uvicorn apis.app_api.main:app \
    --host "${HOST}" \
    --port "${PORT}" \
    --app-dir src \
    >"${LOG_FILE}" 2>&1
