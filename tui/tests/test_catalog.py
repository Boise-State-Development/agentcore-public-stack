"""Tests for the JSON API client.

Every request is served by ``httpx.MockTransport``. The assertions concentrate on
the wire facts that are easy to get wrong and fail *silently* when you do:

* app-api mixes casing conventions, so reading a field under the wrong one
  yields a default rather than an error;
* the preference endpoints take a **map**, not a list of enabled ids, and a list
  cannot express "off";
* ``mark_read`` and ``generate_title`` are best-effort and must never raise;
* a message's content is a list of typed blocks, and an unknown block type must
  not lose the message.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from agentcore_tui.client.auth import SessionAuth
from agentcore_tui.client.catalog import (
    CatalogClient,
    ConversationSummary,
    HistoryMessage,
    Model,
    Skill,
    SystemPromptOption,
    Tool,
)
from agentcore_tui.errors import ApiError, AuthError, BadRequestError, ConfigError, ConnectionFailedError

BASE_URL = "https://catalog.invalid/api"


def build(handler: Any, *, capture: list[httpx.Request] | None = None) -> CatalogClient:
    def wrapped(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        return handler(request)

    return CatalogClient(
        BASE_URL,
        auth=SessionAuth("sealed"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(wrapped)),
    )


def json_handler(payload: dict[str, Any], status: int = 200) -> Any:
    return lambda _request: httpx.Response(status, json=payload)


# ---------------------------------------------------------------------------
# Catalogues
# ---------------------------------------------------------------------------


MODEL_WIRE = {
    "id": "row-1",
    "modelId": "us.anthropic.claude-haiku-4-5",
    "modelName": "Claude Haiku 4.5",
    "provider": "bedrock",
    "providerName": "Bedrock",
    "inputModalities": ["text", "image"],
    "outputModalities": ["text"],
    "maxInputTokens": 200000,
    "maxOutputTokens": 8192,
    "enabled": True,
    "inputPricePerMillionTokens": 0.8,
    "outputPricePerMillionTokens": 4.0,
    "createdAt": "x",
    "updatedAt": "y",
}


class TestModels:
    async def test_parses_the_camelcase_catalogue(self) -> None:
        async with build(json_handler({"models": [MODEL_WIRE], "totalCount": 1})) as api:
            models = await api.models()

        assert models == [
            Model(
                model_id="us.anthropic.claude-haiku-4-5",
                provider="bedrock",
                name="Claude Haiku 4.5",
                provider_name="Bedrock",
                max_input_tokens=200000,
                max_output_tokens=8192,
                input_price_per_million=0.8,
                output_price_per_million=4.0,
                supports_images=True,
            )
        ]

    async def test_the_provider_travels_with_the_id(self) -> None:
        """The pair is one decision — see `Model.selection`."""
        async with build(json_handler({"models": [MODEL_WIRE]})) as api:
            model = (await api.models())[0]
        assert model.selection == ("us.anthropic.claude-haiku-4-5", "bedrock")

    async def test_a_text_only_model_does_not_claim_images(self) -> None:
        wire = {**MODEL_WIRE, "inputModalities": ["text"]}
        async with build(json_handler({"models": [wire]})) as api:
            assert (await api.models())[0].supports_images is False

    async def test_an_empty_catalogue_is_not_an_error(self) -> None:
        async with build(json_handler({"models": [], "totalCount": 0})) as api:
            assert await api.models() == []

    async def test_requests_carry_the_session_header(self) -> None:
        captured: list[httpx.Request] = []
        async with build(json_handler({"models": []}), capture=captured) as api:
            await api.models()
        assert captured[0].headers["authorization"] == "BFF sealed"


class TestTools:
    async def test_uses_the_servers_resolved_enabled_state(self) -> None:
        """`isEnabled` already folds in the role default and any user override,
        so the client shows that rather than recomputing it."""
        wire = {
            "toolId": "calculator",
            "displayName": "Calculator",
            "description": "Arithmetic",
            "category": "utility",
            "protocol": "local",
            "status": "active",
            "grantedBy": ["role:everyone"],
            "enabledByDefault": False,
            "userEnabled": True,
            "isEnabled": True,
        }
        async with build(json_handler({"tools": [wire]})) as api:
            tools = await api.tools()

        assert tools == [
            Tool(
                tool_id="calculator",
                name="Calculator",
                description="Arithmetic",
                category="utility",
                protocol="local",
                status="active",
                enabled=True,
                enabled_by_default=False,
            )
        ]

    async def test_reads_the_trailing_slash_path(self) -> None:
        """The bare path answers 307, which costs a round trip on every read."""
        captured: list[httpx.Request] = []
        async with build(json_handler({"tools": []}), capture=captured) as api:
            await api.tools()
        assert str(captured[0].url) == f"{BASE_URL}/tools/"

    async def test_an_unhealthy_tool_is_reported_not_hidden(self) -> None:
        wire = {
            "toolId": "flaky",
            "displayName": "Flaky",
            "description": "",
            "category": "custom",
            "protocol": "mcp_external",
            "status": "unreachable",
            "grantedBy": [],
            "enabledByDefault": True,
            "isEnabled": True,
        }
        async with build(json_handler({"tools": [wire]})) as api:
            tool = (await api.tools())[0]
        assert tool.available is False
        assert tool.enabled is True

    async def test_an_oauth_tool_is_flagged(self) -> None:
        wire = {
            "toolId": "gdrive",
            "displayName": "Drive",
            "description": "",
            "category": "search",
            "protocol": "mcp",
            "status": "active",
            "grantedBy": [],
            "enabledByDefault": False,
            "isEnabled": False,
            "requiresOauthProvider": "google",
        }
        async with build(json_handler({"tools": [wire]})) as api:
            assert (await api.tools())[0].requires_oauth_provider == "google"


class TestPreferences:
    async def test_tool_preferences_are_sent_as_a_map(self) -> None:
        """A list of enabled ids could not express "I turned this off", which is
        the case that matters for a tool a role enables by default."""
        captured: list[httpx.Request] = []
        async with build(json_handler({}), capture=captured) as api:
            await api.save_tool_preferences({"calculator": True, "browser": False})

        assert captured[0].method == "PUT"
        assert str(captured[0].url) == f"{BASE_URL}/tools/preferences"
        assert json.loads(captured[0].content) == {"preferences": {"calculator": True, "browser": False}}

    async def test_skill_preferences_use_the_same_shape(self) -> None:
        captured: list[httpx.Request] = []
        async with build(json_handler({}), capture=captured) as api:
            await api.save_skill_preferences({"research": True})
        assert json.loads(captured[0].content) == {"preferences": {"research": True}}

    async def test_an_empty_body_response_is_fine(self) -> None:
        """204 and an empty 200 are both legitimate for these writes."""
        async with build(lambda _r: httpx.Response(204)) as api:
            await api.save_tool_preferences({"a": True})


class TestSkillsAndPrompts:
    async def test_skills_parse(self) -> None:
        wire = {"skillId": "research", "displayName": "Web Research", "description": "d", "isEnabled": True}
        async with build(json_handler({"skills": [wire], "totalCount": 1})) as api:
            assert await api.skills() == [Skill(skill_id="research", name="Web Research", description="d", enabled=True)]

    async def test_system_prompts_use_snake_case(self) -> None:
        """This endpoint does not follow the catalogue's camelCase convention —
        reading `promptId` here would silently yield an empty id."""
        wire = {"prompt_id": "concise", "name": "Concise", "description": "Short answers"}
        async with build(json_handler({"prompts": [wire], "total": 1})) as api:
            assert await api.system_prompts() == [SystemPromptOption(prompt_id="concise", name="Concise", description="Short answers")]

    async def test_a_deployment_with_no_prompts_is_normal(self) -> None:
        async with build(json_handler({"prompts": [], "total": 0})) as api:
            assert await api.system_prompts() == []


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


SESSION_WIRE = {
    "sessionId": "sess-1",
    "title": "Arithmetic",
    "status": "active",
    "createdAt": "2026-08-08T10:00:00Z",
    "lastMessageAt": "2026-08-08T11:00:00Z",
    "messageCount": 4,
    "lastContextTokens": 4000,
    "contextWindow": 200000,
    "unread": True,
    "lastTurnContinuable": False,
}


class TestConversations:
    async def test_parses_a_page(self) -> None:
        async with build(json_handler({"sessions": [SESSION_WIRE], "nextToken": "cursor-2"})) as api:
            page = await api.conversations()

        assert page.has_more
        assert page.next_token == "cursor-2"
        assert page.items == [
            ConversationSummary(
                session_id="sess-1",
                title="Arithmetic",
                message_count=4,
                last_message_at="2026-08-08T11:00:00Z",
                created_at="2026-08-08T10:00:00Z",
                unread=True,
                context_tokens=4000,
                context_window=200000,
            )
        ]

    async def test_context_percent_is_derived(self) -> None:
        async with build(json_handler({"sessions": [SESSION_WIRE]})) as api:
            assert (await api.conversations()).items[0].context_percent == 2

    async def test_context_percent_is_none_without_both_numbers(self) -> None:
        wire = {**SESSION_WIRE, "contextWindow": None}
        async with build(json_handler({"sessions": [wire]})) as api:
            assert (await api.conversations()).items[0].context_percent is None

    async def test_an_untitled_conversation_gets_a_placeholder(self) -> None:
        wire = {**SESSION_WIRE, "title": ""}
        async with build(json_handler({"sessions": [wire]})) as api:
            assert (await api.conversations()).items[0].title == "Untitled"

    async def test_the_cursor_is_passed_through(self) -> None:
        captured: list[httpx.Request] = []
        async with build(json_handler({"sessions": []}), capture=captured) as api:
            await api.conversations(limit=10, next_token="cursor-2")
        assert captured[0].url.params["next_token"] == "cursor-2"
        assert captured[0].url.params["limit"] == "10"

    async def test_no_cursor_is_sent_on_the_first_page(self) -> None:
        captured: list[httpx.Request] = []
        async with build(json_handler({"sessions": []}), capture=captured) as api:
            await api.conversations()
        assert "next_token" not in captured[0].url.params


class TestHistory:
    async def test_flattens_text_blocks(self) -> None:
        wire = {
            "messages": [
                {"id": "m1", "role": "user", "createdAt": "t", "content": [{"type": "text", "text": "hi"}]},
                {
                    "id": "m2",
                    "role": "assistant",
                    "createdAt": "t",
                    "content": [{"type": "text", "text": "he"}, {"type": "text", "text": "llo"}],
                },
            ]
        }
        async with build(json_handler(wire)) as api:
            page = await api.history("sess-1")

        assert [(m.role, m.text) for m in page.items] == [("user", "hi"), ("assistant", "hello")]

    async def test_counts_tool_and_attachment_blocks_without_losing_the_message(self) -> None:
        """A tool-call block has nothing to render in a transcript, but the
        message it belongs to must still exist."""
        wire = {
            "messages": [
                {
                    "id": "m1",
                    "role": "assistant",
                    "createdAt": "t",
                    "content": [
                        {"type": "text", "text": "Let me check."},
                        {"type": "toolUse", "toolUse": {"name": "calculator"}},
                        {"type": "toolResult", "toolResult": {"status": "success"}},
                        {"type": "image", "image": {}},
                    ],
                }
            ]
        }
        async with build(json_handler(wire)) as api:
            message = (await api.history("sess-1")).items[0]

        assert message.text == "Let me check."
        assert message.tool_blocks == 2
        assert message.attachment_blocks == 1

    async def test_an_unknown_block_type_is_recorded_not_dropped(self) -> None:
        """The schema says "etc.", so a client must tolerate new block types."""
        wire = {"messages": [{"id": "m1", "role": "assistant", "createdAt": "t", "content": [{"type": "somethingNew"}]}]}
        async with build(json_handler(wire)) as api:
            message = (await api.history("sess-1")).items[0]
        assert message.other_blocks == ("somethingNew",)
        assert message.message_id == "m1"

    async def test_reasoning_is_kept_separate_from_prose(self) -> None:
        wire = {
            "messages": [
                {
                    "id": "m1",
                    "role": "assistant",
                    "createdAt": "t",
                    "content": [
                        {"type": "reasoningContent", "reasoningContent": {"text": "thinking"}},
                        {"type": "text", "text": "answer"},
                    ],
                }
            ]
        }
        async with build(json_handler(wire)) as api:
            message = (await api.history("sess-1")).items[0]
        assert message.reasoning == "thinking"
        assert message.text == "answer"

    async def test_missing_content_does_not_raise(self) -> None:
        wire = {"messages": [{"id": "m1", "role": "user", "createdAt": "t"}]}
        async with build(json_handler(wire)) as api:
            assert (await api.history("sess-1")).items == [HistoryMessage(message_id="m1", role="user", created_at="t")]


class TestMutations:
    async def test_rename_puts_the_title_to_metadata(self) -> None:
        captured: list[httpx.Request] = []
        async with build(json_handler({}), capture=captured) as api:
            await api.rename("sess-1", "New name")
        assert captured[0].method == "PUT"
        assert str(captured[0].url) == f"{BASE_URL}/sessions/sess-1/metadata"
        assert json.loads(captured[0].content) == {"title": "New name"}

    async def test_delete_uses_the_session_path(self) -> None:
        captured: list[httpx.Request] = []
        async with build(json_handler({}), capture=captured) as api:
            await api.delete("sess-1")
        assert captured[0].method == "DELETE"
        assert str(captured[0].url) == f"{BASE_URL}/sessions/sess-1"

    async def test_bulk_delete_sends_camelcase_ids(self) -> None:
        captured: list[httpx.Request] = []
        async with build(json_handler({}), capture=captured) as api:
            await api.delete_many(["a", "b"])
        assert json.loads(captured[0].content) == {"sessionIds": ["a", "b"]}

    async def test_mark_read_and_unread_hit_different_paths(self) -> None:
        captured: list[httpx.Request] = []
        async with build(json_handler({}), capture=captured) as api:
            assert await api.mark_read("sess-1") is True
            assert await api.mark_read("sess-1", read=False) is True
        assert [str(request.url).rsplit("/", 1)[-1] for request in captured] == ["read", "unread"]

    async def test_mark_read_never_raises(self) -> None:
        """Best-effort, like the web app. A terminal that refused to open a
        conversation because a read receipt failed would be worse than one that
        quietly disagrees about a bold row."""
        async with build(json_handler({"detail": "nope"}, status=500)) as api:
            assert await api.mark_read("sess-1") is False

    async def test_generate_title_returns_none_on_failure(self) -> None:
        async with build(json_handler({"detail": "quota"}, status=429)) as api:
            assert await api.generate_title("sess-1", "hello") is None

    async def test_generate_title_uses_the_snake_case_body(self) -> None:
        """This endpoint follows the `/chat/*` convention, and the field is
        `input` rather than `message`."""
        captured: list[httpx.Request] = []
        async with build(json_handler({"title": "Greetings"}), capture=captured) as api:
            assert await api.generate_title("sess-1", "hello there") == "Greetings"
        assert json.loads(captured[0].content) == {"session_id": "sess-1", "input": "hello there"}


class TestErrors:
    def test_requires_a_base_url(self) -> None:
        with pytest.raises(ConfigError, match="base URL"):
            CatalogClient("", auth=SessionAuth("s"))

    @pytest.mark.parametrize(
        ("status", "expected"),
        [(401, AuthError), (403, ApiError), (404, ApiError), (400, BadRequestError), (422, BadRequestError)],
    )
    async def test_status_codes_map_to_typed_errors(self, status: int, expected: type[Exception]) -> None:
        async with build(json_handler({"detail": "no"}, status=status)) as api:
            with pytest.raises(expected):
                await api.models()

    async def test_a_404_hints_at_the_likely_cause(self) -> None:
        async with build(json_handler({"detail": "gone"}, status=404)) as api:
            with pytest.raises(ApiError) as caught:
                await api.history("sess-1")
        assert "deleted" in caught.value.hint

    async def test_connection_failure_names_the_host(self) -> None:
        def explode(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        async with build(explode) as api:
            with pytest.raises(ConnectionFailedError, match="catalog.invalid"):
                await api.models()

    async def test_a_non_json_body_does_not_raise(self) -> None:
        """A proxy can return HTML with a 200."""
        async with build(lambda _r: httpx.Response(200, text="<html>hi</html>")) as api:
            assert await api.models() == []


class TestLifecycle:
    async def test_does_not_close_an_injected_client(self) -> None:
        injected = httpx.AsyncClient(transport=httpx.MockTransport(json_handler({"models": []})))
        async with CatalogClient(BASE_URL, auth=SessionAuth("s"), client=injected):
            pass
        assert not injected.is_closed
        await injected.aclose()
