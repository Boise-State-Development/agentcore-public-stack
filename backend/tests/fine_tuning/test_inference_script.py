"""Unit tests for SageMaker inference script handler functions."""

import json
import pytest
from unittest.mock import MagicMock, patch

import numpy as np

from apis.app_api.fine_tuning.sagemaker_scripts.inference import (
    input_fn,
    output_fn,
    predict_fn,
    _sanitize_label,
)


class TestInputFn:

    def test_text_plain_parses_lines(self):
        body = "Hello world\nFoo bar\nBaz qux\n"
        result = input_fn(body, "text/plain")
        assert result == ["Hello world", "Foo bar", "Baz qux"]

    def test_text_plain_skips_empty_lines(self):
        body = "Hello\n\n  \nWorld\n"
        result = input_fn(body, "text/plain")
        assert result == ["Hello", "World"]

    def test_json_list_input(self):
        body = json.dumps(["Hello", "World"])
        result = input_fn(body, "application/json")
        assert result == ["Hello", "World"]

    def test_json_dict_with_texts_key(self):
        body = json.dumps({"texts": ["Hello", "World"]})
        result = input_fn(body, "application/json")
        assert result == ["Hello", "World"]

    def test_json_dict_without_texts_key_raises(self):
        body = json.dumps({"data": ["Hello"]})
        with pytest.raises(ValueError, match="must be a list"):
            input_fn(body, "application/json")

    def test_unsupported_content_type_raises(self):
        with pytest.raises(ValueError, match="Unsupported content type"):
            input_fn("data", "application/xml")

    def test_json_list_skips_empty_strings(self):
        body = json.dumps(["Hello", "", "  ", "World"])
        result = input_fn(body, "application/json")
        assert result == ["Hello", "World"]


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

    def test_csv_header_includes_label_columns(self):
        prediction = {
            "texts": ["hello"],
            "probabilities": np.array([[0.8, 0.2]]),
            "labels": ["positive", "negative"],
        }
        result = output_fn(prediction)
        lines = result.split("\n")
        assert lines[0] == "text,prob_positive,prob_negative"

    def test_csv_row_has_quoted_text(self):
        prediction = {
            "texts": ["hello world"],
            "probabilities": np.array([[0.85, 0.15]]),
            "labels": ["pos", "neg"],
        }
        result = output_fn(prediction)
        lines = result.split("\n")
        assert lines[1].startswith('"hello world"')

    def test_escapes_quotes_in_text(self):
        prediction = {
            "texts": ['She said "hello"'],
            "probabilities": np.array([[0.9, 0.1]]),
            "labels": ["pos", "neg"],
        }
        result = output_fn(prediction)
        lines = result.split("\n")
        # Quotes in text should be doubled
        assert '""hello""' in lines[1]

    def test_escapes_commas_in_text(self):
        prediction = {
            "texts": ["hello, world"],
            "probabilities": np.array([[0.7, 0.3]]),
            "labels": ["pos", "neg"],
        }
        result = output_fn(prediction)
        lines = result.split("\n")
        # Text with commas should be quoted
        assert lines[1].startswith('"hello, world"')

    def test_multiple_rows(self):
        prediction = {
            "texts": ["a", "b", "c"],
            "probabilities": np.array([[0.9, 0.1], [0.3, 0.7], [0.5, 0.5]]),
            "labels": ["pos", "neg"],
        }
        result = output_fn(prediction)
        lines = result.split("\n")
        assert len(lines) == 4  # header + 3 rows

    def test_probability_values_six_decimals(self):
        prediction = {
            "texts": ["test"],
            "probabilities": np.array([[0.123456789, 0.876543211]]),
            "labels": ["a", "b"],
        }
        result = output_fn(prediction)
        lines = result.split("\n")
        # Should have 6 decimal places
        assert "0.123457" in lines[1]  # rounded


_has_torch = True
try:
    import torch
except ImportError:
    _has_torch = False


@pytest.mark.skipif(not _has_torch, reason="torch not installed (SageMaker DLC only)")
class TestPredictFn:

    def test_empty_input_returns_empty(self):
        model_tuple = (MagicMock(), MagicMock(), "cpu")
        result = predict_fn([], model_tuple)
        assert result["texts"] == []
        assert result["probabilities"].shape == (0, 0)
        assert result["labels"] == []

    def test_returns_correct_structure(self):
        # Create mock model that returns logits
        mock_model = MagicMock()
        mock_model.config.id2label = {0: "positive", 1: "negative"}

        mock_outputs = MagicMock()
        mock_outputs.logits = torch.tensor([[2.0, -1.0], [0.5, 1.5]])

        mock_model.return_value = mock_outputs

        # Mock tokenizer
        mock_tokenizer = MagicMock()
        mock_tokenizer.return_value = {
            "input_ids": torch.tensor([[1, 2], [3, 4]]),
            "attention_mask": torch.tensor([[1, 1], [1, 1]]),
        }

        device = torch.device("cpu")
        result = predict_fn(["hello", "world"], (mock_model, mock_tokenizer, device))

        assert result["texts"] == ["hello", "world"]
        assert result["probabilities"].shape == (2, 2)
        assert result["labels"] == ["positive", "negative"]

        # Probabilities should sum to 1 for each row (softmax)
        for row in result["probabilities"]:
            assert abs(sum(row) - 1.0) < 1e-5

    def test_uses_class_prefix_when_no_id2label(self):
        mock_model = MagicMock()
        mock_model.config.id2label = None

        mock_outputs = MagicMock()
        mock_outputs.logits = torch.tensor([[1.0, 2.0, 3.0]])
        mock_model.return_value = mock_outputs

        mock_tokenizer = MagicMock()
        mock_tokenizer.return_value = {
            "input_ids": torch.tensor([[1, 2]]),
            "attention_mask": torch.tensor([[1, 1]]),
        }

        device = torch.device("cpu")
        result = predict_fn(["test"], (mock_model, mock_tokenizer, device))

        assert result["labels"] == ["class_0", "class_1", "class_2"]
