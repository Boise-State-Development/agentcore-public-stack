"""AgentCore Memory session managers share one set of boto3 clients.

The SDK's ``AgentCoreMemorySessionManager.__init__`` built a ``MemoryClient``
(fresh session, two clients) and then a second fresh session and two more
clients, on every construction. That was ~360ms of CPU per session manager on
the event loop, plus a cold connection pool. These tests build REAL
``TurnBasedSessionManager`` instances through the factory, with only the SDK's
network calls stubbed, so they fail if an SDK upgrade stops going through the
seams the factory relies on. The session itself lives in
``apis.shared.aws_clients`` (``shared_boto_session``), where warm-up builds it.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List
from unittest.mock import MagicMock, patch

import boto3
import pytest

from agents.main_agent.session import session_factory as factory
from apis.shared import aws_clients
from bedrock_agentcore.memory.integrations.strands import session_manager as sdk


@pytest.fixture
def memory_env(monkeypatch):
    """Real construction, no network: the SDK's session read/create are stubbed."""
    monkeypatch.setenv("AGENTCORE_MEMORY_ID", "mem-test")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("AGENT_BUILD_EXPERIMENT", "shared_clients")
    monkeypatch.setattr(factory, "_discover_strategy_ids", lambda memory_id, region, **kwargs: (None, None, None))
    monkeypatch.setattr(sdk.AgentCoreMemorySessionManager, "read_session", lambda self, session_id, **k: None)
    monkeypatch.setattr(sdk.AgentCoreMemorySessionManager, "create_session", lambda self, session, **k: session)
    # A fresh shared session per test, so client counts start from zero.
    aws_clients.reset_cached_clients()
    yield
    aws_clients.reset_cached_clients()


@pytest.fixture
def client_builds(monkeypatch) -> List[str]:
    """Every boto3 client actually constructed, by service name."""
    built: List[str] = []
    original = boto3.Session.client

    def counting_client(self, service_name, *args, **kwargs):
        built.append(service_name)
        return original(self, service_name, *args, **kwargs)

    monkeypatch.setattr(boto3.Session, "client", counting_client)
    return built


def _build(session_id: str) -> Any:
    return factory.SessionFactory.create_session_manager(session_id=session_id, user_id="user-1")


class TestSharedClients:
    def test_the_sdk_builds_its_memory_client_through_the_shared_seam(self):
        assert sdk.MemoryClient is factory._SharedSessionMemoryClient

    def test_later_session_managers_build_no_clients(self, memory_env, client_builds):
        first = _build("session-a")
        built_by_first = len(client_builds)

        second = _build("session-b")

        assert built_by_first > 0
        assert len(client_builds) == built_by_first
        assert second.memory_client.gmdp_client is first.memory_client.gmdp_client
        assert second.memory_client.gmcp_client is first.memory_client.gmcp_client

    def test_each_manager_keeps_its_own_session_state(self, memory_env, client_builds):
        """Only the clients are shared; the session identity is per manager."""
        first = _build("session-a")
        second = _build("session-b")

        assert first.session_id == "session-a"
        assert second.session_id == "session-b"
        assert first.config is not second.config

    def test_shared_clients_get_a_bigger_pool_and_keep_the_sdk_user_agent(self, memory_env):
        manager = _build("session-a")
        config = manager.memory_client.gmdp_client.meta.config

        assert config.max_pool_connections == aws_clients.SHARED_SESSION_MAX_POOL_CONNECTIONS
        assert "strands-agents" in (config.user_agent_extra or "")

    def test_concurrent_first_builds_share_one_client(self, memory_env, client_builds):
        """Two builds racing for the first client must still end up with one."""
        barrier = threading.Barrier(4)

        def build(i: int) -> Any:
            barrier.wait()
            return _build(f"session-{i}")

        with ThreadPoolExecutor(max_workers=4) as pool:
            managers = list(pool.map(build, range(4)))

        assert len({id(m.memory_client.gmdp_client) for m in managers}) == 1


class TestControlArm:
    def test_control_keeps_the_sdks_per_manager_clients(self, memory_env, client_builds, monkeypatch):
        """The default: no experiment set means every session is control."""
        monkeypatch.delenv("AGENT_BUILD_EXPERIMENT", raising=False)

        first = _build("session-a")
        built_by_first = len(client_builds)
        second = _build("session-b")

        assert len(client_builds) == 2 * built_by_first
        assert second.memory_client.gmdp_client is not first.memory_client.gmdp_client

    def test_the_memory_client_seam_is_inert_outside_a_shared_construction(self, memory_env):
        """The rebinding in the SDK module must not change a MemoryClient built
        anywhere else (e.g. `_discover_strategy_ids`)."""
        client = sdk.MemoryClient(region_name="us-west-2")
        assert client.gmdp_client is not aws_clients.shared_boto_session().client(
            "bedrock-agentcore", region_name="us-west-2"
        )


