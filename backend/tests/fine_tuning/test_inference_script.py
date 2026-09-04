"""Unit tests for the Batch Transform handler.

``inference.py`` is a dispatcher: it resolves the task recorded in the model
artifact and delegates to the matching ``task_*`` module. These cover the
dispatch itself, the shared CSV output contract, and the per-task payload
parsing — the parts that must hold for every modality.
"""

import json
import zipfile

import numpy as np
import pytest
from unittest.mock import MagicMock

from apis.app_api.fine_tuning import task_types
from apis.app_api.fine_tuning.sagemaker_scripts import task_text_classification
from apis.app_api.fine_tuning.sagemaker_scripts.inference import (
    _sanitize_label,
    input_fn,
    output_fn,
    read_task_type,
    resolve_task_module,
)

TEXT_SPEC = task_types.get_task_spec(task_types.TEXT_CLASSIFICATION)


def _texts(records):
    """Pull the text out of the record dicts input_fn now returns."""
    return [record["text"] for record in records]


class TestTaskDispatch:
    """The handler must serve whichever task the artifact was trained for."""

    def test_reads_the_recorded_task_type(self, tmp_path):
        (tmp_path / "task_type.json").write_text(
            json.dumps({"task_type": task_types.IMAGE_CLASSIFICATION})
        )

        assert read_task_type(str(tmp_path)) == task_types.IMAGE_CLASSIFICATION

    def test_artifact_without_a_marker_is_text_classification(self, tmp_path):
        """Artifacts trained before task types existed carry no marker.

        They are all text classifiers, and must keep loading.
        """
        assert read_task_type(str(tmp_path)) == task_types.TEXT_CLASSIFICATION

    @pytest.mark.parametrize("task_type", task_types.TASK_TYPES)
    def test_every_registered_task_has_an_inference_module(self, task_type):
        module, spec = resolve_task_module(task_type)

        assert spec.task_type == task_type
        for handler in ("model_fn", "input_fn", "predict_fn"):
            assert hasattr(module, handler), f"{module.__name__} lacks {handler}"

    def test_unknown_task_type_raises(self):
        with pytest.raises(ValueError, match="Unknown task type"):
            resolve_task_module("image-to-interpretive-dance")


class TestInputFn:
    """With no model loaded the handler assumes text, preserving old behaviour."""

    def test_text_plain_parses_lines(self):
        result = input_fn("Hello world\nFoo bar\nBaz qux\n", "text/plain")
        assert _texts(result) == ["Hello world", "Foo bar", "Baz qux"]

    def test_text_plain_skips_empty_lines(self):
        assert _texts(input_fn("Hello\n\n  \nWorld\n", "text/plain")) == ["Hello", "World"]

    def test_json_list_input(self):
        body = json.dumps(["Hello", "World"])
        assert _texts(input_fn(body, "application/json")) == ["Hello", "World"]

    def test_json_dict_with_texts_key(self):
        body = json.dumps({"texts": ["Hello", "World"]})
        assert _texts(input_fn(body, "application/json")) == ["Hello", "World"]

    def test_json_dict_without_texts_key_raises(self):
        with pytest.raises(ValueError, match="must be a list"):
            input_fn(json.dumps({"data": ["Hello"]}), "application/json")

    def test_unsupported_content_type_raises(self):
        with pytest.raises(ValueError, match="Unsupported content type"):
            input_fn("data", "application/xml")

    def test_json_list_skips_empty_strings(self):
        body = json.dumps(["Hello", "", "  ", "World"])
        assert _texts(input_fn(body, "application/json")) == ["Hello", "World"]

    def test_text_plain_bytes_input(self):
        """SageMaker HuggingFace DLC passes request_body as bytes."""
        result = input_fn(b"Hello world\nFoo bar\n", "text/plain")
        assert _texts(result) == ["Hello world", "Foo bar"]

    def test_json_bytes_input(self):
        body = json.dumps(["Hello", "World"]).encode("utf-8")
        assert _texts(input_fn(body, "application/json")) == ["Hello", "World"]

    def test_bytes_with_utf8_characters(self):
        body = "café latte\nnaïve résumé\n".encode("utf-8")
        assert _texts(input_fn(body, "text/plain")) == ["café latte", "naïve résumé"]

    def test_bytearray_input(self):
        assert _texts(input_fn(bytearray(b"Hello\nWorld\n"), "text/plain")) == [
            "Hello",
            "World",
        ]

    def test_text_records_identify_themselves_by_their_text(self):
        records = input_fn("alpha\nbeta\n", "text/plain")
        assert [r["identifier"] for r in records] == ["alpha", "beta"]

    def test_dispatches_to_the_loaded_task(self, tmp_path):
        """A loaded image model must not parse its payload as newline text."""
        from apis.app_api.fine_tuning.sagemaker_scripts import task_image_classification

        archive = tmp_path / "images.zip"
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
            "1f15c4890000000a49444154789c6360000002000100ffff0300000600"
            "05570c9d0000000049454e44ae426082"
        )
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("a.png", png)
            handle.writestr("b.png", png)

        records = input_fn(
            archive.read_bytes(),
            "application/zip",
            {"task_type": task_types.IMAGE_CLASSIFICATION},
        )

        assert sorted(r["identifier"] for r in records) == ["a.png", "b.png"]
        assert all(task_image_classification is not None for _ in records)

    def test_image_task_rejects_a_non_archive_payload(self):
        with pytest.raises(ValueError, match="Expected archive bytes"):
            input_fn(
                "not an archive",
                "application/zip",
                {"task_type": task_types.IMAGE_CLASSIFICATION},
            )


