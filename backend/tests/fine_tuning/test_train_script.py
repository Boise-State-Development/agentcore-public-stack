"""Unit tests for the shared training helpers in ``task_common``.

These deliberately avoid torch/transformers: the dataset, archive and label
contracts have to be assertable in the backend venv, where the ML stack is
absent. That constraint is why the helpers live in a module that imports its
heavy dependencies lazily.
"""

import io
import json
import zipfile

import pytest
from unittest.mock import MagicMock, patch

from apis.app_api.fine_tuning import task_types
from apis.app_api.fine_tuning.sagemaker_scripts.task_common import (
    DATASET_READERS,
    DynamoDBProgressCallback,
    SageMakerLoggingCallback,
    build_label_mapping,
    copy_inference_bundle,
    extract_archive,
    find_file_in_dir,
    label_names,
    load_manifest_frame,
    prepare_dataset,
    resolve_dataset_reader,
    resolve_image_path,
    resolve_max_context_length,
    validate_dataset_columns,
)

TEXT_SPEC = task_types.get_task_spec(task_types.TEXT_CLASSIFICATION)
IMAGE_SPEC = task_types.get_task_spec(task_types.IMAGE_CLASSIFICATION)
IMAGE_TEXT_SPEC = task_types.get_task_spec(task_types.IMAGE_TEXT_CLASSIFICATION)

MANIFEST_EXTENSIONS = TEXT_SPEC.manifest_extensions


def _write_zip(path, entries):
    """Write a zip from {archive name: bytes-or-str} and return its path."""
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            if isinstance(content, str):
                content = content.encode("utf-8")
            archive.writestr(name, content)
    return str(path)


# 1x1 PNG — enough for path resolution tests, which never decode the file.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "05570c9d0000000049454e44ae426082"
)


class TestResolveMaxContextLength:

    def test_returns_min_of_valid_values(self):
        config = MagicMock()
        config.max_position_embeddings = 512
        config.n_positions = 1024
        config.seq_length = None
        config.text_config = None

        tokenizer = MagicMock()
        tokenizer.model_max_length = 2048

        assert resolve_max_context_length(config, tokenizer) == 512

    def test_returns_none_when_all_invalid(self):
        config = MagicMock(spec=[])
        tokenizer = MagicMock(spec=[])

        assert resolve_max_context_length(config, tokenizer) is None

    def test_ignores_very_large_values(self):
        config = MagicMock()
        config.max_position_embeddings = 2_000_000
        config.n_positions = None
        config.seq_length = None
        config.text_config = None

        tokenizer = MagicMock()
        tokenizer.model_max_length = 512

        assert resolve_max_context_length(config, tokenizer) == 512

    def test_uses_model_max_length_as_fallback(self):
        config = MagicMock()
        config.max_position_embeddings = None
        config.n_positions = None
        config.seq_length = None
        config.text_config = None

        tokenizer = MagicMock()
        tokenizer.model_max_length = 768

        assert resolve_max_context_length(config, tokenizer) == 768

    def test_reads_nested_text_config(self):
        """Multimodal configs keep the text limits on config.text_config.

        Reading only the top level yields None on every vision-language model,
        which silently drops the context cap.
        """
        text_config = MagicMock()
        text_config.max_position_embeddings = 77
        text_config.n_positions = None
        text_config.seq_length = None

        config = MagicMock()
        config.max_position_embeddings = None
        config.n_positions = None
        config.seq_length = None
        config.text_config = text_config

        tokenizer = MagicMock(spec=[])

        assert resolve_max_context_length(config, tokenizer) == 77


