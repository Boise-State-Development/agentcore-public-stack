"""Tests for the SKIP_AUTH=true local-dev bypass.

Covers:
- `_skip_auth_user()` returns None when disabled, fake User when enabled
- All three auth dependencies bypass when SKIP_AUTH=true
- `_validate_skip_auth_or_raise()` accepts localhost-only CORS_ORIGINS,
  rejects empty CORS_ORIGINS, rejects any non-localhost origin
- The CI-guard regex matches realistic leak strings and skips the
  legitimate references in dependencies.py / main.py
"""

import importlib
import re
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from apis.shared.auth.dependencies import (
    _skip_auth_user,
    get_current_user,
    get_current_user_from_session,
    get_current_user_trusted,
)
from apis.shared.auth.models import User


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_skip_auth_env(monkeypatch):
    """Clear all SKIP_AUTH_* env vars so each test starts from a known state."""
    for key in (
        "SKIP_AUTH",
        "SKIP_AUTH_ROLES",
        "SKIP_AUTH_USER_ID",
        "SKIP_AUTH_EMAIL",
        "CORS_ORIGINS",
    ):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# _skip_auth_user
# ---------------------------------------------------------------------------


class TestSkipAuthUser:
    """Tests for the `_skip_auth_user()` helper."""

    def test_returns_none_when_unset(self, clean_skip_auth_env):
        assert _skip_auth_user() is None

    @pytest.mark.parametrize("value", ["false", "0", "", "no", "FALSE"])
    def test_returns_none_when_falsey(self, clean_skip_auth_env, monkeypatch, value):
        monkeypatch.setenv("SKIP_AUTH", value)
        assert _skip_auth_user() is None

    @pytest.mark.parametrize("value", ["true", "TRUE", "True"])
    def test_returns_user_when_true(self, clean_skip_auth_env, monkeypatch, value):
        monkeypatch.setenv("SKIP_AUTH", value)
        user = _skip_auth_user()
        assert isinstance(user, User)
        assert user.user_id == "local-dev"
        assert user.email == "dev@local"
        assert user.name == "Local Dev"
        assert user.roles == ["admin"]

    def test_overrides_via_env(self, clean_skip_auth_env, monkeypatch):
        monkeypatch.setenv("SKIP_AUTH", "true")
        monkeypatch.setenv("SKIP_AUTH_USER_ID", "phil")
        monkeypatch.setenv("SKIP_AUTH_EMAIL", "phil@example.com")
        monkeypatch.setenv("SKIP_AUTH_ROLES", "admin,DotNetDevelopers, ,QA ")

        user = _skip_auth_user()
        assert user is not None
        assert user.user_id == "phil"
        assert user.email == "phil@example.com"
        # whitespace-only entries filtered, surrounding whitespace stripped
        assert user.roles == ["admin", "DotNetDevelopers", "QA"]


# ---------------------------------------------------------------------------
# Dependency bypass
# ---------------------------------------------------------------------------


class TestDependencyBypass:
    """Tests that the bypass short-circuits each auth dependency."""

    @pytest.mark.asyncio
    async def test_get_current_user_bypassed(self, clean_skip_auth_env, monkeypatch):
        monkeypatch.setenv("SKIP_AUTH", "true")
        # Patch the validator to assert it's never consulted.
        with patch(
            "apis.shared.auth.dependencies._get_cognito_validator"
        ) as mock_get:
            user = await get_current_user(credentials=None)
            assert mock_get.call_count == 0
        assert user.user_id == "local-dev"
        assert user.roles == ["admin"]

    @pytest.mark.asyncio
    async def test_get_current_user_from_session_bypassed(
        self, clean_skip_auth_env, monkeypatch
    ):
        monkeypatch.setenv("SKIP_AUTH", "true")
        # Build a request whose state.bff_session is unset; without the
        # bypass this would 401, with the bypass we get the fake user.
        request = MagicMock()
        request.state = MagicMock(spec=[])  # no bff_session attr

        user = await get_current_user_from_session(request)
        assert user.user_id == "local-dev"

    @pytest.mark.asyncio
    async def test_get_current_user_trusted_bypassed(
        self, clean_skip_auth_env, monkeypatch
    ):
        monkeypatch.setenv("SKIP_AUTH", "true")
        # No credentials supplied — without the bypass this 401s.
        user = await get_current_user_trusted(credentials=None)
        assert user.user_id == "local-dev"

    @pytest.mark.asyncio
    async def test_get_current_user_still_401_when_disabled(
        self, clean_skip_auth_env
    ):
        """Sanity check: with SKIP_AUTH unset, missing credentials still 401."""
        with pytest.raises(HTTPException) as exc:
            await get_current_user(credentials=None)
        assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Startup guard
# ---------------------------------------------------------------------------


