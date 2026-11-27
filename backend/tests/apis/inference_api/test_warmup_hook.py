"""The Runtime V2 build warmup hook, `POST /warmup` (apis/inference_api/warmup_hook.py).

AgentCore calls it twice while it prepares a Runtime version: `memory` before
the snapshot, `disk` after it. A call that does not answer 2xx fails the
deploy and leaves the Runtime in UPDATE_FAILED, so most of this file is about
what the handler does when something goes wrong: it still answers 200.

Nothing here touches the network. The disk phase's throwaway agent build runs
for real, and the suite's off-box socket guard (tests/conftest.py) is what
proves it stays offline.
"""

from __future__ import annotations

import asyncio
import re
import sys
import threading
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.inference_api import warmup, warmup_hook
from apis.inference_api.runtime_health import (
    STATUS_HEALTHY,
    InvocationActivityMiddleware,
    RuntimeActivityTracker,
)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(warmup_hook.router)
    return TestClient(app)


def _post(client: TestClient, warmup_type: str | None):
    headers = {warmup_hook.WARMUP_TYPE_HEADER: warmup_type} if warmup_type is not None else {}
    return client.post(warmup_hook.WARMUP_HOOK_PATH, headers=headers)


class TestHookContract:
    """What `hooksConfiguration.build.warmup.action.httpPost` requires of us."""

    def test_the_path_meets_agentcores_rules(self):
        path = warmup_hook.WARMUP_HOOK_PATH
        assert path == "/warmup"
        assert 1 <= len(path) <= 32
        assert re.fullmatch(r"/[A-Za-z0-9._~/-]*", path)
        assert not path.endswith("/") and "//" not in path

    def test_the_route_is_not_one_agentcore_already_calls(self):
        # POST /invocations, GET /ping and GET /ws are the platform's own.
        assert warmup_hook.WARMUP_HOOK_PATH not in {"/invocations", "/ping", "/ws"}

    def test_the_header_name_is_the_documented_one(self):
        assert warmup_hook.WARMUP_TYPE_HEADER == "X-Amzn-Bedrock-AgentCore-Runtime-Warmup-Type"

    def test_the_app_serves_it_as_a_post_route(self):
        from apis.inference_api.main import app

        methods = {
            method
            for route in app.routes
            if getattr(route, "path", None) == warmup_hook.WARMUP_HOOK_PATH
            for method in route.methods
        }
        assert methods == {"POST"}

    def test_the_two_phases_fit_the_shared_120_seconds(self):
        assert warmup_hook.MEMORY_PHASE_TIMEOUT_SECONDS + warmup_hook.DISK_PHASE_TIMEOUT_SECONDS < 120
        assert warmup_hook.SOURCE_READ_BUDGET_SECONDS < warmup_hook.DISK_PHASE_TIMEOUT_SECONDS

    def test_the_route_docstring_says_why_it_is_allowed(self):
        # CLAUDE.md's Inference API boundary keeps routes off this service.
        doc = warmup_hook.warmup_hook.__doc__ or ""
        assert "Inference API boundary" in doc
        assert "data plane" in doc