class TestFindFileInDir:

    def test_finds_csv_file(self, tmp_path):
        csv_file = tmp_path / "dataset.csv"
        csv_file.write_text("text,label\nhello,1\n")

        assert find_file_in_dir(str(tmp_path), MANIFEST_EXTENSIONS) == str(csv_file)

    @pytest.mark.parametrize("filename", ["dataset.jsonl", "dataset.json"])
    def test_finds_json_formats(self, tmp_path, filename):
        """The UI offers JSONL/JSON, so the trainer has to find them too."""
        dataset = tmp_path / filename
        dataset.write_text('{"text": "hello", "label": "a"}\n')

        assert find_file_in_dir(str(tmp_path), MANIFEST_EXTENSIONS) == str(dataset)

    def test_raises_when_no_supported_dataset(self, tmp_path):
        (tmp_path / "readme.txt").write_text("not a dataset")

        with pytest.raises(FileNotFoundError, match="No dataset file found"):
            find_file_in_dir(str(tmp_path), MANIFEST_EXTENSIONS)

    def test_case_insensitive_extension(self, tmp_path):
        csv_file = tmp_path / "DATA.CSV"
        csv_file.write_text("text,label\nhello,1\n")

        assert find_file_in_dir(str(tmp_path), MANIFEST_EXTENSIONS) == str(csv_file)

    def test_raises_when_dir_missing(self):
        with pytest.raises(FileNotFoundError, match="does not exist"):
            find_file_in_dir("/nonexistent/path", MANIFEST_EXTENSIONS)

    def test_prefers_shallowest_match(self, tmp_path):
        """A manifest at the archive root wins over one inside an image folder."""
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "notes.csv").write_text("a,b\n")
        root_manifest = tmp_path / "manifest.csv"
        root_manifest.write_text("image,label\na.png,cat\n")

        assert find_file_in_dir(str(tmp_path), MANIFEST_EXTENSIONS) == str(root_manifest)

    def test_skips_macos_resource_forks(self, tmp_path):
        """Zips made on macOS carry ._ shadow files that are not manifests."""
        (tmp_path / "._manifest.csv").write_text("junk")
        real = tmp_path / "manifest.csv"
        real.write_text("image,label\na.png,cat\n")

        assert find_file_in_dir(str(tmp_path), MANIFEST_EXTENSIONS) == str(real)


class TestResolveDatasetReader:
    """Every format the upload UI accepts must actually be readable.

    A JSONL dataset previously uploaded and dispatched fine, then died on the
    GPU several billed minutes in because the trainer only read CSV. These
    assert the dispatch table directly so they run without pandas, which
    exists only inside the SageMaker training container.
    """

    def test_supports_the_formats_the_ui_offers(self):
        assert set(DATASET_READERS) == {".csv", ".jsonl", ".json"}

    def test_manifest_extensions_match_the_readers(self):
        """The registry and the reader table must not drift apart."""
        for task_type in task_types.TASK_TYPES:
            spec = task_types.get_task_spec(task_type)
            assert set(spec.manifest_extensions) == set(DATASET_READERS)

    def test_csv_uses_read_csv(self):
        assert resolve_dataset_reader("/data/dataset.csv") == ("read_csv", {})

    def test_jsonl_reads_line_delimited(self):
        assert resolve_dataset_reader("/data/dataset.jsonl") == (
            "read_json",
            {"lines": True},
        )

    def test_json_reads_whole_document(self):
        assert resolve_dataset_reader("/data/dataset.json") == ("read_json", {})

    def test_extension_match_is_case_insensitive(self):
        assert resolve_dataset_reader("/data/DATA.CSV") == ("read_csv", {})

    def test_raises_on_unsupported_extension(self):
        with pytest.raises(ValueError, match="Unsupported dataset format"):
            resolve_dataset_reader("/data/dataset.parquet")


