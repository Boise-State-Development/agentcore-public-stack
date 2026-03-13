"""Unit tests for SageMaker training script core functions."""

import os
import pytest
from unittest.mock import MagicMock, patch, call

from apis.app_api.fine_tuning.sagemaker_scripts.train import (
    resolve_max_context_length,
    find_csv_in_channel,
    copy_inference_script,
    DynamoDBProgressCallback,
    SageMakerLoggingCallback,
)


class TestResolveMaxContextLength:

    def test_returns_min_of_valid_values(self):
        config = MagicMock()
        config.max_position_embeddings = 512
        config.n_positions = 1024
        config.seq_length = None

        tokenizer = MagicMock()
        tokenizer.model_max_length = 2048

        result = resolve_max_context_length(config, tokenizer)
        assert result == 512

    def test_returns_none_when_all_invalid(self):
        config = MagicMock(spec=[])
        tokenizer = MagicMock(spec=[])

        result = resolve_max_context_length(config, tokenizer)
        assert result is None

    def test_ignores_very_large_values(self):
        config = MagicMock()
        config.max_position_embeddings = 2_000_000
        config.n_positions = None
        config.seq_length = None

        tokenizer = MagicMock()
        tokenizer.model_max_length = 512

        result = resolve_max_context_length(config, tokenizer)
        assert result == 512

    def test_uses_model_max_length_as_fallback(self):
        config = MagicMock()
        config.max_position_embeddings = None
        config.n_positions = None
        config.seq_length = None

        tokenizer = MagicMock()
        tokenizer.model_max_length = 768

        result = resolve_max_context_length(config, tokenizer)
        assert result == 768


class TestFindCsvInChannel:

    def test_finds_csv_file(self, tmp_path):
        csv_file = tmp_path / "dataset.csv"
        csv_file.write_text("text,label\nhello,1\n")

        result = find_csv_in_channel(str(tmp_path))
        assert result == str(csv_file)

    def test_raises_when_no_csv(self, tmp_path):
        txt_file = tmp_path / "readme.txt"
        txt_file.write_text("not a csv")

        with pytest.raises(FileNotFoundError, match="No CSV file found"):
            find_csv_in_channel(str(tmp_path))

    def test_case_insensitive_extension(self, tmp_path):
        csv_file = tmp_path / "DATA.CSV"
        csv_file.write_text("text,label\nhello,1\n")

        result = find_csv_in_channel(str(tmp_path))
        assert result == str(csv_file)

    def test_raises_when_dir_missing(self):
        with pytest.raises(FileNotFoundError, match="does not exist"):
            find_csv_in_channel("/nonexistent/path")


class TestCopyInferenceScript:

    def test_copies_files_to_code_dir(self, tmp_path):
        # Create fake script files in a "source" dir
        source_dir = tmp_path / "scripts"
        source_dir.mkdir()
        (source_dir / "inference.py").write_text("# inference handler")
        (source_dir / "requirements.txt").write_text("pandas\n")

        # Create output dir
        model_dir = tmp_path / "model"
        model_dir.mkdir()

        # Patch __file__ so copy_inference_script looks in our source_dir
        with patch(
            "apis.app_api.fine_tuning.sagemaker_scripts.train.os.path.dirname",
            return_value=str(source_dir),
        ):
            with patch(
                "apis.app_api.fine_tuning.sagemaker_scripts.train.os.path.abspath",
                return_value=str(source_dir / "train.py"),
            ):
                copy_inference_script(str(model_dir))

        code_dir = model_dir / "code"
        assert code_dir.exists()
        assert (code_dir / "inference.py").exists()
        assert (code_dir / "requirements.txt").exists()
        assert (code_dir / "inference.py").read_text() == "# inference handler"

    def test_creates_code_directory(self, tmp_path):
        source_dir = tmp_path / "scripts"
        source_dir.mkdir()
        (source_dir / "inference.py").write_text("# handler")

        model_dir = tmp_path / "model"
        model_dir.mkdir()

        with patch(
            "apis.app_api.fine_tuning.sagemaker_scripts.train.os.path.dirname",
            return_value=str(source_dir),
        ):
            with patch(
                "apis.app_api.fine_tuning.sagemaker_scripts.train.os.path.abspath",
                return_value=str(source_dir / "train.py"),
            ):
                copy_inference_script(str(model_dir))

        assert (model_dir / "code").is_dir()


class TestDynamoDBProgressCallback:

    def test_on_train_begin_sets_zero(self):
        mock_client = MagicMock()
        cb = DynamoDBProgressCallback("table", "us-west-2", "PK", "SK")
        cb._client = mock_client

        state = MagicMock()
        cb.on_train_begin(MagicMock(), state, MagicMock())

        mock_client.update_item.assert_called_once()
        call_kwargs = mock_client.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":p"]["N"] == "0.0"

    def test_on_train_end_sets_one(self):
        mock_client = MagicMock()
        cb = DynamoDBProgressCallback("table", "us-west-2", "PK", "SK")
        cb._client = mock_client

        state = MagicMock()
        cb.on_train_end(MagicMock(), state, MagicMock())

        call_kwargs = mock_client.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":p"]["N"] == "1.0"

    def test_noop_when_no_table_configured(self):
        """When table_name is empty, no DynamoDB client is created."""
        cb = DynamoDBProgressCallback("", "us-west-2", "", "")
        assert cb._client is None

        # Calling _update_progress should not raise
        cb._update_progress(0.5)

    def test_throttles_step_updates(self):
        mock_client = MagicMock()
        cb = DynamoDBProgressCallback("table", "us-west-2", "PK", "SK")
        cb._client = mock_client

        state = MagicMock()
        state.max_steps = 100

        # Step 5 should NOT trigger (5 % 10 != 0)
        state.global_step = 5
        cb.on_step_end(MagicMock(), state, MagicMock())
        mock_client.update_item.assert_not_called()

        # Step 10 SHOULD trigger (10 % 10 == 0)
        state.global_step = 10
        cb.on_step_end(MagicMock(), state, MagicMock())
        mock_client.update_item.assert_called_once()


class TestSageMakerLoggingCallback:

    def test_logs_accuracy_on_evaluate(self, caplog):
        import logging
        cb = SageMakerLoggingCallback()

        args = MagicMock()
        args.num_train_epochs = 5

        state = MagicMock()
        state.epoch = 2

        with caplog.at_level(logging.INFO):
            cb.on_evaluate(args, state, MagicMock(), metrics={"eval_accuracy": 0.9123})

        assert any("eval_accuracy=0.9123" in msg for msg in caplog.messages)

    def test_skips_final_epoch(self, caplog):
        import logging
        cb = SageMakerLoggingCallback()

        args = MagicMock()
        args.num_train_epochs = 3

        state = MagicMock()
        state.epoch = 3  # Final epoch

        with caplog.at_level(logging.INFO):
            cb.on_evaluate(args, state, MagicMock(), metrics={"eval_accuracy": 0.95})

        # Should NOT log accuracy for final epoch (avoids redundancy)
        assert not any("eval_accuracy" in msg for msg in caplog.messages)
