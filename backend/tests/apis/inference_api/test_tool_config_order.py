"""The order of a representative turn's ``toolConfig``, pinned, and the fact it is not name order.

Bedrock prompt caching is exact-prefix-match and ``toolConfig`` is the first thing in
the prefix, so a re-ordered tool list re-writes every live conversation's cached
30k–150k-token prefix once, at the cache-write premium. The determinism guard
(``tests/agents/main_agent/test_prompt_cache_determinism.py``) proves the order is
stable across builds; this test records *what* the order is.

**Finding (strands-agents 1.63.0): our order is NOT name order.** Strands keeps
registration order (``ToolRegistry.get_all_tool_specs`` is dict insertion order, and
``BedrockModel.format_request`` keeps it). We register the registry's local tools in
enabled-id order, then the context-bound families in ``_build_turn_tools`` order
(spreadsheet, artifact, Word, workspace, Excel, PowerPoint, …, ``document_read``
last), each family in its builder's own order. ``analyze_spreadsheet`` follows
``list_spreadsheets`` and ``workspace_*`` sits between Word and Excel.

**For the Strands 1.65.0 bump.** harness-sdk #5700 may sort tool specs by name by
default. If it lands default-on, this test fails, and the bump has to choose: pass
``sort_tools=False`` (no prefix change), or accept one fleet-wide ``toolConfig``
re-write at deploy and price it in the PR (``scripts/measure_tool_spec_tokens.py``
gives the prefix size). Either way, don't just regenerate the list below.

A failure for any other reason is the same decision. If you added or renamed a
tool in one of these families, update ``EXPECTED_ORDER`` and say in the PR that the
change rewrites the prefix once. MCP and Gateway tools are left out on purpose:
their order comes from the server's ``tools/list``, not from us.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from strands import Agent

from agents.main_agent.core.bedrock_count_tokens import CountTokensBedrockModel
from agents.main_agent.core.model_config import ModelConfig
from agents.main_agent.tools.tool_filter import ToolFilter
from agents.main_agent.tools.tool_registry import create_default_registry
from apis.inference_api.chat import routes
from apis.shared.auth.models import User

# A turn with the registry's local tools and every enabled_tools-gated family a
# granted user commonly carries, plus an attachment (so ``document_read``).
ENABLED_TOOLS = sorted(
    [
        "ask_user_question",
        "browse_web",
        "calculator",
        "create_visualization",
        "fetch_url_content",
        "generate_diagram_and_validate",
        "list_spreadsheets",
        "analyze_spreadsheet",
        "create_artifact",
        "create_word_document",
        "workspace_files",
        "create_excel_spreadsheet",
        "create_powerpoint_presentation",
    ]
)

EXPECTED_ORDER = [
    # Registry tools, in enabled-id order.
    "ask_user_question",
    "browse_web",
    "calculator",
    "create_visualization",
    "fetch_url_content",
    "generate_diagram_and_validate",
    # Context-bound families, in _build_turn_tools order.
    "list_spreadsheets",
    "analyze_spreadsheet",
    "create_artifact",
    "update_artifact",
    "create_word_document",
    "modify_word_document",
    "list_word_documents",
    "read_word_document",
    "redline_word_document",
    "compare_word_documents",
    "workspace_list",
    "workspace_read",
    "workspace_write",
    "create_excel_spreadsheet",
    "modify_excel_spreadsheet",
    "list_excel_spreadsheets",
    "read_excel_spreadsheet",
    "create_powerpoint_presentation",
    "modify_powerpoint_presentation",
    "list_powerpoint_presentations",
    "read_powerpoint_presentation",
    "list_powerpoint_layouts",
    "document_read",
]


@pytest.fixture(autouse=True)
def _deployment_defaults(monkeypatch):
    """Pin the flags that add or remove a name here to their shipped defaults."""
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    for name in (
        "ASK_USER_QUESTION_ENABLED",
        "BROWSER_TAKEOVER_ENABLED",
        "WORD_TRACKED_CHANGES_ENABLED",
        "DOCUMENT_READ_ENABLED",
        "SOURCE_PROVENANCE_ENABLED",
        "DOCUMENT_EXTRACT_ENABLED",
        "AGENT_KNOWLEDGE_FILES_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)


async def _turn_tool_names() -> tuple[list[str], list[str]]:
    """(registry order, the order in the formatted Converse request)."""
    user = User(email="user@example.com", user_id="user-1", name="User One", roles=[])
    input_data = SimpleNamespace(rag_assistant_id=None, session_id="sess-order")

    async def _has_document(*_args, **_kwargs) -> bool:
        return True

    # The one gate with IO (the session's attachments); every builder is the real one.
    with patch.object(routes, "_document_tools_gate", side_effect=_has_document):
        turn_tools = await routes._build_turn_tools(
            input_data,
            user,
            user.user_id,
            ENABLED_TOOLS,
            project_memory=None,
            turn_has_document=True,
        )
    local_tools = ToolFilter(create_default_registry()).filter_tools_extended(ENABLED_TOOLS).local_tools

    # BaseAgent hands Strands local tools first, then extra_tools.
    model = MagicMock()
    model.config = {}
    agent = Agent(model=model, tools=local_tools + turn_tools.extra_tools)
    specs = agent.tool_registry.get_all_tool_specs()

    config = ModelConfig(
        model_id="us.anthropic.claude-sonnet-4-6",
        caching_enabled=True,
        inference_params={"max_tokens": 8192},
    )
    bedrock = CountTokensBedrockModel(native_projection=False, **config.to_bedrock_config())
    request = bedrock.format_request(
        [{"role": "user", "content": [{"text": "hi"}]}],
        specs,
        system_prompt_content=[{"text": "You are helpful."}],
    )
    sent = [entry["toolSpec"]["name"] for entry in request["toolConfig"]["tools"] if "toolSpec" in entry]
    return [spec["name"] for spec in specs], sent


@pytest.mark.asyncio
async def test_tool_config_order_is_pinned_and_is_not_name_order():
    registry_order, sent_order = await _turn_tool_names()

    assert sent_order == registry_order, (
        "the Converse request re-orders the registry's tools. Every live conversation "
        "re-writes its cached prefix once when this ships (see module docstring)"
    )
    assert sent_order == EXPECTED_ORDER, (
        "toolConfig order changed. If Strands now sorts tools by name (harness-sdk #5700), "
        "pass sort_tools=False or accept and price one fleet-wide prefix re-write; "
        "see this module's docstring before regenerating"
    )
    # The recorded finding: a name sort would move tools, so it is not a no-op for us.
    assert sent_order != sorted(sent_order)