class TestValidateDatasetColumns:

    def test_accepts_required_columns(self):
        validate_dataset_columns(["text", "label"], "/data/dataset.csv", TEXT_SPEC)

    def test_raises_when_label_missing(self):
        with pytest.raises(ValueError, match="missing required column"):
            validate_dataset_columns(["text"], "/data/dataset.csv", TEXT_SPEC)

    def test_raises_when_text_missing(self):
        with pytest.raises(ValueError, match="missing required column"):
            validate_dataset_columns(["label"], "/data/dataset.csv", TEXT_SPEC)

    def test_image_task_requires_image_column(self):
        with pytest.raises(ValueError, match="missing required column"):
            validate_dataset_columns(["text", "label"], "/m.csv", IMAGE_SPEC)

    def test_image_text_task_requires_all_three(self):
        validate_dataset_columns(
            ["image", "text", "label"], "/m.csv", IMAGE_TEXT_SPEC
        )
        with pytest.raises(ValueError, match="missing required column"):
            validate_dataset_columns(["image", "label"], "/m.csv", IMAGE_TEXT_SPEC)


class TestExtractArchive:
    """A dataset archive is untrusted user input."""

    def test_extracts_flat_entries(self, tmp_path):
        archive = _write_zip(tmp_path / "d.zip", {"manifest.csv": "image,label\n"})
        dest = tmp_path / "out"

        extract_archive(archive, str(dest))

        assert (dest / "manifest.csv").read_text() == "image,label\n"

    def test_extracts_nested_entries(self, tmp_path):
        archive = _write_zip(
            tmp_path / "d.zip",
            {"manifest.csv": "image,label\n", "images/a.png": PNG_BYTES},
        )
        dest = tmp_path / "out"

        extract_archive(archive, str(dest))

        assert (dest / "images" / "a.png").read_bytes() == PNG_BYTES

    def test_rejects_parent_traversal(self, tmp_path):
        """Zip-Slip: a crafted entry must not write outside the destination."""
        archive = _write_zip(tmp_path / "evil.zip", {"../escaped.txt": "pwned"})

        with pytest.raises(ValueError, match="unsafe archive entry"):
            extract_archive(archive, str(tmp_path / "out"))

        assert not (tmp_path / "escaped.txt").exists()

    def test_rejects_absolute_paths(self, tmp_path):
        archive = _write_zip(tmp_path / "evil.zip", {"/etc/passwd": "pwned"})

        with pytest.raises(ValueError, match="unsafe archive entry"):
            extract_archive(archive, str(tmp_path / "out"))


class TestResolveImagePath:

    def test_resolves_relative_reference(self, tmp_path):
        (tmp_path / "images").mkdir()
        image = tmp_path / "images" / "a.png"
        image.write_bytes(PNG_BYTES)

        assert resolve_image_path(str(tmp_path), "images/a.png") == str(image)

    def test_strips_surrounding_whitespace(self, tmp_path):
        image = tmp_path / "a.png"
        image.write_bytes(PNG_BYTES)

        assert resolve_image_path(str(tmp_path), "  a.png  ") == str(image)

    def test_raises_when_missing(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="missing from the archive"):
            resolve_image_path(str(tmp_path), "nope.png")

    def test_rejects_escape_from_archive(self, tmp_path):
        outside = tmp_path.parent / "outside.png"
        outside.write_bytes(PNG_BYTES)
        root = tmp_path / "root"
        root.mkdir()

        with pytest.raises(ValueError, match="escapes the dataset archive"):
            resolve_image_path(str(root), "../outside.png")


class TestLoadManifestFrame:
    """End-to-end load, where pandas is available (the training container)."""

    @pytest.mark.parametrize(
        "filename,content",
        [
            ("dataset.csv", "text,label\nhello,positive\nbye,negative\n"),
            (
                "dataset.jsonl",
                '{"text": "hello", "label": "positive"}\n'
                '{"text": "bye", "label": "negative"}\n',
            ),
            (
                "dataset.json",
                '[{"text": "hello", "label": "positive"},'
                ' {"text": "bye", "label": "negative"}]',
            ),
        ],
    )
    def test_loads_each_supported_format(self, tmp_path, filename, content):
        pytest.importorskip("pandas")
        path = tmp_path / filename
        path.write_text(content)

        frame = load_manifest_frame(str(path), TEXT_SPEC)

        assert frame["text"].tolist() == ["hello", "bye"]
        assert frame["label"].tolist() == ["positive", "negative"]


