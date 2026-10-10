"""Startup warm-up — pulls the first turn's lazy imports and boto service-model
loads forward to container start (apis/inference_api/warmup.py).

Nothing here touches the network: boto3.client is patched, and the module list
is exercised with stand-ins so the tests don't depend on sympy import time.
"""

import ast
import asyncio
import sys
import threading
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apis.inference_api import warmup


class TestKillSwitch:
    def test_defaults_on(self, monkeypatch):
        monkeypatch.delenv(warmup.WARMUP_ENABLED_ENV, raising=False)
        assert warmup.warmup_enabled()

    def test_empty_string_is_on(self, monkeypatch):
        monkeypatch.setenv(warmup.WARMUP_ENABLED_ENV, "")
        assert warmup.warmup_enabled()

    @pytest.mark.parametrize("value", ["false", "FALSE", " False "])
    def test_false_is_off(self, monkeypatch, value):
        monkeypatch.setenv(warmup.WARMUP_ENABLED_ENV, value)
        assert not warmup.warmup_enabled()

    def test_disabled_runs_nothing(self, monkeypatch):
        monkeypatch.setenv(warmup.WARMUP_ENABLED_ENV, "false")
        with patch.object(warmup, "run_warmup") as run:
            assert asyncio.run(warmup.warm_before_ready()) is False
        run.assert_not_called()


class TestWarmModules:
    def test_imports_each_module_once(self, monkeypatch):
        fake = types.ModuleType("fake_warm_target")
        fake.loaded = 0
        monkeypatch.setitem(sys.modules, "fake_warm_target", fake)

        with patch.object(warmup.importlib, "import_module", wraps=warmup.importlib.import_module) as imp:
            warmup.warm_modules(["fake_warm_target"])

        imp.assert_called_once_with("fake_warm_target")

    def test_a_missing_module_is_logged_not_raised(self):
        # Must not raise: a warm-up failure is never a startup failure.
        warmup.warm_modules(["definitely_not_a_real_module_xyz"])

    def test_the_default_list_names_the_calculator_first(self):
        # sympy behind the calculator is the single heaviest lazy import on the
        # first-turn path; it must be the first thing warmed.
        assert warmup.WARM_MODULES[0] == "strands_tools.calculator"


