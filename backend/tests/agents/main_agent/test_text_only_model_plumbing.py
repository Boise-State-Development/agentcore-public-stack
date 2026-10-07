"""``text_only_model`` reaches the factory and survives a paused turn.

The factory projects history only when ``ModelConfig.text_only`` is set, so a
link that drops the flag silently restores the stuck-conversation bug: a
vision-era image in history fails every turn on a text-only model.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _build(**kwargs):
    from agents.main_agent import base_agent as ba

    class Probe(ba.BaseAgent):
        def _create_agent(self):
            pass

        async def stream_async(self, *a, **k):  # pragma: no cover - abstract stub
            yield None

    with patch.object(ba, "create_default_registry", return_value=MagicMock()), \
         patch.object(ba, "ToolFilter", return_value=MagicMock()), \
         patch.object(ba.BaseAgent, "_register_external_mcp_tools", lambda self: None), \
         patch.object(ba, "GatewayIntegration", return_value=MagicMock()), \
         patch.object(ba, "SessionFactory") as sf, \
         patch.object(ba, "StreamCoordinator", return_value=MagicMock()):
        sf.create_session_manager.return_value = MagicMock()
        return Probe(session_id="s", user_id="u", model_id="zai.glm-5", **kwargs)


@pytest.mark.parametrize("text_only", [True, False])
def test_base_agent_sets_model_config_and_snapshot(text_only):
    agent = _build(text_only_model=text_only)
    assert agent.model_config.text_only is text_only
    assert agent._construction_snapshot["text_only_model"] is text_only


def test_default_is_not_text_only():
    agent = _build()
    assert agent.model_config.text_only is False


@pytest.mark.asyncio
async def test_paused_snapshot_persists_the_flag():
    from agents.main_agent.streaming.stream_coordinator import StreamCoordinator

    agent = SimpleNamespace(_interrupt_state=SimpleNamespace(activated=True))
    wrapper = SimpleNamespace(_construction_snapshot={"enabled_tools": [], "text_only_model": True})
    with patch("apis.shared.sessions.metadata.set_paused_turn", new=AsyncMock()) as write:
        await StreamCoordinator()._persist_paused_turn_snapshot(agent, "sess-1", "user-1", wrapper)
    snapshot = write.await_args.args[2]
    assert snapshot.text_only_model is True
    assert snapshot.model_dump(by_alias=True)["textOnlyModel"] is True


def test_a_snapshot_written_before_the_field_reads_as_none():
    from apis.shared.sessions.models import PausedTurnSnapshot

    snapshot = PausedTurnSnapshot.model_validate({"capturedAt": "t", "expiresAt": "t"})
    assert snapshot.text_only_model is None