class TestDispatch:
    def _phases(self, calls: list, **overrides):
        phases = {
            warmup_hook.WARMUP_TYPE_MEMORY: (lambda: calls.append("memory"), 5.0),
            warmup_hook.WARMUP_TYPE_DISK: (lambda: calls.append("disk"), 5.0),
        }
        phases.update(overrides)
        return patch.dict(warmup_hook._PHASES, phases)

    def test_memory_runs_the_memory_phase(self):
        calls: list = []
        with self._phases(calls):
            response = _post(_client(), "memory")
        assert response.status_code == 200
        assert response.json()["warmupType"] == "memory"
        assert response.json()["outcome"] == "ok"
        assert calls == ["memory"]

    def test_disk_runs_the_disk_phase(self):
        calls: list = []
        with self._phases(calls):
            response = _post(_client(), "disk")
        assert response.status_code == 200
        assert response.json()["outcome"] == "ok"
        assert calls == ["disk"]

    @pytest.mark.parametrize("value", ["MEMORY", " Memory ", "Disk"])
    def test_the_type_is_matched_loosely(self, value):
        calls: list = []
        with self._phases(calls):
            assert _post(_client(), value).json()["outcome"] == "ok"
        assert calls == [value.strip().lower()]

    @pytest.mark.parametrize("value", [None, "", "network", "memory,disk"])
    def test_an_unknown_or_missing_type_answers_200_and_does_nothing(self, value):
        """A type AWS adds later must not fail a deploy of this image."""
        calls: list = []
        with self._phases(calls):
            response = _post(_client(), value)
        assert response.status_code == 200
        assert response.json()["outcome"] == "ignored"
        assert calls == []

    def test_a_phase_that_raises_still_answers_200(self):
        def boom() -> None:
            raise RuntimeError("phase exploded")

        with patch.dict(warmup_hook._PHASES, {warmup_hook.WARMUP_TYPE_DISK: (boom, 5.0)}):
            response = _post(_client(), "disk")
        assert response.status_code == 200
        assert response.json()["outcome"] == "error"

    def test_a_phase_past_its_bound_answers_200_and_keeps_running(self):
        finished = threading.Event()

        def slow() -> None:
            # The test client's loop waits for this thread when it closes, so
            # "answered before the phase ended" is read off the handler's own
            # clock rather than off `finished` at response time.
            threading.Event().wait(timeout=0.6)
            finished.set()

        with patch.dict(warmup_hook._PHASES, {warmup_hook.WARMUP_TYPE_MEMORY: (slow, 0.05)}):
            response = _post(_client(), "memory")

        assert response.status_code == 200
        assert response.json()["outcome"] == "timeout"
        assert response.json()["ms"] < 500
        assert finished.wait(timeout=5)


class TestPingStaysResponsive:
    """`/ping` must keep answering while a phase runs, and keep saying Healthy."""

    def test_ping_answers_healthy_while_a_phase_is_blocked(self, monkeypatch):
        tracker = RuntimeActivityTracker()
        monkeypatch.setattr("apis.inference_api.runtime_health.tracker", tracker)

        app = FastAPI()
        app.include_router(warmup_hook.router)

        @app.get("/ping")
        async def ping() -> dict:
            return {"status": tracker.snapshot()[0], "in_flight": tracker.in_flight}

        app.add_middleware(InvocationActivityMiddleware)

        started = threading.Event()
        release = threading.Event()

        def blocked() -> None:
            started.set()
            release.wait(timeout=10)

        async def scenario() -> tuple:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://runtime") as client:
                hook = asyncio.create_task(
                    client.post(
                        warmup_hook.WARMUP_HOOK_PATH,
                        headers={warmup_hook.WARMUP_TYPE_HEADER: "memory"},
                    )
                )
                try:
                    while not started.is_set():
                        await asyncio.sleep(0.005)
                    ping = await asyncio.wait_for(client.get("/ping"), timeout=2)
                    still_running = not hook.done()
                finally:
                    release.set()
                return ping, still_running, await hook

        with patch.dict(warmup_hook._PHASES, {warmup_hook.WARMUP_TYPE_MEMORY: (blocked, 10.0)}):
            ping, still_running, hook_response = asyncio.run(scenario())

        assert still_running, "the phase finished before /ping was asked"
        assert ping.status_code == 200
        # Not HealthyBusy: the hook is the platform preparing an image, not a
        # session's work, and the idle origin must not move for it.
        assert ping.json() == {"status": STATUS_HEALTHY, "in_flight": 0}
        assert hook_response.status_code == 200
        assert hook_response.json()["outcome"] == "ok"


