"""
Tests for SystemPromptBuilder and DEFAULT_SYSTEM_PROMPT.

Requirements: 3.1–3.5
"""

from unittest.mock import patch


from agents.main_agent.core.system_prompt_builder import (
    DEFAULT_SYSTEM_PROMPT,
    SystemPromptBuilder,
)


# ---------------------------------------------------------------------------
# Requirement 3.5: DEFAULT_SYSTEM_PROMPT is a non-empty string
# ---------------------------------------------------------------------------
class TestDefaultSystemPrompt:
    """Verify DEFAULT_SYSTEM_PROMPT is defined and non-empty."""

    def test_is_non_empty_string(self):
        assert isinstance(DEFAULT_SYSTEM_PROMPT, str)
        assert len(DEFAULT_SYSTEM_PROMPT) > 0


# ---------------------------------------------------------------------------
# Requirement 3.1: build with include_date=True appends "Current date:" line
# ---------------------------------------------------------------------------
class TestBuildWithDateTrue:
    """When include_date is True, build appends a 'Current date:' line."""

    @patch(
        "agents.main_agent.core.system_prompt_builder.get_current_date_pacific",
        return_value="2024-06-15 (Saturday) 10:00 PDT",
    )
    def test_appends_current_date_line(self, mock_date):
        builder = SystemPromptBuilder()
        result = builder.build(include_date=True)

        assert result.endswith("Current date: 2024-06-15 (Saturday) 10:00 PDT")

    @patch(
        "agents.main_agent.core.system_prompt_builder.get_current_date_pacific",
        return_value="2024-06-15 (Saturday) 10:00 PDT",
    )
    def test_includes_base_prompt(self, mock_date):
        builder = SystemPromptBuilder()
        result = builder.build(include_date=True)

        assert result.startswith(DEFAULT_SYSTEM_PROMPT)

    @patch(
        "agents.main_agent.core.system_prompt_builder.get_current_date_pacific",
        return_value="2024-01-01 (Monday) 08:00 PST",
    )
    def test_date_separated_by_blank_line(self, mock_date):
        builder = SystemPromptBuilder()
        result = builder.build(include_date=True)

        expected = f"{DEFAULT_SYSTEM_PROMPT}\n\nCurrent date: 2024-01-01 (Monday) 08:00 PST"
        assert result == expected


# ---------------------------------------------------------------------------
# Requirement 3.2: build with include_date=False returns base prompt unchanged
# ---------------------------------------------------------------------------
class TestBuildWithDateFalse:
    """When include_date is False, build returns the base prompt unchanged."""

    def test_returns_default_prompt_unchanged(self):
        builder = SystemPromptBuilder()
        result = builder.build(include_date=False)

        assert result == DEFAULT_SYSTEM_PROMPT

    def test_returns_custom_prompt_unchanged(self):
        custom = "You are a helpful assistant."
        builder = SystemPromptBuilder(base_prompt=custom)
        result = builder.build(include_date=False)

        assert result == custom

    def test_no_current_date_in_output(self):
        builder = SystemPromptBuilder()
        result = builder.build(include_date=False)

        assert "Current date:" not in result


# ---------------------------------------------------------------------------
# Requirement 3.3: custom base_prompt overrides DEFAULT_SYSTEM_PROMPT
# ---------------------------------------------------------------------------
class TestCustomBasePrompt:
    """When a custom base_prompt is provided, it replaces DEFAULT_SYSTEM_PROMPT."""

    def test_uses_custom_prompt(self):
        custom = "Custom system instructions for testing."
        builder = SystemPromptBuilder(base_prompt=custom)

        assert builder.base_prompt == custom

    @patch(
        "agents.main_agent.core.system_prompt_builder.get_current_date_pacific",
        return_value="2024-03-20 (Wednesday) 15:00 PDT",
    )
    def test_build_with_date_uses_custom_prompt(self, mock_date):
        custom = "Custom prompt."
        builder = SystemPromptBuilder(base_prompt=custom)
        result = builder.build(include_date=True)

        assert result == "Custom prompt.\n\nCurrent date: 2024-03-20 (Wednesday) 15:00 PDT"

    def test_none_base_prompt_falls_back_to_default(self):
        builder = SystemPromptBuilder(base_prompt=None)

        assert builder.base_prompt == DEFAULT_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Requirement 3.4: from_user_prompt wraps the user prompt with the platform
# safety floor.
# ---------------------------------------------------------------------------
class TestFromUserPrompt:
    """from_user_prompt sandwiches a user-supplied prompt below the platform
    safety floor and wraps it in <user_instructions> tags.
    """

    def test_assembled_prompt_starts_with_safety_floor(self):
        from agents.main_agent.core.system_prompt_builder import PLATFORM_SAFETY_FLOOR

        user_prompt = "You are a research assistant for biology."
        builder = SystemPromptBuilder.from_user_prompt(user_prompt)

        assert builder.base_prompt.startswith(PLATFORM_SAFETY_FLOOR)

    def test_assembled_prompt_contains_user_text_inside_tags(self):
        user_prompt = "You are a research assistant for biology."
        builder = SystemPromptBuilder.from_user_prompt(user_prompt)

        assert "<user_instructions>" in builder.base_prompt
        assert "</user_instructions>" in builder.base_prompt
        # User text appears inside the tags.
        opening = builder.base_prompt.index("<user_instructions>")
        closing = builder.base_prompt.index("</user_instructions>")
        assert opening < builder.base_prompt.index(user_prompt) < closing

    def test_returns_system_prompt_builder_instance(self):
        builder = SystemPromptBuilder.from_user_prompt("Any prompt")

        assert isinstance(builder, SystemPromptBuilder)

    def test_build_without_date_includes_floor_and_user_prompt(self):
        from agents.main_agent.core.system_prompt_builder import PLATFORM_SAFETY_FLOOR

        user_prompt = "User-provided prompt with date already."
        builder = SystemPromptBuilder.from_user_prompt(user_prompt)
        result = builder.build(include_date=False)

        assert result.startswith(PLATFORM_SAFETY_FLOOR)
        assert user_prompt in result