def _import_main_module():
    """Import (and reload) apis.app_api.main so it picks up current env.

    The module calls `load_dotenv(..., override=True)` at import time,
    which would clobber monkeypatched env vars on reload. Patch the
    upstream symbol so the `from dotenv import load_dotenv` re-binding
    inside the module reload also picks up the no-op.
    """
    with patch("dotenv.load_dotenv", lambda *a, **kw: None):
        import apis.app_api.main as m
        return importlib.reload(m)


class TestStartupGuard:
    """Tests for `_validate_skip_auth_or_raise()` in app_api/main.py."""

    def test_noop_when_skip_auth_off(self, clean_skip_auth_env):
        m = _import_main_module()
        # Doesn't raise even with no CORS_ORIGINS — guard is a no-op.
        m._validate_skip_auth_or_raise()

    @pytest.mark.parametrize(
        "origins",
        [
            "http://localhost:4200",
            "http://localhost:4200,http://127.0.0.1:8000",
            "http://[::1]:4200",
            "http://0.0.0.0:4200",
            "http://localhost:4200, http://127.0.0.1:8000 ",  # whitespace tolerated
        ],
    )
    def test_accepts_localhost_origins(
        self, clean_skip_auth_env, monkeypatch, origins
    ):
        monkeypatch.setenv("SKIP_AUTH", "true")
        monkeypatch.setenv("CORS_ORIGINS", origins)
        m = _import_main_module()
        m._validate_skip_auth_or_raise()  # no raise

    def test_rejects_empty_cors_origins(self, clean_skip_auth_env, monkeypatch):
        monkeypatch.setenv("SKIP_AUTH", "true")
        monkeypatch.setenv("CORS_ORIGINS", "")
        m = _import_main_module()
        with pytest.raises(RuntimeError, match="localhost"):
            m._validate_skip_auth_or_raise()

    def test_rejects_unset_cors_origins(self, clean_skip_auth_env, monkeypatch):
        monkeypatch.setenv("SKIP_AUTH", "true")
        # CORS_ORIGINS deliberately unset
        m = _import_main_module()
        with pytest.raises(RuntimeError, match="localhost"):
            m._validate_skip_auth_or_raise()

    @pytest.mark.parametrize(
        "origins",
        [
            "https://app.example.com",
            "http://localhost:4200,https://app.example.com",  # one bad apple
            "https://prod.boisestate.edu",
        ],
    )
    def test_rejects_non_localhost(
        self, clean_skip_auth_env, monkeypatch, origins
    ):
        monkeypatch.setenv("SKIP_AUTH", "true")
        monkeypatch.setenv("CORS_ORIGINS", origins)
        m = _import_main_module()
        with pytest.raises(RuntimeError, match="localhost"):
            m._validate_skip_auth_or_raise()


# ---------------------------------------------------------------------------
# CI-guard regex
# ---------------------------------------------------------------------------


class TestCIGuardPattern:
    """Tests that mirror the grep pattern in skip-auth-guard.yml.

    The CI workflow uses `grep -E` with this pattern; we validate the
    same regex against representative leak strings and the legitimate
    references in our own source so a future refactor of the workflow
    has a behavioral spec to test against.
    """

    # Mirrors the PATTERN in .github/workflows/skip-auth-guard.yml
    PATTERN = re.compile(r"""SKIP_AUTH[ \t]*[:=][ \t]*["']*true""")

    @pytest.mark.parametrize(
        "leak",
        [
            "SKIP_AUTH=true",
            'SKIP_AUTH: "true"',
            "SKIP_AUTH: true",
            "SKIP_AUTH:true",
            "SKIP_AUTH: 'true'",
            "  SKIP_AUTH = true",
            'SKIP_AUTH="true"',
            "ENV SKIP_AUTH=true",  # Dockerfile
            "      SKIP_AUTH: 'true'  # in some yaml",
        ],
    )
    def test_matches_leak_strings(self, leak):
        assert self.PATTERN.search(leak) is not None, f"missed leak: {leak!r}"

    @pytest.mark.parametrize(
        "benign",
        [
            'SKIP_AUTH = "false"',
            "SKIP_AUTH=false",
            "# Document SKIP_AUTH behaviour",
            'os.environ.get("SKIP_AUTH", "")',
            'if os.environ.get("SKIP_AUTH", "").lower() == "true":',
        ],
    )
    def test_skips_benign_strings(self, benign):
        # The legitimate dependencies.py / main.py references compare against
        # "true" but don't *assign* SKIP_AUTH=true, so they shouldn't match.
        # The one exception is the inline comparison string "== \"true\"" —
        # which the workflow excludes via path-based filtering, not regex.
        # We only assert the pattern itself doesn't trip on these forms.
        assert self.PATTERN.search(benign) is None, f"false positive: {benign!r}"
