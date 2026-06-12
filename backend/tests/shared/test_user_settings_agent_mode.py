"""Tests for the preferredAgentMode user setting (skills-mode PR-3)."""

import pytest
from pydantic import ValidationError

from apis.shared.user_settings.models import UserSettings, UserSettingsUpdate
from apis.shared.user_settings.repository import DEFAULT_SETTINGS


class TestPreferredAgentModeModels:
    def test_settings_accept_camel_case_alias(self):
        settings = UserSettings.model_validate({"preferredAgentMode": "chat"})
        assert settings.preferred_agent_mode == "chat"

    def test_update_rejects_unknown_mode(self):
        with pytest.raises(ValidationError):
            UserSettingsUpdate.model_validate({"preferredAgentMode": "voice"})

    def test_defaults_to_none(self):
        assert UserSettings().preferred_agent_mode is None


class TestRepositoryDefaults:
    def test_default_settings_include_preferred_agent_mode(self):
        # The repository's get_settings extracts a fixed key set from the
        # Dynamo item; a key missing from DEFAULT_SETTINGS would be silently
        # dropped on read.
        assert "preferredAgentMode" in DEFAULT_SETTINGS
        assert DEFAULT_SETTINGS["preferredAgentMode"] is None
