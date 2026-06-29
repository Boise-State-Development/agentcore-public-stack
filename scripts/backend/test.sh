#!/bin/bash
set -euo pipefail

# scripts/backend/test.sh — run the backend (App API) pytest suite WITH coverage.
#
# Ported from the pre-#396 scripts/stack-app-api/test.sh during the
# platform-as-bootstrap refactor: the per-stack script dirs (stack-app-api,
# stack-frontend, …) were deleted and replaced by the single-stack layout,
# but nightly.yml was never repointed. repo-shape.test.ts now FORBIDS
# recreating scripts/stack-*, so the coverage variant lives here instead.
#
# This is the COVERAGE variant used by nightly.yml's "Test Backend with
# Coverage" job (it emits backend/coverage.json + backend/htmlcov/ which the
# workflow uploads as the `backend-coverage` artifact and feeds to the
# coverage-analysis jobs). The plain, no-coverage gate used by
# nightly-deploy-pipeline.yml / backend.yml stays inline (`uv run pytest`).
#
# Runs on a fresh, isolated GitHub runner: the uv *cache* (~/.cache/uv +
# backend/.venv) is restored by the install-backend job, but the uv *binary*
# is not, so this script installs uv if it's missing.

# Get the directory of this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BACKEND_DIR="${PROJECT_ROOT}/backend"

# Logging functions
log_info() {
    echo "[INFO] $1"
}

log_error() {
    echo "[ERROR] $1" >&2
}

log_success() {
    echo "[SUCCESS] $1"
}

main() {
    log_info "Running App API tests..."

    # Change to backend directory
    cd "${BACKEND_DIR}"
    log_info "Working directory: $(pwd)"

    # Install uv if not present (the runner is fresh; only the cache persists)
    if ! command -v uv &> /dev/null; then
        log_info "Installing uv..."
        curl -LsSf https://astral.sh/uv/0.7.12/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
    fi

    # Sync dependencies from lock file (includes dev deps for testing)
    log_info "Syncing dependencies from uv.lock..."
    uv sync --frozen --extra agentcore --extra dev

    # Verify installation
    log_info "Verifying installation..."
    uv run python -c "import fastapi; import uvicorn; print('Core dependencies installed')"

    # Run tests
    log_info "Executing tests..."

    if [ ! -d "tests" ]; then
        log_info "No tests/ directory found. Skipping tests."
        log_success "App API tests completed successfully!"
        return 0
    fi

    # Set PYTHONPATH explicitly
    export PYTHONPATH="${BACKEND_DIR}/src:${PYTHONPATH:-}"
    log_info "PYTHONPATH=${PYTHONPATH}"

    # Set dummy AWS credentials for tests
    export AWS_DEFAULT_REGION=us-east-1
    export AWS_ACCESS_KEY_ID=testing
    export AWS_SECRET_ACCESS_KEY=testing

    # Test import directly
    log_info "Testing direct import..."
    uv run python -c "from agents.main_agent.quota.checker import QuotaChecker; print('Direct import works')"

    # Run pytest with coverage (html + json + term reports)
    log_info "Running pytest..."
    uv run python -m pytest tests/ \
        -v \
        --tb=short \
        --color=yes \
        --disable-warnings \
        --cov=src \
        --cov-report=html \
        --cov-report=json \
        --cov-report=term

    log_success "App API tests completed successfully!"
}

main "$@"
