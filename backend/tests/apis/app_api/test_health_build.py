"""`/health` reports the baked version and build (release-ledger spec D2)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.health import health


def test_health_names_version_and_build(monkeypatch):
    monkeypatch.setattr(health, "health_fields", lambda: {"version": "1.33.0", "build": "8f676bb2e1a4c0d2"})
    app = FastAPI()
    app.include_router(health.router)
    body = TestClient(app).get("/health").json()
    assert body == {"status": "healthy", "service": "agent-core", "version": "1.33.0", "build": "8f676bb2e1a4c0d2"}
