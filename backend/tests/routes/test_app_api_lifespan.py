"""app-api's lifespan starts and stops its background loops.

Pins the session revocation watcher in particular: without it, logout only
clears the cache of the task that served it (apis/shared/sessions_bff/
revocations.py), and nothing else would notice the loop had gone.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_lifespan_runs_the_revocation_watcher(monkeypatch) -> None:
    from apis.app_api import main
    from apis.app_api.telemetry import event_loop_lag
    from apis.shared.auth_providers import app_client_reconciler
    from apis.shared.costs import deploy_marker
    from apis.shared.sessions_bff import revocations

    state = {"started": False, "cancelled": False}

    async def watcher() -> None:
        state["started"] = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    async def idle() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(revocations, "run_revocation_watcher", watcher)
    monkeypatch.setattr(app_client_reconciler, "run_reconcile_loop", idle)
    monkeypatch.setattr(event_loop_lag, "run_event_loop_lag_monitor", idle)
    monkeypatch.setattr(deploy_marker, "record_deploy_marker", lambda *_: None)
    monkeypatch.delenv("SKIP_AUTH", raising=False)

    async with main.lifespan(main.app):
        await asyncio.sleep(0)
        assert state["started"]
    assert state["cancelled"]
