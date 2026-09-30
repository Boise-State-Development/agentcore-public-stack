"""On the `shared_clients` arm, Strands' BedrockModel is built on the process-wide
boto3 session instead of the fresh `boto3.Session()` it would construct itself.

`BedrockModel.__init__` raises when `region_name` and `boto_session` are both
given (strands-agents 1.55.0), so the arm must never send both. Real objects,
no network: constructing a client opens no socket.
"""

import pytest

from agents.main_agent.core.agent_factory import AgentFactory
from agents.main_agent.core.model_config import ModelConfig
from apis.shared import aws_clients

MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


@pytest.fixture(autouse=True)
def _region_and_fresh_session(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    aws_clients.reset_cached_clients()
    yield
    aws_clients.reset_cached_clients()


class TestOnTheArm:
    @pytest.fixture(autouse=True)
    def _arm(self, monkeypatch):
        monkeypatch.setenv("AGENT_BUILD_EXPERIMENT", "shared_clients")

    def test_config_carries_the_shared_session_and_no_region(self):
        config = ModelConfig(model_id=MODEL_ID).to_bedrock_config(session_id="s")

        assert config["boto_session"] is aws_clients.shared_boto_session()
        assert "region_name" not in config

    def test_the_model_client_is_the_shared_sessions_client(self):
        first = AgentFactory._create_bedrock_model(ModelConfig(model_id=MODEL_ID), session_id="s")
        second = AgentFactory._create_bedrock_model(ModelConfig(model_id=MODEL_ID), session_id="s")

        assert first.client is second.client, "two models, one bedrock-runtime client"
        assert first.client.meta.config.max_pool_connections == aws_clients.SHARED_SESSION_MAX_POOL_CONNECTIONS

    def test_the_arm_resolves_the_same_region_as_a_fresh_session(self, monkeypatch):
        """Strands resolves the region from the session it is given, and a
        fresh `boto3.Session()` reads the same environment the shared one
        does, so the arm must land on the same region as control."""
        shared = AgentFactory._create_bedrock_model(ModelConfig(model_id=MODEL_ID), session_id="s")
        monkeypatch.setenv("AGENT_BUILD_EXPERIMENT", "control")
        control = AgentFactory._create_bedrock_model(ModelConfig(model_id=MODEL_ID), session_id="s")

        assert shared.client.meta.region_name == control.client.meta.region_name

    def test_a_deliberate_region_next_to_the_session_is_still_refused(self):
        """Pins the Strands contract the arm is written around."""
        from strands.models import BedrockModel

        with pytest.raises(ValueError, match="both"):
            BedrockModel(model_id=MODEL_ID, region_name="us-west-2", boto_session=aws_clients.shared_boto_session())


class TestOffTheArm:
    @pytest.mark.parametrize("value", [None, "", "control", "ab"])
    def test_config_carries_no_session(self, monkeypatch, value):
        if value is None:
            monkeypatch.delenv("AGENT_BUILD_EXPERIMENT", raising=False)
        else:
            monkeypatch.setenv("AGENT_BUILD_EXPERIMENT", value)

        config = ModelConfig(model_id=MODEL_ID).to_bedrock_config(session_id=None)

        assert "boto_session" not in config
        assert "region_name" not in config

    def test_the_model_builds_its_own_client(self, monkeypatch):
        monkeypatch.delenv("AGENT_BUILD_EXPERIMENT", raising=False)

        first = AgentFactory._create_bedrock_model(ModelConfig(model_id=MODEL_ID))
        second = AgentFactory._create_bedrock_model(ModelConfig(model_id=MODEL_ID))

        assert first.client is not second.client
        assert aws_clients._shared_session is None, "control never touches the shared session"
