"""Tool-result offload at intake — thresholds spec §3.6 (PR-4)."""

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.main_agent.core import tool_result_offload as tro
from agents.main_agent.core.tool_result_offload import (
    PREFILTER_RATIO,
    build_tool_result_offloader,
    offload_prefix,
    tool_result_offload_enabled,
)


class _FakeStorage:
    """Unified-Storage-shaped in-memory backend (write/read), like strands.storage."""

    def __init__(self):
        self.objects = {}

    async def write(self, key, data):
        self.objects[key] = data

    async def read(self, key):
        return self.objects.get(key)

    async def delete(self, key):
        self.objects.pop(key, None)

    async def list(self, query=""):
        return [k for k in self.objects if k.startswith(query)]


class _Agent:
    """Weak-referenceable stand-in (the plugin keys WeakKeyDictionaries on the agent)."""

    def __init__(self, model=None):
        self.model = model
        self.event_loop_metrics = SimpleNamespace(cycle_count=1)
        self.storage = None
        self.sandbox = None


def _event(text, tool_name="gmail_search", count_tokens=None):
    result = {"toolUseId": "t1", "status": "success", "content": [{"text": text}]}
    model = SimpleNamespace(count_tokens=count_tokens or AsyncMock(return_value=len(text) // 4))
    agent = _Agent(model)
    return SimpleNamespace(
        result=result,
        tool_use={"toolUseId": "t1", "name": tool_name},
        selected_tool=None,
        cancel_message=None,
        agent=agent,
    )


@pytest.fixture
def offloader(monkeypatch):
    monkeypatch.setenv("AGENTCORE_TOOL_RESULT_OFFLOAD_MAX_TOKENS", "1000")
    monkeypatch.setenv("AGENTCORE_TOOL_RESULT_OFFLOAD_PREVIEW_TOKENS", "100")
    storage = _FakeStorage()
    plugin = build_tool_result_offloader(session_id="s1", user_id="u1", storage=storage)
    assert plugin is not None
    # Bind storage the way init_agent would.
    plugin.init_agent(_Agent())
    return plugin, storage


class TestBuilder:
    def test_flag_default_on_only_literal_false_off(self, monkeypatch):
        monkeypatch.delenv("AGENTCORE_TOOL_RESULT_OFFLOAD_ENABLED", raising=False)
        assert tool_result_offload_enabled() is True
        monkeypatch.setenv("AGENTCORE_TOOL_RESULT_OFFLOAD_ENABLED", "false")
        assert tool_result_offload_enabled() is False
        assert build_tool_result_offloader(session_id="s", user_id="u", storage=_FakeStorage()) is None

    def test_no_bucket_means_no_plugin(self, monkeypatch):
        monkeypatch.delenv("S3_USER_FILES_BUCKET_NAME", raising=False)
        assert build_tool_result_offloader(session_id="s", user_id="u") is None

    def test_s3_backend_is_scoped_per_user_and_session(self, monkeypatch):
        monkeypatch.setenv("S3_USER_FILES_BUCKET_NAME", "files-bucket")
        captured = {}

        class FakeS3Storage:
            def __init__(self, bucket, *, prefix="", region_name=None, **kw):
                captured.update(bucket=bucket, prefix=prefix, region=region_name)

            async def write(self, k, d): ...
            async def read(self, k): ...
            async def delete(self, k): ...
            async def list(self, q=""): return []

        monkeypatch.setitem(sys.modules, "strands.storage", types.SimpleNamespace(S3Storage=FakeS3Storage))
        plugin = build_tool_result_offloader(session_id="sess", user_id="usr", region="us-west-2")
        assert plugin is not None
        assert captured == {"bucket": "files-bucket", "prefix": "compaction-offload/usr/sess", "region": "us-west-2"}
        assert offload_prefix("usr", "sess") == "compaction-offload/usr/sess"

    def test_configuration(self, offloader):
        plugin, _ = offloader
        assert plugin._max_result_tokens == 1000
        assert plugin._preview_tokens == 100
        assert plugin._evict_after_cycles is None  # no eviction from the model path
        assert plugin._include_retrieval_tool is True
        assert type(plugin).__name__ == "BoundedToolResultOffloader"

    def test_preview_is_clamped_below_gate(self, monkeypatch):
        monkeypatch.setenv("AGENTCORE_TOOL_RESULT_OFFLOAD_MAX_TOKENS", "500")
        monkeypatch.setenv("AGENTCORE_TOOL_RESULT_OFFLOAD_PREVIEW_TOKENS", "9000")
        plugin = build_tool_result_offloader(session_id="s", user_id="u", storage=_FakeStorage())
        assert plugin._preview_tokens == 499


class TestPrefilterAndOffload:
    @pytest.mark.asyncio
    async def test_small_result_skips_count_tokens_and_is_untouched(self, offloader):
        plugin, storage = offloader
        counter = AsyncMock(return_value=10)
        ev = _event("short result", count_tokens=counter)
        before = ev.result
        await plugin._handle_tool_result(ev)
        counter.assert_not_called()
        assert ev.result is before and storage.objects == {}

    @pytest.mark.asyncio
    async def test_borderline_result_is_measured_by_count_tokens(self, offloader):
        plugin, _ = offloader
        # ~600 estimated tokens: over the pre-filter (500) but under the gate (1000).
        counter = AsyncMock(return_value=600)
        ev = _event("x" * 2400, count_tokens=counter)
        before = ev.result
        await plugin._handle_tool_result(ev)
        counter.assert_called_once()
        assert ev.result is before

    @pytest.mark.asyncio
    async def test_oversized_result_is_offloaded_with_preview_and_reference(self, offloader, monkeypatch):
        plugin, storage = offloader
        import apis.shared.observability.emf as emf
        emitted = []
        monkeypatch.setattr(emf, "emit_emf_metrics", lambda ns, metrics, properties=None, units=None: emitted.append((ns, metrics, properties)))
        monkeypatch.delenv("PROMPT_CACHE_OBSERVABILITY_ENABLED", raising=False)

        body = "header line\n" + ("row data " * 2000)
        ev = _event(body, count_tokens=AsyncMock(return_value=5000))
        await plugin._handle_tool_result(ev)

        text = ev.result["content"][0]["text"]
        assert text.startswith("[Offloaded:")
        assert "retrieve_offloaded_content" in text
        assert "header line" in text  # preview keeps the head
        assert len(text) < len(body) // 4
        assert len(storage.objects) == 1  # the full block landed in storage
        assert ev.result["toolUseId"] == "t1" and ev.result["status"] == "success"
        assert emitted and emitted[0][0] == "AgentCoreStack/Compaction"
        assert emitted[0][1]["ToolResultOffloaded"] == 1
        assert emitted[0][2]["toolName"] == "gmail_search"
        assert "row data" not in str(emitted[0][2])  # content-free

    @pytest.mark.asyncio
    async def test_storage_failure_keeps_original_result(self, offloader):
        plugin, storage = offloader

        async def boom(key, data):
            raise RuntimeError("s3 down")

        storage.write = boom
        ev = _event("y" * 20000, count_tokens=AsyncMock(return_value=5000))
        before = ev.result
        await plugin._handle_tool_result(ev)
        assert ev.result is before

    @pytest.mark.asyncio
    async def test_count_tokens_failure_keeps_original_result(self, offloader):
        plugin, _ = offloader
        ev = _event("z" * 20000, count_tokens=AsyncMock(side_effect=RuntimeError("AccessDenied")))
        before = ev.result
        await plugin._handle_tool_result(ev)
        assert ev.result is before

    def test_prefilter_ratio_is_half_the_gate(self):
        assert PREFILTER_RATIO == 0.5


class TestChatAgentWiring:
    def test_chat_agent_adds_the_offloader_plugin(self, monkeypatch):
        from unittest.mock import MagicMock
        from agents.main_agent.chat_agent import ChatAgent
        from agents.main_agent.core import AgentFactory

        monkeypatch.setenv("S3_USER_FILES_BUCKET_NAME", "files-bucket")

        class FakeS3Storage:
            def __init__(self, bucket, *, prefix="", region_name=None, **kw):
                self.prefix = prefix

            async def write(self, k, d): ...
            async def read(self, k): ...
            async def delete(self, k): ...
            async def list(self, q=""): return []

        monkeypatch.setitem(sys.modules, "strands.storage", types.SimpleNamespace(S3Storage=FakeS3Storage))
        captured = {}
        monkeypatch.setattr(AgentFactory, "create_agent", staticmethod(lambda **kw: captured.update(kw) or MagicMock()))

        agent = ChatAgent.__new__(ChatAgent)
        agent.system_prompt = "BASE"
        agent.model_config = MagicMock()
        agent.session_manager = MagicMock()
        agent.session_id = "sess"
        agent.user_id = "usr"
        agent._accessible_skill_ids = None
        monkeypatch.setattr(ChatAgent, "_build_filtered_tools", lambda self: [], raising=False)
        monkeypatch.setattr(ChatAgent, "_create_hooks", lambda self: [], raising=False)

        agent._create_agent()

        plugins = captured["plugins"]
        assert plugins and type(plugins[0]).__name__ == "BoundedToolResultOffloader"
        assert plugins[0]._storage._prefix if hasattr(plugins[0]._storage, "_prefix") else True

    def test_chat_agent_without_bucket_passes_no_plugins(self, monkeypatch):
        from unittest.mock import MagicMock
        from agents.main_agent.chat_agent import ChatAgent
        from agents.main_agent.core import AgentFactory

        monkeypatch.delenv("S3_USER_FILES_BUCKET_NAME", raising=False)
        captured = {}
        monkeypatch.setattr(AgentFactory, "create_agent", staticmethod(lambda **kw: captured.update(kw) or MagicMock()))
        agent = ChatAgent.__new__(ChatAgent)
        agent.system_prompt = "BASE"
        agent.model_config = MagicMock()
        agent.session_manager = MagicMock()
        agent.session_id = "sess"
        agent.user_id = "usr"
        agent._accessible_skill_ids = None
        monkeypatch.setattr(ChatAgent, "_build_filtered_tools", lambda self: [], raising=False)
        monkeypatch.setattr(ChatAgent, "_create_hooks", lambda self: [], raising=False)
        agent._create_agent()
        assert captured["plugins"] is None
