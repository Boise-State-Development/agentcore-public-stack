"""Tests for tolerant deserialization and generic error responses on
admin-only model/job listing routes.

Two related cleanups verified here:

* ``GET /admin/fine-tuning/jobs`` no longer returns a 500 when one of
  the persisted job records fails to validate against ``JobResponse``.
  The route serializes records one by one, drops the malformed ones
  (with a warning log), and returns 200 with the well-formed ones.
* ``GET /admin/gemini/models`` and ``GET /admin/openai/models`` no
  longer surface the literal environment variable names
  (``GOOGLE_API_KEY``, ``GOOGLE_GEMINI_API_KEY``, ``OPENAI_API_KEY``)
  in their response bodies when the API key is unconfigured. The
  unconfigured condition maps to a generic 503 with no detail leak;
  the specific env var name is logged server-side for operator
  diagnostics.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.fine_tuning.routes import (
    router as fine_tuning_router,
    get_jobs_repository,
)
from apis.app_api.admin.routes import router as admin_router
from apis.shared.auth.rbac import require_admin
from apis.shared.security import (
    register_aws_client_error_handler,
    register_validation_error_handler,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app(monkeypatch) -> FastAPI:
    # The fine-tuning subrouter mounts conditionally on import. Set the
    # flag, force a fresh module load, then build the app.
    monkeypatch.setenv("FINE_TUNING_ENABLED", "true")
    import importlib
    import apis.app_api.admin.routes as admin_routes_module

    importlib.reload(admin_routes_module)

    _app = FastAPI()
    _app.include_router(admin_routes_module.router)
    register_aws_client_error_handler(_app)
    register_validation_error_handler(_app)
    return _app


def _admin(app: FastAPI, make_user) -> TestClient:
    user = make_user(roles=["system_admin"])
    app.dependency_overrides[require_admin] = lambda: user
    return TestClient(app)


def _well_formed_job(job_id: str = "job-1") -> dict:
    return {
        "job_id": job_id,
        "user_id": "u-1",
        "email": "u@x",
        "model_id": "m-1",
        "model_name": "Some Model",
        "status": "COMPLETED",
        "dataset_s3_key": "s3://b/k.csv",
        "instance_type": "ml.m5.xlarge",
        "instance_count": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# F15: tolerant deserialization on /admin/fine-tuning/jobs
# ---------------------------------------------------------------------------


def test_jobs_list_skips_malformed_records_returns_200(app, make_user) -> None:
    """A record that fails JobResponse validation must not bring down
    the whole listing — drop it, keep going, surface the rest."""
    client = _admin(app, make_user)

    repo = MagicMock()
    repo.list_all_jobs.return_value = [
        _well_formed_job("job-1"),
        # Missing required fields → JobResponse will reject this row.
        {"job_id": "job-broken"},
        _well_formed_job("job-2"),
    ]
    app.dependency_overrides[get_jobs_repository] = lambda: repo

    resp = client.get("/admin/fine-tuning/jobs")

    assert resp.status_code == 200
    body = resp.json()
    ids = [j["job_id"] for j in body["jobs"]]
    # The malformed row is dropped; the well-formed ones survive.
    assert "job-1" in ids
    assert "job-2" in ids
    assert "job-broken" not in ids


def test_jobs_list_with_all_well_formed_records_returns_200(app, make_user) -> None:
    client = _admin(app, make_user)

    repo = MagicMock()
    repo.list_all_jobs.return_value = [
        _well_formed_job("job-1"),
        _well_formed_job("job-2"),
    ]
    app.dependency_overrides[get_jobs_repository] = lambda: repo

    resp = client.get("/admin/fine-tuning/jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 2


def test_jobs_list_with_only_malformed_records_returns_200_empty(app, make_user) -> None:
    """All rows malformed → 200 with empty list, not 500."""
    client = _admin(app, make_user)

    repo = MagicMock()
    repo.list_all_jobs.return_value = [
        {"job_id": "x"},
        {"some": "garbage"},
    ]
    app.dependency_overrides[get_jobs_repository] = lambda: repo

    resp = client.get("/admin/fine-tuning/jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["jobs"] == []
    assert body["total_count"] == 0


def test_jobs_list_propagates_repository_failure_as_500(app, make_user) -> None:
    """Repository-level failures still surface (the per-record drop is
    only for individual record validation, not a wholesale exception)."""
    client = _admin(app, make_user)

    repo = MagicMock()
    repo.list_all_jobs.side_effect = RuntimeError("table unreachable")
    app.dependency_overrides[get_jobs_repository] = lambda: repo

    resp = TestClient(app, raise_server_exceptions=False).get("/admin/fine-tuning/jobs")
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# F16: external-model 503 instead of 500 with env-var name leak
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/admin/gemini/models", "/admin/openai/models"])
def test_external_model_unconfigured_returns_503_generic(
    app, make_user, monkeypatch, path: str
) -> None:
    client = _admin(app, make_user)

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    resp = client.get(path)
    assert resp.status_code == 503
    body_text = resp.text
    assert "GOOGLE_API_KEY" not in body_text
    assert "GOOGLE_GEMINI_API_KEY" not in body_text
    assert "OPENAI_API_KEY" not in body_text


@pytest.mark.parametrize("path", ["/admin/gemini/models", "/admin/openai/models"])
def test_external_model_unconfigured_does_not_disclose_env_var_names(
    app, make_user, monkeypatch, path: str
) -> None:
    """Belt-and-suspenders: even if message wording changes, the raw env
    var names must never appear in the body."""
    client = _admin(app, make_user)

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    resp = client.get(path)
    detail = resp.json().get("detail", "")
    assert "API_KEY" not in detail
