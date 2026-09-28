"""AgentCore Memory session managers share one set of boto3 clients.

The SDK's ``AgentCoreMemorySessionManager.__init__`` built a ``MemoryClient``
(fresh session, two clients) and then a second fresh session and two more
clients, on every construction. That was ~360ms of CPU per session manager on
the event loop, plus a cold connection pool. These tests build REAL
``TurnBasedSessionManager`` instances through the factory, with only the SDK's
network calls stubbed, so they fail if an SDK upgrade stops going through the
seams the factory relies on.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List

import boto3
import pytest

from agents.main_agent.session import session_factory as factory
from bedrock_agentcore.memory.integrations.strands import session_manager as sdk


@pytest.fixture
def memory_env(monkeypatch):
    """Real construction, no network: the SDK's session read/create are stubbed."""
    monkeypatch.setenv("AGENTCORE_MEMORY_ID", "mem-test")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("AGENT_BUILD_EXPERIMENT", "shared_clients")
    monkeypatch.setattr(factory, "_discover_strategy_ids", lambda memory_id, region: (None, None, None))
    monkeypatch.setattr(sdk.AgentCoreMemorySessionManager, "read_session", lambda self, session_id, **k: None)
    monkeypatch.setattr(sdk.AgentCoreMemorySessionManager, "create_session", lambda self, session, **k: session)
    # A fresh shared session per test, so client counts start from zero.
    monkeypatch.setattr(factory, "_shared_memory_session", None)


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

        assert config.max_pool_connections == factory._SHARED_MEMORY_MAX_POOL_CONNECTIONS
        assert "strands-agents" in (config.user_agent_extra or "")

    def test_concurrent_first_builds_share_one_client(self, memory_env, client_builds):
        """Builds run in worker threads; the first ones race to create the clients."""
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
        assert client.gmdp_client is not factory.shared_memory_boto_session().client(
            "bedrock-agentcore", region_name="us-west-2"
        )


class TestClientReusingSession:
    def test_explicit_credentials_are_never_shared(self):
        session = factory._ClientReusingSession()
        kwargs = dict(region_name="us-west-2", aws_access_key_id="a", aws_secret_access_key="b")

        assert session.client("sts", **kwargs) is not session.client("sts", **kwargs)

    def test_different_configs_get_different_clients(self):
        from botocore.config import Config

        session = factory._ClientReusingSession()
        a = session.client("sts", region_name="us-west-2", config=Config(user_agent_extra="a"))
        b = session.client("sts", region_name="us-west-2", config=Config(user_agent_extra="b"))

        assert a is not b
        assert session.client("sts", region_name="us-west-2", config=Config(user_agent_extra="a")) is a