class TestClientReusingSession:
    def test_explicit_credentials_are_never_shared(self):
        session = aws_clients.ClientReusingSession()
        kwargs = dict(region_name="us-west-2", aws_access_key_id="a", aws_secret_access_key="b")

        assert session.client("sts", **kwargs) is not session.client("sts", **kwargs)

    def test_different_configs_get_different_clients(self):
        from botocore.config import Config

        session = aws_clients.ClientReusingSession()
        a = session.client("sts", region_name="us-west-2", config=Config(user_agent_extra="a"))
        b = session.client("sts", region_name="us-west-2", config=Config(user_agent_extra="b"))

        assert a is not b
        assert session.client("sts", region_name="us-west-2", config=Config(user_agent_extra="a")) is a


class TestWhatTheFactoryHandsTheSdk:
    """The seam the arm rides on: the SDK's constructor takes ``boto_session``,
    and ``MemoryClient`` takes ``boto3_session``. On the arm both get the
    shared session; off it, nothing — the SDKs build their own, as before."""

    def test_the_sdk_constructor_gets_the_shared_session_on_the_arm(self, memory_env, monkeypatch):
        captured = {}
        original = sdk.AgentCoreMemorySessionManager.__init__

        def spy(self, *args, **kwargs):
            captured["boto_session"] = kwargs.get("boto_session")
            original(self, *args, **kwargs)

        monkeypatch.setattr(sdk.AgentCoreMemorySessionManager, "__init__", spy)

        _build("session-a")

        assert captured["boto_session"] is aws_clients.shared_boto_session()

    def test_the_sdk_constructor_gets_nothing_off_the_arm(self, memory_env, monkeypatch):
        monkeypatch.delenv("AGENT_BUILD_EXPERIMENT", raising=False)
        captured = {}
        original = sdk.AgentCoreMemorySessionManager.__init__

        def spy(self, *args, **kwargs):
            captured["boto_session"] = kwargs.get("boto_session")
            original(self, *args, **kwargs)

        monkeypatch.setattr(sdk.AgentCoreMemorySessionManager, "__init__", spy)

        _build("session-a")

        assert captured["boto_session"] is None

    def test_strategy_discovery_builds_its_client_on_the_shared_session_on_the_arm(self):
        fetch = factory._discover_strategy_ids.__wrapped__
        with patch.object(factory, "MemoryClient") as memory_client:
            memory_client.return_value.get_memory_strategies.return_value = []
            fetch("mem-test", "us-west-2", shared_session=True)

        memory_client.assert_called_once_with(
            region_name="us-west-2", boto3_session=aws_clients.shared_boto_session()
        )

    def test_strategy_discovery_builds_a_fresh_client_off_the_arm(self):
        fetch = factory._discover_strategy_ids.__wrapped__
        with patch.object(factory, "MemoryClient") as memory_client:
            memory_client.return_value.get_memory_strategies.return_value = []
            fetch("mem-test", "us-west-2", shared_session=False)

        memory_client.assert_called_once_with(region_name="us-west-2", boto3_session=None)

    def test_the_factory_asks_for_the_shared_entry_only_on_the_arm(self, memory_env, monkeypatch):
        asked: List[bool] = []
        monkeypatch.setattr(
            factory,
            "_discover_strategy_ids",
            lambda memory_id, region, **kwargs: (asked.append(kwargs["shared_session"]), (None, None, None))[1],
        )

        _build("session-a")
        monkeypatch.delenv("AGENT_BUILD_EXPERIMENT", raising=False)
        _build("session-b")

        assert asked == [True, False]

    def test_warm_strategy_ids_primes_the_shared_entry(self, memory_env, monkeypatch):
        """Warm-up's call and the arm's first turn must hit the same cache key,
        or the first turn pays the call warm-up already made."""
        with patch.object(factory, "_discover_strategy_ids", return_value=(None, None, None)) as discover:
            factory.warm_strategy_ids()

        discover.assert_called_once_with("mem-test", "us-west-2", shared_session=True)