class TestSanitizeLabel:

    def test_alphanumeric_unchanged(self):
        assert _sanitize_label("positive") == "positive"

    def test_spaces_become_underscores(self):
        assert _sanitize_label("very positive") == "very_positive"

    def test_special_chars_become_underscores(self):
        assert _sanitize_label("class-1/2") == "class_1_2"

    def test_none_returns_class(self):
        assert _sanitize_label(None) == "class"

    def test_underscores_preserved(self):
        assert _sanitize_label("some_label") == "some_label"


class TestOutputFn:
    """One CSV shape for every task, so a new modality never breaks the viewer."""

    def test_csv_header_includes_label_columns(self):
        result = output_fn(
            {
                "identifiers": ["hello"],
                "probabilities": np.array([[0.8, 0.2]]),
                "labels": ["positive", "negative"],
                "identifier_column": "text",
            }
        )
        assert result.split("\n")[0] == "text,prob_positive,prob_negative"

    def test_image_task_names_the_first_column_image(self):
        result = output_fn(
            {
                "identifiers": ["cats/a.png"],
                "probabilities": np.array([[0.8, 0.2]]),
                "labels": ["cat", "dog"],
                "identifier_column": "image",
            }
        )
        lines = result.split("\n")
        assert lines[0] == "image,prob_cat,prob_dog"
        assert lines[1].startswith('"cats/a.png"')

    def test_csv_row_has_quoted_text(self):
        result = output_fn(
            {
                "identifiers": ["hello world"],
                "probabilities": np.array([[0.85, 0.15]]),
                "labels": ["pos", "neg"],
            }
        )
        assert result.split("\n")[1].startswith('"hello world"')

    def test_escapes_quotes_in_text(self):
        result = output_fn(
            {
                "identifiers": ['She said "hello"'],
                "probabilities": np.array([[0.9, 0.1]]),
                "labels": ["pos", "neg"],
            }
        )
        assert '""hello""' in result.split("\n")[1]

    def test_escapes_commas_in_text(self):
        result = output_fn(
            {
                "identifiers": ["hello, world"],
                "probabilities": np.array([[0.7, 0.3]]),
                "labels": ["pos", "neg"],
            }
        )
        assert result.split("\n")[1].startswith('"hello, world"')

    def test_multiple_rows(self):
        result = output_fn(
            {
                "identifiers": ["a", "b", "c"],
                "probabilities": np.array([[0.9, 0.1], [0.3, 0.7], [0.5, 0.5]]),
                "labels": ["pos", "neg"],
            }
        )
        assert len(result.split("\n")) == 4  # header + 3 rows

    def test_probability_values_six_decimals(self):
        result = output_fn(
            {
                "identifiers": ["test"],
                "probabilities": np.array([[0.123456789, 0.876543211]]),
                "labels": ["a", "b"],
            }
        )
        assert "0.123457" in result.split("\n")[1]  # rounded


_has_torch = True
try:
    import torch
except ImportError:
    _has_torch = False


@pytest.mark.skipif(not _has_torch, reason="torch not installed (SageMaker DLC only)")
class TestTextPredictFn:

    def _loaded(self, model, tokenizer):
        return {"model": model, "tokenizer": tokenizer, "device": torch.device("cpu")}

    def test_empty_input_returns_empty(self):
        result = task_text_classification.predict_fn(
            [], self._loaded(MagicMock(), MagicMock()), TEXT_SPEC
        )
        assert result["identifiers"] == []
        assert result["probabilities"].shape == (0, 0)
        assert result["labels"] == []

    def test_returns_correct_structure(self):
        mock_model = MagicMock()
        mock_model.config.id2label = {0: "positive", 1: "negative"}
        outputs = MagicMock()
        outputs.logits = torch.tensor([[2.0, -1.0], [0.5, 1.5]])
        mock_model.return_value = outputs

        mock_tokenizer = MagicMock()
        mock_tokenizer.return_value = {
            "input_ids": torch.tensor([[1, 2], [3, 4]]),
            "attention_mask": torch.tensor([[1, 1], [1, 1]]),
        }

        records = [
            {"text": "hello", "identifier": "hello"},
            {"text": "world", "identifier": "world"},
        ]
        result = task_text_classification.predict_fn(
            records, self._loaded(mock_model, mock_tokenizer), TEXT_SPEC
        )

        assert result["identifiers"] == ["hello", "world"]
        assert result["probabilities"].shape == (2, 2)
        assert result["labels"] == ["positive", "negative"]
        for row in result["probabilities"]:
            assert abs(sum(row) - 1.0) < 1e-5

    def test_uses_class_prefix_when_no_id2label(self):
        mock_model = MagicMock()
        mock_model.config.id2label = None
        outputs = MagicMock()
        outputs.logits = torch.tensor([[1.0, 2.0, 3.0]])
        mock_model.return_value = outputs

        mock_tokenizer = MagicMock()
        mock_tokenizer.return_value = {
            "input_ids": torch.tensor([[1, 2]]),
            "attention_mask": torch.tensor([[1, 1]]),
        }

        result = task_text_classification.predict_fn(
            [{"text": "test", "identifier": "test"}],
            self._loaded(mock_model, mock_tokenizer),
            TEXT_SPEC,
        )

        assert result["labels"] == ["class_0", "class_1", "class_2"]
