"""Which catalog models a prompt step can run on: one rule for the sheet and the Runtime."""

from types import SimpleNamespace

import pytest

from apis.shared.handoff.prompt_models import prompt_model_supported, prompt_model_unsupported_reason


def _row(provider="bedrock", inputs=("TEXT", "IMAGE"), outputs=("TEXT",)):
    return SimpleNamespace(provider=provider, input_modalities=list(inputs), output_modalities=list(outputs))


@pytest.mark.parametrize(
    "row, reason",
    [
        (_row(), None),
        (_row(inputs=("TEXT", "IMAGE", "DOCUMENT")), None),
        (None, "uncataloged"),
        (_row(provider="bedrock-responses"), "not_converse"),
        (_row(provider="mantle"), "not_converse"),
        (_row(inputs=("TEXT",)), "text_only"),
        (_row(inputs=("SPEECH",), outputs=("SPEECH",)), "no_text_input"),
        (_row(inputs=("TEXT", "SPEECH"), outputs=("TEXT", "SPEECH")), "no_text_output"),
        (_row(outputs=("EMBEDDING",)), "no_text_output"),
    ],
    ids=["claude", "documents", "no-row", "responses-api", "mantle", "text-only", "speech-only", "voice", "embedding"],
)
def test_the_rule(row, reason):
    assert prompt_model_unsupported_reason(row) == reason
    assert prompt_model_supported(row) is (reason is None)
