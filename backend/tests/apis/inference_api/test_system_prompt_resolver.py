"""Tests for the inference-api system prompt resolver.

Covers the gating rules and the resolution path. The route itself is too
heavy to set up in a unit test; the helpers live in their own module so
the rules can be asserted directly.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apis.inference_api.chat.system_prompt_resolver import (
    append_active_prompt,
    resolve_active_prompt_text,
    should_resolve_custom_prompt,
)


# ---------------------------------------------------------------------------
# Gating rules
# ---------------------------------------------------------------------------

class TestShouldResolveCustomPrompt:
    def test_plain_turn_resolves(self):
        assert should_resolve_custom_prompt(
            is_resume=False,
            is_continuation=False,
            is_preview=False,
            has_assistant=False,
        ) is True

    @pytest.mark.parametrize(
        "flag",
        ["is_resume", "is_continuation", "is_preview", "has_assistant"],
    )
    def test_any_flag_skips(self, flag):
        kwargs = dict(
            is_resume=False,
            is_continuation=False,
            is_preview=False,
            has_assistant=False,
        )
        kwargs[flag] = True
        assert should_resolve_custom_prompt(**kwargs) is False


# ---------------------------------------------------------------------------
# Composition format
# ---------------------------------------------------------------------------

class TestAppendActivePrompt:
    def test_appends_with_active_mode_header(self):
        result = append_active_prompt("base", "Guided Learning", "be socratic")
        assert "base" in result
        assert "## Active Mode: Guided Learning" in result
        assert "be socratic" in result

    def test_separator_is_blank_line(self):
        # The base prompt is separated from the mode header by exactly one
        # blank line. Loose joins like a single newline run the risk of the
        # mode header collapsing into the previous line.
        result = append_active_prompt("base", "Mode", "text")
        assert "base\n\n## Active Mode: Mode\n\ntext" == result


# ---------------------------------------------------------------------------
# resolve_active_prompt_text
# ---------------------------------------------------------------------------

@pytest.fixture()
def patched_session_meta():
    """Patches get_session_metadata used inside the resolver module."""
    with patch(
        "apis.inference_api.chat.system_prompt_resolver.get_session_metadata",
        new_callable=AsyncMock,
    ) as mock_get_meta:
        yield mock_get_meta


@pytest.fixture()
def patched_service():
    """Patches the system prompts service factory used by the resolver."""
    with patch(
        "apis.inference_api.chat.system_prompt_resolver.get_system_prompts_service"
    ) as mock_factory:
        service = MagicMock()
        service.get_enabled_prompt = AsyncMock(return_value=None)
        mock_factory.return_value = service
        yield service


@pytest.fixture()
def patched_persist():
    """Patches the persistence write so we can assert it without DynamoDB."""
    with patch(
        "apis.inference_api.chat.system_prompt_resolver.set_selected_prompt_id",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_persist:
        yield mock_persist


@pytest.mark.asyncio
async def test_returns_none_when_no_session_metadata(patched_session_meta, patched_service):
    patched_session_meta.return_value = None

    result = await resolve_active_prompt_text(session_id="sess", user_id="user")

    assert result is None
    patched_service.get_enabled_prompt.assert_not_called()


@pytest.mark.asyncio
async def test_returns_none_when_no_active_prompt(patched_session_meta, patched_service):
    meta = MagicMock()
    meta.preferences = MagicMock(selected_prompt_id=None)
    patched_session_meta.return_value = meta

    result = await resolve_active_prompt_text(session_id="sess", user_id="user")

    assert result is None
    patched_service.get_enabled_prompt.assert_not_called()


@pytest.mark.asyncio
async def test_returns_name_and_text_when_prompt_is_enabled(
    patched_session_meta, patched_service
):
    meta = MagicMock()
    meta.preferences = MagicMock(selected_prompt_id="p-1")
    patched_session_meta.return_value = meta

    enabled_prompt = MagicMock(name="Guided", prompt_text="be socratic")
    enabled_prompt.name = "Guided"  # MagicMock 'name' kwarg is special
    patched_service.get_enabled_prompt = AsyncMock(return_value=enabled_prompt)

    result = await resolve_active_prompt_text(session_id="sess", user_id="user")

    assert result == ("Guided", "be socratic")
    patched_service.get_enabled_prompt.assert_awaited_once_with("p-1")


@pytest.mark.asyncio
async def test_returns_none_when_prompt_disabled_or_missing(
    patched_session_meta, patched_service
):
    """Service returns None for both missing and disabled — both paths
    must skip silently rather than fail the turn."""
    meta = MagicMock()
    meta.preferences = MagicMock(selected_prompt_id="ghost-id")
    patched_session_meta.return_value = meta

    patched_service.get_enabled_prompt = AsyncMock(return_value=None)

    result = await resolve_active_prompt_text(session_id="sess", user_id="user")

    assert result is None


@pytest.mark.asyncio
async def test_swallows_exceptions(patched_session_meta, patched_service):
    """A bug or an outage in the lookup must never break a conversation."""
    patched_session_meta.side_effect = RuntimeError("dynamodb is down")

    result = await resolve_active_prompt_text(session_id="sess", user_id="user")

    assert result is None


# ---------------------------------------------------------------------------
# request_prompt_id precedence + persistence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_prompt_id_takes_precedence_over_metadata(
    patched_session_meta, patched_service, patched_persist
):
    """When the request carries a prompt id, the resolver uses it without
    even fetching session metadata. This is the first-turn-of-new-session
    case where no metadata row exists yet."""
    enabled_prompt = MagicMock(prompt_text="from request id")
    enabled_prompt.name = "FromRequest"
    patched_service.get_enabled_prompt = AsyncMock(return_value=enabled_prompt)

    result = await resolve_active_prompt_text(
        session_id="sess",
        user_id="user",
        request_prompt_id="req-prompt-id",
    )

    assert result == ("FromRequest", "from request id")
    patched_service.get_enabled_prompt.assert_awaited_once_with("req-prompt-id")
    # Metadata fetch is skipped when the request supplies the id.
    patched_session_meta.assert_not_called()
    # The id is mirrored onto the session row for refresh / new-device support.
    patched_persist.assert_awaited_once_with("sess", "user", "req-prompt-id")


@pytest.mark.asyncio
async def test_falls_back_to_metadata_when_request_has_no_id(
    patched_session_meta, patched_service, patched_persist
):
    """No request id ⇒ resolve from metadata. No persist (the metadata
    already holds the id we'd be writing back)."""
    meta = MagicMock()
    meta.preferences = MagicMock(selected_prompt_id="from-meta-id")
    patched_session_meta.return_value = meta

    enabled_prompt = MagicMock(prompt_text="from meta")
    enabled_prompt.name = "FromMeta"
    patched_service.get_enabled_prompt = AsyncMock(return_value=enabled_prompt)

    result = await resolve_active_prompt_text(session_id="sess", user_id="user")

    assert result == ("FromMeta", "from meta")
    patched_service.get_enabled_prompt.assert_awaited_once_with("from-meta-id")
    patched_persist.assert_not_called()


@pytest.mark.asyncio
async def test_request_id_disabled_returns_none_and_skips_persist(
    patched_session_meta, patched_service, patched_persist
):
    """A disabled or missing request prompt id must not be persisted —
    we don't want to stamp a known-bad id onto the session row."""
    patched_service.get_enabled_prompt = AsyncMock(return_value=None)

    result = await resolve_active_prompt_text(
        session_id="sess",
        user_id="user",
        request_prompt_id="disabled-id",
    )

    assert result is None
    patched_persist.assert_not_called()
