"""Pytest configuration for test suite."""

import os
import sys
from pathlib import Path

import pytest

# Ensure AWS region is set so that module-level boto3 calls don't fail
# during import (e.g. agents.main_agent.quota -> boto3.resource('dynamodb'))
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

# botocore >= 1.43 accesses Credentials.account_id during endpoint
# construction. On a RefreshableCredentials object (e.g. resolved from a
# real SSO profile) that property forces a credential _refresh() →
# GetRoleCredentials, which moto does not implement, so mocked AWS calls
# fail. Pin static dummy credentials so the chain builds a non-refreshable
# Credentials object instead. The matching AWS_PROFILE scrub is done
# per-test below (a process-wide pop here is not enough: tests that reload
# `apis.app_api.main` run load_dotenv(override=True), which re-injects
# AWS_PROFILE from backend/src/.env mid-suite). Mirrors moto's practice.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")

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


# Same load_dotenv(override=True) bleed as above, but for AWS_PROFILE.
# backend/src/.env sets a real SSO profile for local dev; once a test
# reloads `apis.app_api.main` it lands in process env and every later
# test that builds a boto3 client resolves SSO credentials. Under
# botocore >= 1.43 that fails all mocked AWS calls (see import-time note).
# Scrub per-test so the static dummy credentials win the provider chain.
_AWS_PROFILE_ENV_KEYS = (
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
)


@pytest.fixture(autouse=True)
def _clear_aws_profile_env():
    saved = {k: os.environ.pop(k, None) for k in _AWS_PROFILE_ENV_KEYS}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# Same load_dotenv(override=True) bleed again, for the infra-resource
# config families. backend/src/.env sets real DYNAMODB_*_TABLE_NAME and
# COGNITO_* identifiers for local dev; once a test reloads
# `apis.app_api.main` they land in process env. Repositories/services gate
# their "configured" flag on `param or os.getenv("DYNAMODB_..."/"COGNITO_...")`,
# so a leaked value makes "disabled when unconfigured" tests construct a
# live client and attempt real AWS calls. Tests always inject their own
# resource names via moto fixtures, so scrub the whole family per-test.
_ENV_CONFIG_BLEED_PREFIXES = (
    "DYNAMODB_",
    "COGNITO_",
)


@pytest.fixture(autouse=True)
def _clear_env_config_bleed():
    saved = {
        k: os.environ.pop(k)
        for k in list(os.environ)
        if k.startswith(_ENV_CONFIG_BLEED_PREFIXES)
    }
    try:
        yield
    finally:
        for k, v in saved.items():
            os.environ[k] = v



# ---------------------------------------------------------------------------
# Admin authorization overrides
# ---------------------------------------------------------------------------
#
# Admin routes are no longer guarded by a single `require_admin`: each admin
# router package declares its own `require_admin_scope(...)` dependency
# (delegated admin scopes, docs/specs/granular-admin-permissions.md). A test
# that mounts the root admin router therefore has several distinct dependency
# objects to satisfy, and overriding `require_admin` alone silently 401s.
#
# `override_admin_auth` overrides all of them at once, so a new admin area is
# covered automatically instead of breaking every route test that mounts the
# root router.


# The closures returned by the dependency factories in
# `apis/shared/auth/rbac.py`. Matching on qualname rather than importing the
# module-level names is deliberate: `test_skills_feature_flag.py` reloads
# `apis.app_api.admin.routes`, which rebuilds every `require_*_admin` object in
# it. A test app built before that reload still holds the *old* dependency
# objects, so an import-based override list silently stops matching and every
# request 401s. Reading the dependencies off the app can't go stale.
_AUTH_CHECKER_QUALNAMES = frozenset(
    {
        "require_app_roles.<locals>.checker",   # includes require_admin
        "require_admin_scope.<locals>.checker",
    }
)


def _walk_dependants(dependant):
    """Yield a route's dependency tree, sub-dependencies included."""
    for dep in dependant.dependencies:
        yield dep
        yield from _walk_dependants(dep)


def override_admin_auth(app, impl) -> None:
    """Point every admin authorization dependency on ``app`` at ``impl``.

    Walks the app's own routes, so it covers whichever admin areas the test
    mounted and picks up new ones for free.

    Note it targets only the *authorization closures*, not wrappers built on
    them. `require_marketplace_admin` is a wrapper that also enforces the
    AGENT_MARKETPLACE_ENABLED kill switch (404 when off); overriding it would
    authorize the caller *and* silently disable the kill switch, so a test
    asserting the disabled behavior would sail straight past it. Overriding the
    scope check it wraps leaves the wrapper running.

    Args:
        app: The FastAPI app under test.
        impl: A zero-arg callable returning the User to inject — or one that
            raises, to exercise the denied path.

    Raises:
        AssertionError: if the app exposes no authorization dependency at all,
            which means the test would have 401'd for a non-obvious reason.
    """
    matched = 0

    for route in app.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        for dep in _walk_dependants(dependant):
            call = dep.call
            if call is None:
                continue
            if getattr(call, "__qualname__", "") in _AUTH_CHECKER_QUALNAMES:
                app.dependency_overrides[call] = impl
                matched += 1

    assert matched, (
        "override_admin_auth found no authorization dependency on this app — "
        "did the router fail to mount, or was the app built before a module "
        "reload replaced its dependencies?"
    )
