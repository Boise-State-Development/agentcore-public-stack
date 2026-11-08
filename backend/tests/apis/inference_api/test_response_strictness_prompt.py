"""Response strictness (#112): the grounding rule in an Agent turn's system prompt."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apis.inference_api.chat.routes import (
    RESPONSE_STRICTNESS_GUIDANCE,
    RESPONSE_STRICTNESS_HEADING,
    apply_response_strictness,
    compose_turn_system_prompt,
)

HEADING = "## Assistant-Specific Instructions"


def compose(*, instructions="Be helpful.", strictness="__absent__", project_id=None, personal=None, input_data=None):
    fields = {"instructions": instructions}
    if strictness != "__absent__":
        fields["response_strictness"] = strictness
    return compose_turn_system_prompt(
        input_data or SimpleNamespace(session_id="s-1", system_prompt=None),
        SimpleNamespace(**fields),
        system_prompt=None,
        project_id=project_id,
        personal_instructions=personal,
    )


def test_apply_returns_instructions_unchanged_for_balanced_and_unknown():
    assert apply_response_strictness("x", "balanced") == "x"
    assert apply_response_strictness("x", None) == "x"
    assert apply_response_strictness("x", "nonsense") == "x"


@pytest.mark.parametrize("strictness", ["balanced", None, "nonsense"])
def test_default_modes_are_byte_identical_to_an_agent_without_the_field(strictness):
    assert compose(strictness=strictness) == compose()


def test_strict_block_follows_instructions_and_precedes_personal_instructions():
    prompt = compose(strictness="strict", personal="Always search the web.")
    assert prompt.index("Be helpful.") < prompt.index(RESPONSE_STRICTNESS_HEADING) < prompt.index("## Personal Instructions")
    assert RESPONSE_STRICTNESS_GUIDANCE["strict"] in prompt
    assert "follow the Assistant-Specific Instructions" in prompt


def test_flexible_emits_its_own_text():
    prompt = compose(strictness="flexible")
    assert RESPONSE_STRICTNESS_GUIDANCE["flexible"] in prompt
    assert RESPONSE_STRICTNESS_GUIDANCE["strict"] not in prompt


def test_preview_override_still_gets_the_saved_strictness():
    preview = SimpleNamespace(session_id="preview-abc", system_prompt="Live form text")
    from apis.inference_api.chat.routes import is_preview_session

    if not is_preview_session(preview.session_id):
        pytest.skip("preview session id convention differs")
    prompt = compose(strictness="strict", input_data=preview)
    assert "Live form text" in prompt and RESPONSE_STRICTNESS_GUIDANCE["strict"] in prompt


def test_project_harness_never_gets_the_block():
    prompt = compose(strictness="strict", project_id="proj-1")
    assert RESPONSE_STRICTNESS_HEADING not in prompt


def test_strict_with_empty_instructions_still_gets_the_agent_heading_and_block():
    prompt = compose(instructions="", strictness="strict")
    assert HEADING in prompt
    assert prompt.index(HEADING) < prompt.index(RESPONSE_STRICTNESS_HEADING)
