"""Speech-to-speech rows are priced, never picked."""

from types import SimpleNamespace

from apis.shared.models.modalities import is_speech_model


def test_speech_output_is_a_speech_model_regardless_of_case():
    assert is_speech_model(SimpleNamespace(output_modalities=["SPEECH", "TEXT"]))
    assert is_speech_model(SimpleNamespace(output_modalities=["speech"]))


def test_text_and_image_rows_are_not():
    assert not is_speech_model(SimpleNamespace(output_modalities=["TEXT"]))
    assert not is_speech_model(SimpleNamespace(output_modalities=["TEXT", "IMAGE"]))


def test_missing_or_empty_modalities_are_not():
    assert not is_speech_model(SimpleNamespace(output_modalities=None))
    assert not is_speech_model(SimpleNamespace(output_modalities=[]))
    assert not is_speech_model(SimpleNamespace())


def test_speech_input_alone_does_not_make_a_speech_model():
    # Voxtral-style speech-to-text answers Converse; it belongs in the picker.
    assert not is_speech_model(SimpleNamespace(input_modalities=["SPEECH"], output_modalities=["TEXT"]))