class TestMemoryPhase:
    def test_runs_the_startup_warmup_again(self, monkeypatch):
        monkeypatch.delenv(warmup.WARMUP_ENABLED_ENV, raising=False)
        with patch.object(warmup, "run_warmup") as run:
            warmup_hook.run_memory_phase()
        run.assert_called_once_with()

    def test_the_kill_switch_skips_it(self, monkeypatch):
        monkeypatch.setenv(warmup.WARMUP_ENABLED_ENV, "false")
        with patch.object(warmup, "run_warmup") as run:
            warmup_hook.run_memory_phase()
        run.assert_not_called()

    def test_a_failing_warmup_does_not_raise(self, monkeypatch):
        monkeypatch.delenv(warmup.WARMUP_ENABLED_ENV, raising=False)
        with patch.object(warmup, "run_warmup", side_effect=RuntimeError("no region")):
            warmup_hook.run_memory_phase()


class TestDiskPhase:
    def test_the_throwaway_build_runs_offline_and_reports_the_build_stages(self, monkeypatch):
        """The real build. The suite's socket guard fails this test if it
        opens a connection, which is the hermetic claim in the docstring."""
        monkeypatch.setenv("AWS_REGION", "us-west-2")

        stages = warmup_hook.build_throwaway_agent()

        assert {"prompt", "registry", "session_mgr", "hooks", "plugins", "finalize"} <= set(stages)
        assert all(isinstance(ms, int) and ms >= 0 for ms in stages.values())

    def test_the_throwaway_build_leaves_no_trace_in_the_agent_cache(self, monkeypatch):
        """The agent cache and the build counter belong to real sessions."""
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        from apis.inference_api.chat import service

        cache_before = dict(service._agent_cache)
        builds_before = service.process_build_count()

        warmup_hook.build_throwaway_agent()

        assert service._agent_cache == cache_before
        assert service.process_build_count() == builds_before

    def test_the_throwaway_build_reads_no_catalog_and_builds_no_memory_manager(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        from agents.main_agent.session.session_factory import SessionFactory

        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository"
        ) as catalog, patch.object(SessionFactory, "_create_cloud_session_manager") as cloud:
            warmup_hook.build_throwaway_agent()

        catalog.assert_not_called()
        cloud.assert_not_called()

    def test_a_failing_build_does_not_stop_the_other_steps(self):
        with patch.object(
            warmup_hook, "build_throwaway_agent", side_effect=RuntimeError("no model")
        ), patch.object(warmup_hook, "format_one_traceback", return_value=1) as tb, patch.object(
            warmup_hook, "read_loaded_sources", return_value={"files": 0}
        ) as sources:
            warmup_hook.run_disk_phase()

        tb.assert_called_once_with()
        sources.assert_called_once_with()

    def test_every_step_failing_still_does_not_raise(self):
        boom = RuntimeError("boom")
        with patch.object(warmup_hook, "build_throwaway_agent", side_effect=boom), patch.object(
            warmup_hook, "format_one_traceback", side_effect=boom
        ), patch.object(warmup_hook, "read_loaded_sources", side_effect=boom):
            warmup_hook.run_disk_phase()

    def test_format_one_traceback_formats_a_real_traceback(self):
        assert warmup_hook.format_one_traceback() > 0


class TestReadLoadedSources:
    def test_reads_the_loaded_modules_sources(self):
        result = warmup_hook.read_loaded_sources(budget_seconds=30.0)

        assert result["files"] > 0
        assert result["bytes"] > 0
        assert result["files"] <= result["of"]

    def test_stops_at_the_budget_and_says_so(self):
        result = warmup_hook.read_loaded_sources(budget_seconds=0.0)

        assert result["files"] == 0
        assert result["complete"] is False

    def test_first_party_modules_come_first(self):
        paths = warmup_hook._loaded_source_files()
        own = sys.modules[warmup_hook.__name__].__file__
        stdlib = sys.modules["json"].__file__

        assert paths.index(own) < paths.index(stdlib)
        assert len(paths) == len(set(paths))

    def test_an_unreadable_file_is_skipped(self, tmp_path):
        missing = str(tmp_path / "gone.py")
        present = tmp_path / "here.py"
        present.write_text("x = 1\n")

        with patch.object(warmup_hook, "_loaded_source_files", return_value=[missing, str(present)]):
            result = warmup_hook.read_loaded_sources(budget_seconds=5.0)

        assert result == {"files": 1, "of": 2, "bytes": 6, "complete": False}
