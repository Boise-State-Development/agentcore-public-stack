"""Pytest configuration for test suite."""

import os
import sys
from pathlib import Path

import pytest

# Ensure AWS region is set so that module-level boto3 calls don't fail
# during import (e.g. agents.main_agent.quota -> boto3.resource('dynamodb'))
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

# Add backend/src to Python path for imports
# This file is in backend/tests/, so we need to go up one level to backend/
BACKEND_DIR = Path(__file__).parent.parent
SRC_DIR = BACKEND_DIR / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


# Scrub SKIP_AUTH bleed from local .env. Some tests reload
# `apis.app_api.main`, which calls `load_dotenv(override=True)` and
# clobbers process env with whatever `backend/src/.env` has set —
# typically `SKIP_AUTH=true` for local dev. Without this fixture every
# auth-aware test downstream of that reload returns the fake bypass
# user. Tests that need SKIP_AUTH on can still set it via monkeypatch
# (test-local setenv runs after this autouse delenv).
#
# Manages os.environ directly rather than depending on monkeypatch so
# this autouse fixture doesn't perturb fixture-teardown ordering for
# tests that already use monkeypatch + their own autouse fixtures
# (e.g. tests/apis/app_api/test_connectors_routes.py).
_SKIP_AUTH_ENV_KEYS = (
    "SKIP_AUTH",
    "SKIP_AUTH_ROLES",
    "SKIP_AUTH_USER_ID",
    "SKIP_AUTH_EMAIL",
)


@pytest.fixture(autouse=True)
def _clear_skip_auth_env():
    saved = {k: os.environ.pop(k, None) for k in _SKIP_AUTH_ENV_KEYS}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