class TestPrepareDataset:

    def test_text_task_reads_the_channel_directly(self, tmp_path):
        pytest.importorskip("pandas")
        (tmp_path / "d.csv").write_text("text,label\nhello,a\nbye,b\n")

        frame, image_root = prepare_dataset(str(tmp_path), TEXT_SPEC)

        assert image_root is None
        assert frame["text"].tolist() == ["hello", "bye"]

    def test_image_task_unpacks_and_resolves_paths(self, tmp_path, monkeypatch):
        pytest.importorskip("pandas")
        from apis.app_api.fine_tuning.sagemaker_scripts import task_common

        monkeypatch.setattr(task_common, "EXTRACT_DIR", str(tmp_path / "extracted"))

        channel = tmp_path / "channel"
        channel.mkdir()
        _write_zip(
            channel / "dataset.zip",
            {
                "manifest.csv": "image,label\nimages/a.png,cat\nimages/b.png,dog\n",
                "images/a.png": PNG_BYTES,
                "images/b.png": PNG_BYTES,
            },
        )

        frame, image_root = prepare_dataset(str(channel), IMAGE_SPEC)

        assert image_root == str(tmp_path / "extracted")
        assert frame["label"].tolist() == ["cat", "dog"]
        # Image column is rewritten to absolute, existence-checked paths.
        for path in frame["image"]:
            assert path.startswith(image_root)

    def test_image_task_reports_a_missing_image(self, tmp_path, monkeypatch):
        pytest.importorskip("pandas")
        from apis.app_api.fine_tuning.sagemaker_scripts import task_common

        monkeypatch.setattr(task_common, "EXTRACT_DIR", str(tmp_path / "extracted"))

        channel = tmp_path / "channel"
        channel.mkdir()
        _write_zip(
            channel / "dataset.zip",
            {"manifest.csv": "image,label\nimages/gone.png,cat\n"},
        )

        with pytest.raises(FileNotFoundError, match="missing from the archive"):
            prepare_dataset(str(channel), IMAGE_SPEC)


class TestBuildLabelMapping:

    def test_maps_string_labels_to_contiguous_ids(self):
        pytest.importorskip("pandas")
        import pandas as pd

        frame = pd.DataFrame({"text": ["a", "b", "c"], "label": ["dog", "cat", "dog"]})

        frame, label2id, id2label = build_label_mapping(frame, TEXT_SPEC)

        assert label2id == {"cat": 0, "dog": 1}
        assert id2label == {0: "cat", 1: "dog"}
        assert frame["label"].tolist() == [1, 0, 1]

    def test_rejects_a_single_class(self):
        """One class cannot be classified, and the GPU error is unreadable."""
        pytest.importorskip("pandas")
        import pandas as pd

        frame = pd.DataFrame({"text": ["a", "b"], "label": ["same", "same"]})

        with pytest.raises(ValueError, match="at least 2 distinct values"):
            build_label_mapping(frame, TEXT_SPEC)


class TestLabelNames:

    def test_reads_int_keyed_id2label(self):
        import numpy as np

        config = MagicMock()
        config.id2label = {0: "cat", 1: "dog"}

        assert label_names(config, np.zeros((2, 2))) == ["cat", "dog"]

    def test_reads_string_keyed_id2label(self):
        """A config round-tripped through JSON comes back string-keyed."""
        import numpy as np

        config = MagicMock()
        config.id2label = {"0": "cat", "1": "dog"}

        assert label_names(config, np.zeros((2, 2))) == ["cat", "dog"]

    def test_falls_back_to_positional_names(self):
        import numpy as np

        config = MagicMock()
        config.id2label = None

        assert label_names(config, np.zeros((2, 3))) == ["class_0", "class_1", "class_2"]