class TestWarmBotoClients:
    def test_builds_one_client_per_service_in_the_configured_region(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        with patch("boto3.client") as client:
            warmup.warm_boto_clients(["bedrock-runtime", "s3"])

        assert [c.args[0] for c in client.call_args_list] == ["bedrock-runtime", "s3"]
        assert all(c.kwargs["region_name"] == "us-west-2" for c in client.call_args_list)

    def test_skips_without_a_region(self, monkeypatch):
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        with patch("boto3.client") as client:
            warmup.warm_boto_clients(["s3"])
        client.assert_not_called()

    def test_a_failing_client_does_not_stop_the_rest(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        calls = []

        def flaky(service, **kwargs):
            calls.append(service)
            if service == "bedrock-runtime":
                raise RuntimeError("no such service")
            return object()

        with patch("boto3.client", side_effect=flaky):
            warmup.warm_boto_clients(["bedrock-runtime", "s3"])

        assert calls == ["bedrock-runtime", "s3"]


class TestWarmSharedSession:
    """The agent build's SDK clients are built on one process-wide session
    (`apis.shared.aws_clients.shared_boto_session`). Building them here is
    what lets the `shared_clients` arm's first turn skip the service-model
    parses; discovering the strategy ids here is what lets it skip the one
    control-plane call."""

    def test_builds_each_client_once_on_the_shared_session(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        session = MagicMock(name="shared-session")

        with patch("apis.shared.aws_clients.shared_boto_session", return_value=session), patch(
            "agents.main_agent.session.session_factory.warm_strategy_ids"
        ):
            warmup.warm_shared_session(["bedrock-agentcore", "bedrock-runtime"])

        assert [c.args[0] for c in session.client.call_args_list] == ["bedrock-agentcore", "bedrock-runtime"]
        assert all(c.kwargs["region_name"] == "us-west-2" for c in session.client.call_args_list)

    def test_the_default_list_covers_both_sdks(self):
        # The Memory session manager (data + control plane) and Strands'
        # BedrockModel (runtime) are the two SDKs handed the shared session.
        assert set(warmup.WARM_SHARED_SESSION_SERVICES) == {
            "bedrock-agentcore",
            "bedrock-agentcore-control",
            "bedrock-runtime",
        }

    def test_discovers_the_strategy_ids_once_on_the_shared_session(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        monkeypatch.setenv("AGENTCORE_MEMORY_ID", "mem-warm")
        from agents.main_agent.session import session_factory

        with patch("apis.shared.aws_clients.shared_boto_session", return_value=MagicMock()), patch.object(
            session_factory, "_discover_strategy_ids", return_value=(None, None, None)
        ) as discover:
            warmup.warm_shared_session([])

        discover.assert_called_once_with("mem-warm", "us-west-2", shared_session=True)

    def test_no_memory_configured_skips_discovery_and_does_not_raise(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        monkeypatch.delenv("AGENTCORE_MEMORY_ID", raising=False)
        from agents.main_agent.session import session_factory

        with patch("apis.shared.aws_clients.shared_boto_session", return_value=MagicMock()), patch.object(
            session_factory, "_discover_strategy_ids"
        ) as discover:
            warmup.warm_shared_session([])

        discover.assert_not_called()

    def test_skips_without_a_region(self, monkeypatch):
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        with patch("apis.shared.aws_clients.shared_boto_session") as session:
            warmup.warm_shared_session()
        session.assert_not_called()

    def test_the_kill_switch_skips_the_whole_step(self, monkeypatch):
        """Off means the SDKs build their own sessions, so nothing here would
        be used — including the one connection the strategy read opens."""
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        monkeypatch.setenv("AGENT_BUILD_SHARED_SESSION_ENABLED", "false")
        from agents.main_agent.session import session_factory

        with patch("apis.shared.aws_clients.shared_boto_session") as session, patch.object(
            session_factory, "_discover_strategy_ids"
        ) as discover:
            warmup.warm_shared_session()

        session.assert_not_called()
        discover.assert_not_called()

    def test_a_failing_client_does_not_stop_the_rest(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        session = MagicMock()
        session.client.side_effect = [RuntimeError("no such service"), object()]

        with patch("apis.shared.aws_clients.shared_boto_session", return_value=session), patch(
            "agents.main_agent.session.session_factory.warm_strategy_ids"
        ) as ids:
            warmup.warm_shared_session(["bedrock-agentcore-control", "bedrock-runtime"])

        assert session.client.call_count == 2
        ids.assert_called_once()

    def test_run_warmup_includes_it(self):
        with patch.object(warmup, "warm_modules"), patch.object(warmup, "warm_boto_clients"), patch.object(
            warmup, "warm_shared_session"
        ) as shared:
            warmup.run_warmup()
        shared.assert_called_once_with()


class TestWarmBeforeReady:
    """Warm-up must FINISH before the server can answer /ping.

    AgentCore Runtime V2 snapshots the container on its first healthy /ping
    and restores that snapshot for every new session, so anything still
    running then is redone on each session's first turn (dev, 2026-10-10:
    the snapshot preceded warm-up's end by ~0.9 s, and first-turn preludes
    ran up to 3.4 s against ~1 s).
    """

    def test_returns_only_after_warmup_completes(self, monkeypatch):
        monkeypatch.delenv(warmup.WARMUP_ENABLED_ENV, raising=False)
        finished = threading.Event()

        def work():
            threading.Event().wait(0.2)
            finished.set()

        with patch.object(warmup, "run_warmup", side_effect=work):
            assert asyncio.run(warmup.warm_before_ready()) is True
        assert finished.is_set()

    def test_runs_off_the_event_loop_thread(self, monkeypatch):
        monkeypatch.delenv(warmup.WARMUP_ENABLED_ENV, raising=False)
        seen = {}

        async def main():
            seen["loop"] = threading.get_ident()
            with patch.object(warmup, "run_warmup", side_effect=lambda: seen.setdefault("work", threading.get_ident())):
                await warmup.warm_before_ready()

        asyncio.run(main())
        assert seen["work"] != seen["loop"]

    def test_a_stalled_warmup_does_not_hold_startup_past_the_ceiling(self, monkeypatch):
        monkeypatch.delenv(warmup.WARMUP_ENABLED_ENV, raising=False)
        release = threading.Event()
        with patch.object(warmup, "run_warmup", side_effect=lambda: release.wait(5)):
            assert asyncio.run(warmup.warm_before_ready(timeout=0.1)) is False
        release.set()

    def test_the_ceiling_is_inside_the_runtime_health_deadline(self):
        # AgentCore fails a container that is not healthy within 120 s of start.
        assert 0 < warmup.WARMUP_READY_TIMEOUT_SECONDS < 120

    def test_the_entrypoint_awaits_warmup_before_the_server_starts(self):
        """`await warm_before_ready()` must come before `yield` in the lifespan:
        uvicorn binds its socket only after startup returns, so that ordering is
        what keeps /ping (and the V2 snapshot) behind warm-up."""
        source = Path(__file__).resolve().parents[3] / "src" / "apis" / "inference_api" / "main.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        lifespan = next(
            n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan"
        )
        awaited = [
            n.lineno for n in ast.walk(lifespan)
            if isinstance(n, ast.Await) and isinstance(n.value, ast.Call)
            and getattr(n.value.func, "id", None) == "warm_before_ready"
        ]
        yields = [n.lineno for n in ast.walk(lifespan) if isinstance(n, ast.Yield)]
        assert awaited, "lifespan no longer awaits warm_before_ready()"
        assert yields and min(awaited) < min(yields)
        assert "start_warmup_in_background" not in source.read_text(encoding="utf-8")
