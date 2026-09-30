"""Startup warm-up — pulls the first turn's lazy imports and boto service-model
loads forward to container start (apis/inference_api/warmup.py).

Nothing here touches the network: boto3.client is patched, and the module list
is exercised with stand-ins so the tests don't depend on sympy import time.
"""

import sys
import threading
import types
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

    def test_disabled_starts_no_thread(self, monkeypatch):
        monkeypatch.setenv(warmup.WARMUP_ENABLED_ENV, "false")
        with patch.object(warmup, "run_warmup") as run:
            assert warmup.start_warmup_in_background() is None
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


class TestBackground:
    def test_runs_on_a_daemon_thread_and_returns_immediately(self, monkeypatch):
        monkeypatch.delenv(warmup.WARMUP_ENABLED_ENV, raising=False)
        started = threading.Event()
        release = threading.Event()

        def slow():
            started.set()
            release.wait(timeout=5)

        with patch.object(warmup, "run_warmup", side_effect=slow):
            thread = warmup.start_warmup_in_background()
            assert thread is not None
            assert thread.daemon
            assert started.wait(timeout=5)
            # Caller is not blocked while the warm-up is still running.
            assert thread.is_alive()
            release.set()
            thread.join(timeout=5)
        assert not thread.is_alive()