class TestCopyInferenceBundle:

    def test_copies_files_to_code_dir(self, tmp_path):
        source_dir = tmp_path / "scripts"
        source_dir.mkdir()
        (source_dir / "inference.py").write_text("# inference handler")
        (source_dir / "requirements.txt").write_text("pandas\n")

        model_dir = tmp_path / "model"
        model_dir.mkdir()

        copied = copy_inference_bundle(str(model_dir), script_dir=str(source_dir))

        code_dir = model_dir / "code"
        assert code_dir.exists()
        assert (code_dir / "inference.py").read_text() == "# inference handler"
        assert (code_dir / "requirements.txt").exists()
        assert "inference.py" in copied

    def test_creates_code_directory(self, tmp_path):
        source_dir = tmp_path / "scripts"
        source_dir.mkdir()
        (source_dir / "inference.py").write_text("# handler")

        model_dir = tmp_path / "model"
        model_dir.mkdir()

        copy_inference_bundle(str(model_dir), script_dir=str(source_dir))

        assert (model_dir / "code").is_dir()

    def test_bundles_every_task_module_from_the_real_tree(self, tmp_path):
        """Batch Transform needs the task modules, not just inference.py.

        The handler dispatches on the artifact's task type, so a bundle
        missing a task module fails at load time on the inference container.
        """
        model_dir = tmp_path / "model"
        model_dir.mkdir()

        copied = copy_inference_bundle(str(model_dir))

        for expected in (
            "inference.py",
            "task_types.py",
            "task_common.py",
            "task_text_classification.py",
            "task_image_classification.py",
            "task_image_text_classification.py",
        ):
            assert expected in copied, f"{expected} missing from the inference bundle"


class TestDynamoDBProgressCallback:

    def test_on_train_begin_sets_zero(self):
        mock_client = MagicMock()
        cb = DynamoDBProgressCallback("table", "us-west-2", "PK", "SK")
        cb._client = mock_client

        cb.on_train_begin(MagicMock(), MagicMock(), MagicMock())

        mock_client.update_item.assert_called_once()
        call_kwargs = mock_client.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":p"]["N"] == "0.0"

    def test_on_train_end_sets_one(self):
        mock_client = MagicMock()
        cb = DynamoDBProgressCallback("table", "us-west-2", "PK", "SK")
        cb._client = mock_client

        cb.on_train_end(MagicMock(), MagicMock(), MagicMock())

        call_kwargs = mock_client.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":p"]["N"] == "1.0"

    def test_noop_when_no_table_configured(self):
        """When table_name is empty, no DynamoDB client is created."""
        cb = DynamoDBProgressCallback("", "us-west-2", "", "")
        assert cb._client is None

        cb._update_progress(0.5)

    def test_logs_warning_when_params_empty(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            cb = DynamoDBProgressCallback("", "us-west-2", "", "")

        assert any("disabled" in msg and "EMPTY" in msg for msg in caplog.messages)

    def test_logs_info_when_initialized(self, caplog):
        import logging

        mock_client = MagicMock()
        with patch("boto3.client", return_value=mock_client):
            with caplog.at_level(logging.INFO):
                cb = DynamoDBProgressCallback("my-table", "us-west-2", "PK#1", "SK#1")

        assert cb._client is not None
        assert any("initialized" in msg and "my-table" in msg for msg in caplog.messages)

    def test_throttles_step_updates(self):
        mock_client = MagicMock()
        cb = DynamoDBProgressCallback("table", "us-west-2", "PK", "SK")
        cb._client = mock_client

        state = MagicMock()
        state.max_steps = 100

        state.global_step = 5
        cb.on_step_end(MagicMock(), state, MagicMock())
        mock_client.update_item.assert_not_called()

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
        state.epoch = 3

        with caplog.at_level(logging.INFO):
            cb.on_evaluate(args, state, MagicMock(), metrics={"eval_accuracy": 0.95})

        assert not any("eval_accuracy" in msg for msg in caplog.messages)
