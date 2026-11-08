"""A continuation never ends its request on an assistant message for a model that refuses prefill.

Prod regression: "Continue" after a max_tokens cut, or after an interrupted
turn, sent an empty prompt, so the request ended on the partial assistant
message. Sonnet 5 rejected every one with "This model does not support
assistant message prefill", and each retry of Continue failed the same way.
"""

import copy

import pytest

from apis.shared.models.assistant_prefill import (
    CONTINUATION_PROMPT,
    continuation_prompt,
    model_accepts_assistant_prefill,
)


def _user(text):
    return {"role": "user", "content": [{"text": text}]}


def _assistant(text):
    return {"role": "assistant", "content": [{"text": text}]}


@pytest.mark.parametrize(
    "model_id",
    [
        "global.anthropic.claude-sonnet-5",
        "us.anthropic.claude-sonnet-4-6",
        "us.anthropic.claude-opus-4-6-v1",
        "us.anthropic.claude-opus-4-7",
        "us.anthropic.claude-opus-5-5",
        "global.anthropic.claude-haiku-5-5",
        "global.anthropic.claude-fable-5-1",
        "claude-sonnet-5-5",
        "us.anthropic.claude-sonnet-4-6[1m]",
        # A future version refuses too, without a code change.
        "global.anthropic.claude-sonnet-6",
    ],
)
def test_claude_4_6_and_later_refuse_prefill(model_id):
    assert model_accepts_assistant_prefill(model_id) is False


@pytest.mark.parametrize(
    "model_id",
    [
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "us.anthropic.claude-opus-4-1-20250805-v1:0",
        # A date stamp after the major version is not a minor version.
        "us.anthropic.claude-opus-4-20250514-v1:0",
        "us.anthropic.claude-sonnet-4-20250514-v1:0",
        "anthropic.claude-3-5-sonnet-20240620-v1:0",
        "us.amazon.nova-pro-v1:0",
        "openai.gpt-5.4",
        "",
        None,
    ],
)
def test_older_claude_and_other_models_keep_prefill(model_id):
    assert model_accepts_assistant_prefill(model_id) is True


@pytest.mark.parametrize(
    "model_id",
    [
        "us.openai.gpt-5.5",
        "us.openai.gpt-5.6-sol",
        "us.openai.gpt-6.1-sol",
        # The provider decides, not the id: any model on the Responses API.
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "",
        None,
    ],
)
@pytest.mark.parametrize("provider", ["bedrock-responses", " Bedrock-Responses "])
def test_responses_api_models_do_not_continue_from_prefill(model_id, provider):
    # Measured on dev: a request ending on the partial starts the answer over.
    assert model_accepts_assistant_prefill(model_id, provider) is False


@pytest.mark.parametrize("provider", [None, "", "bedrock", "mantle", "openai", "gemini"])
def test_other_providers_keep_the_model_id_rule(provider):
    assert model_accepts_assistant_prefill("us.openai.gpt-5.6-sol", provider) is True
    assert model_accepts_assistant_prefill("global.anthropic.claude-sonnet-5", provider) is False


def test_continuation_on_a_responses_api_model_gets_a_continue_turn():
    history = [_user("Write a long essay."), _assistant("The first part of the essay")]
    assert continuation_prompt([], history, "us.openai.gpt-5.6-sol", "bedrock-responses") == CONTINUATION_PROMPT
    # Without the provider the GPT id alone keeps today's behaviour.
    assert continuation_prompt([], history, "us.openai.gpt-5.6-sol") == []


def test_continuation_ending_on_assistant_text_gets_a_continue_turn():
    history = [_user("Write a long essay."), _assistant("The first part of the essay")]

    assert continuation_prompt([], history, "global.anthropic.claude-sonnet-5") == CONTINUATION_PROMPT


def test_history_is_read_not_changed():
    history = [_user("Write a long essay."), _assistant("The first part of the essay")]
    before = copy.deepcopy(history)

    continuation_prompt([], history, "global.anthropic.claude-sonnet-5")

    assert history == before


def test_prefill_capable_model_keeps_the_empty_prompt():
    history = [_user("Write a long essay."), _assistant("The first part")]
    prompt = []

    assert continuation_prompt(prompt, history, "us.anthropic.claude-haiku-4-5-20251001-v1:0") is prompt


def test_history_ending_on_user_keeps_the_empty_prompt():
    # An interrupted tool turn whose results were kept already ends on a user
    # turn; a second user turn there would break alternation.
    history = [
        _user("Look it up."),
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": "search", "input": {}}}]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "content": [{"text": "ok"}]}}]},
    ]
    prompt = []

    assert continuation_prompt(prompt, history, "global.anthropic.claude-sonnet-5") is prompt


def test_trailing_tool_use_keeps_the_empty_prompt():
    # Strands answers a dangling toolUse with error results before the model
    # call, so the request already ends on a user turn.
    history = [
        _user("Look it up."),
        {"role": "assistant", "content": [{"text": "Searching"}, {"toolUse": {"toolUseId": "t1", "name": "s", "input": {}}}]},
    ]
    prompt = []

    assert continuation_prompt(prompt, history, "global.anthropic.claude-sonnet-5") is prompt


@pytest.mark.parametrize(
    "prompt",
    [
        "a fresh message",
        [{"text": "content blocks"}],
        [{"interruptResponse": {"interruptId": "i1", "response": {"ok": True}}}],
    ],
)
def test_non_continuation_prompts_are_untouched(prompt):
    history = [_user("hi"), _assistant("hello")]

    assert continuation_prompt(prompt, history, "global.anthropic.claude-sonnet-5") is prompt


def test_empty_history_keeps_the_empty_prompt():
    prompt = []

    assert continuation_prompt(prompt, [], "global.anthropic.claude-sonnet-5") is prompt


def test_continue_text_is_constant():
    # Two continuations of the same partial send identical bytes.
    history = [_user("Write."), _assistant("Part one")]

    first = continuation_prompt([], history, "global.anthropic.claude-sonnet-5")
    second = continuation_prompt([], history, "global.anthropic.claude-sonnet-5")

    assert first == second == CONTINUATION_PROMPT
