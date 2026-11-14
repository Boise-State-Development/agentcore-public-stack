"""Which models refuse a forced toolChoice on Converse (probed on dev 2026-11-14)."""

import pytest

from apis.shared.models.forced_tool_choice import model_accepts_forced_tool_choice


@pytest.mark.parametrize(
    "model_id",
    [
        # Measured: `any` and `tool` both 400 before inference.
        "us.anthropic.claude-opus-5-5",
        "global.anthropic.claude-opus-5-5",
        "us.anthropic.claude-sonnet-5-5",
        # Anthropic's published contract (dev cannot call these yet).
        "global.anthropic.claude-fable-5-2",
        "us.anthropic.claude-fable-5-1",
        "anthropic.claude-mythos-5-2",
        "anthropic.claude-mythos-5-1",
        # A later release in a refusing line is assumed to refuse too.
        "anthropic.claude-opus-6",
        "anthropic.claude-fable-5-10",
    ],
)
def test_refusing_models(model_id):
    assert model_accepts_forced_tool_choice(model_id) is False


@pytest.mark.parametrize(
    "model_id",
    [
        # Measured: both forced choices answered with the tool.
        "us.anthropic.claude-haiku-5-5",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        # Earlier releases in the refusing lines.
        "global.anthropic.claude-sonnet-5",
        "anthropic.claude-opus-5",
        "anthropic.claude-opus-5-20260101-v1:0",
        "us.anthropic.claude-opus-4-7",
        "us.anthropic.claude-sonnet-4-6",
        "anthropic.claude-fable-5",
        "anthropic.claude-3-7-sonnet-20250219-v1:0",
        # Not Claude, or no id at all.
        "us.amazon.nova-micro-v1:0",
        "openai.gpt-oss-120b-1:0",
        "",
        None,
    ],
)
def test_accepting_models(model_id):
    assert model_accepts_forced_tool_choice(model_id) is True


def test_case_does_not_matter():
    assert model_accepts_forced_tool_choice("US.ANTHROPIC.CLAUDE-OPUS-5-5") is False
