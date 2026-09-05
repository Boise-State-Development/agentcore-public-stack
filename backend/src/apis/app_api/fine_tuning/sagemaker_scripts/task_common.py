"""Shared helpers for every fine-tuning task type.

Everything here is task-agnostic: dataset discovery, archive unpacking, label
normalisation, the train/test split, progress reporting and the bundling of
the inference handler into the model artifact.  Task-specific behaviour —
which auto-class loads the model, how a batch is collated — lives in the
``task_*.py`` sibling modules.

**Import shim.** These modules are consumed two ways: as a package
(``apis.app_api.fine_tuning.sagemaker_scripts.task_common``) by the unit tests
and the app-api container, and as flat top-level modules inside the SageMaker
DLC, where ``script_packaging_service`` tars them at the archive root with no
package around them.  The try/except below is what lets one file serve both;
it is not defensive coding, it is the two real layouts.

**Heavy ML dependencies are imported lazily inside functions.**  torch,
transformers, pandas and PIL exist only in the DLC, and the module has to stay
importable in the backend venv so the dataset contract can be unit-tested
without them.
"""

import logging
import os
import shutil
import zipfile

try:  # package context: unit tests and the app-api container
    from .. import task_types
except ImportError:  # pragma: no cover - flat sourcedir inside the SageMaker DLC
    import task_types  # type: ignore

try:
    from transformers import TrainerCallback
except ImportError:  # pragma: no cover - local dev/test without transformers
    TrainerCallback = object

logger = logging.getLogger(__name__)


# =========================================================================
# Dataset formats
# =========================================================================

# Formats the trainer can read, mapped to the pandas reader that loads them.
# Plain .txt is absent because it has no way to express a label; it stays
# valid for *inference* input, which is unlabelled.
#
# Kept as data rather than an if/elif chain so the supported-format contract
# can be asserted without importing pandas, which only exists inside the
# SageMaker container.
DATASET_READERS = {
    ".csv": ("read_csv", {}),
    ".jsonl": ("read_json", {"lines": True}),
    ".json": ("read_json", {}),
}

#: Image files an archive-based dataset may reference.
SUPPORTED_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tiff")

#: Directory the training script unpacks an uploaded archive into.  Sits
#: outside the input channel so the extracted tree is never mistaken for
#: another dataset file on a re-scan.
EXTRACT_DIR = "/opt/ml/input/extracted"


# =========================================================================
# Dataset discovery
# =========================================================================

def find_file_in_dir(directory, extensions, description="dataset"):
    """Return the first file under ``directory`` matching ``extensions``.

    Searches recursively, deepest-last, so a manifest at the archive root wins
    over one nested inside an image folder.  Raises FileNotFoundError when the
    directory is missing or holds no match.
    """
    if not os.path.isdir(directory):
        raise FileNotFoundError(f"Directory does not exist: {directory}")

    matches = []
    for root, _dirs, files in os.walk(directory):
        relative = os.path.relpath(root, directory)
        # relpath returns "." for the top level and "images" one level down —
        # both hold zero separators, so counting them alone ranks a nested
        # manifest equal to a root one.
        depth = 0 if relative == os.curdir else relative.count(os.sep) + 1
        for name in sorted(files):
            if name.startswith("._") or name.startswith("."):
                # Skip macOS resource forks, which zip up alongside the real
                # files and would otherwise be picked as the manifest.
                continue
            if name.lower().endswith(tuple(extensions)):
                matches.append((depth, os.path.join(root, name)))

    if not matches:
        supported = ", ".join(extensions)
        raise FileNotFoundError(
            f"No {description} file found in {directory}. "
            f"Supported formats: {supported}"
        )

    matches.sort(key=lambda pair: (pair[0], pair[1]))
    return matches[0][1]


def resolve_dataset_reader(dataset_path):
    """Return the (pandas reader name, kwargs) pair for a manifest file.

    Raises ValueError for an extension the trainer cannot read.
    """
    extension = os.path.splitext(dataset_path)[1].lower()

    if extension not in DATASET_READERS:
        supported = ", ".join(DATASET_READERS)
        raise ValueError(
            f"Unsupported dataset format '{extension}'. Supported formats: {supported}"
        )

    return DATASET_READERS[extension]


def validate_dataset_columns(columns, dataset_path, spec):
    """Raise ValueError if a column the task requires is absent."""
    missing = [c for c in spec.required_columns if c not in columns]
    if missing:
        required = ", ".join(f'"{c}"' for c in spec.required_columns)
        raise ValueError(
            f"Dataset {os.path.basename(dataset_path)} is missing required "
            f"column(s): {', '.join(missing)}. A {spec.display_name.lower()} "
            f"record needs {required}."
        )


# =========================================================================
# Archive handling
# =========================================================================

def _is_within(directory, target):
    """True when ``target`` resolves to a path inside ``directory``."""
    directory = os.path.realpath(directory)
    target = os.path.realpath(target)
    return os.path.commonpath([directory, target]) == directory


def extract_archive(archive_path, dest_dir):
    """Safely unpack a user-uploaded .zip into ``dest_dir``.

    Rejects absolute paths, parent-directory traversal and symlinks — the
    archive is untrusted input uploaded by a user, and a naive ``extractall``
    would let a crafted entry write anywhere the training container can reach
    (``Zip-Slip``).  Returns ``dest_dir``.
    """
    os.makedirs(dest_dir, exist_ok=True)

    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            name = member.filename
            if name.endswith("/"):
                continue
            if os.path.isabs(name) or ".." in name.replace("\\", "/").split("/"):
                raise ValueError(
                    f"Refusing to extract unsafe archive entry: {name!r}"
                )
            target = os.path.join(dest_dir, name)
            if not _is_within(dest_dir, os.path.dirname(target) or dest_dir):
                raise ValueError(
                    f"Refusing to extract archive entry outside the "
                    f"destination: {name!r}"
                )
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with archive.open(member) as source, open(target, "wb") as sink:
                shutil.copyfileobj(source, sink)

    logger.info(f"Extracted {archive_path} to {dest_dir}")
    return dest_dir


def resolve_image_path(image_root, relative_path):
    """Resolve a manifest image reference against the archive root.

    Raises ValueError if the reference escapes the archive, FileNotFoundError
    if the file is not there.
    """
    candidate = os.path.join(image_root, str(relative_path).strip())

    if not _is_within(image_root, candidate):
        raise ValueError(
            f"Image path escapes the dataset archive: {relative_path!r}"
        )
    if not os.path.isfile(candidate):
        raise FileNotFoundError(
            f"Image referenced by the manifest is missing from the archive: "
            f"{relative_path!r}"
        )
    return candidate


# =========================================================================
# Dataset loading
# =========================================================================

def load_manifest_frame(manifest_path, spec):
    """Load a manifest file into a DataFrame and validate its columns."""
    import pandas as pd

    reader_name, reader_kwargs = resolve_dataset_reader(manifest_path)
    frame = getattr(pd, reader_name)(manifest_path, **reader_kwargs)

    validate_dataset_columns(frame.columns, manifest_path, spec)

    return frame


def prepare_dataset(channel_dir, spec):
    """Locate, unpack and load the dataset for ``spec``.

    Returns ``(frame, image_root)``.  ``image_root`` is None for text-only
    tasks; for archive tasks it is the extracted archive root, and the frame's
    image column has been rewritten to absolute, existence-checked paths.
    """
    if spec.requires_archive:
        archive_path = find_file_in_dir(channel_dir, spec.upload_extensions, "dataset archive")
        logger.info(f"Unpacking dataset archive {archive_path}")
        image_root = extract_archive(archive_path, EXTRACT_DIR)
        manifest_path = find_file_in_dir(image_root, spec.manifest_extensions, "manifest")
    else:
        image_root = None
        manifest_path = find_file_in_dir(channel_dir, spec.manifest_extensions, "dataset")

    logger.info(f"Loading dataset manifest from {manifest_path}")
    frame = load_manifest_frame(manifest_path, spec)

    if spec.image_column:
        frame = frame.copy()
        frame[spec.image_column] = [
            resolve_image_path(image_root, value)
            for value in frame[spec.image_column]
        ]
        logger.info(f"Resolved {len(frame)} image paths against {image_root}")

    if len(frame) == 0:
        raise ValueError(
            f"Dataset {os.path.basename(manifest_path)} contains no records."
        )

    return frame, image_root


def build_label_mapping(frame, spec):
    """Normalise the label column to contiguous ids.

    Returns ``(frame, label2id, id2label)`` with the label column replaced by
    integer ids.  Non-numeric class names are supported and preserved in the
    mapping so the model config can carry them through to inference.
    """
    import pandas as pd

    label_column = spec.label_column
    label_names = sorted(pd.Series(frame[label_column]).astype(str).unique())

    if len(label_names) < 2:
        raise ValueError(
            f"Dataset needs at least 2 distinct values in the "
            f'"{label_column}" column, found {len(label_names)}: {label_names}.'
        )

    label2id = {name: index for index, name in enumerate(label_names)}
    id2label = {index: name for name, index in label2id.items()}

    frame = frame.copy()
    frame[label_column] = frame[label_column].astype(str).map(label2id)

    logger.info(f"Label mapping ({len(label_names)} classes): {label2id}")
    return frame, label2id, id2label


def split_frame(frame, split_ratio, seed):
    """Split a DataFrame into shuffled train/eval HuggingFace Datasets."""
    from datasets import Dataset

    dataset = Dataset.from_pandas(frame.reset_index(drop=True))
    dataset = dataset.train_test_split(test_size=1 - split_ratio, seed=seed)

    logger.info(
        f"Data split: {len(dataset['train'])} train / {len(dataset['test'])} eval"
    )
    return dataset["train"].shuffle(seed=seed), dataset["test"].shuffle(seed=seed)


# =========================================================================
# Model helpers
# =========================================================================

def resolve_max_context_length(config, tokenizer):
    """Resolve the effective maximum text context length from a model config.

    Checks the usual config attributes and returns the smallest valid value,
    or None if none can be determined.  Multimodal configs keep these on a
    nested ``text_config``, so that is consulted too — reading only the top
    level silently yields None on every vision-language model.
    """
    sources = [config, getattr(config, "text_config", None)]

    candidates = []
    for source in sources:
        if source is None:
            continue
        candidates.extend(
            [
                getattr(source, "max_position_embeddings", None),
                getattr(source, "n_positions", None),
                getattr(source, "seq_length", None),
            ]
        )
    candidates.append(getattr(tokenizer, "model_max_length", None))

    def _valid(value):
        try:
            return value is not None and 0 < float(value) < 1_000_000
        except Exception:
            return False

    valid = [int(v) for v in candidates if _valid(v)]
    return min(valid) if valid else None


def build_training_arguments(**kwargs):
    """Construct ``TrainingArguments``, tolerating the evaluation-strategy rename.

    Text tasks run on a transformers 4.36 container, where the argument is
    ``evaluation_strategy``.  Vision tasks run on 4.56, where it is
    ``eval_strategy`` and the old spelling is deprecated.  Pinning either name
    breaks the other container, so inspect the signature and pass whichever
    this transformers actually accepts.
    """
    import inspect

    from transformers import TrainingArguments

    strategy = kwargs.pop("eval_strategy", None)
    if strategy is not None:
        parameters = inspect.signature(TrainingArguments.__init__).parameters
        key = "eval_strategy" if "eval_strategy" in parameters else "evaluation_strategy"
        kwargs[key] = strategy

    return TrainingArguments(**kwargs)


def compute_accuracy(eval_pred):
    """Accuracy metric shared by every classification task."""
    import numpy as np
    import evaluate

    metric = evaluate.load("accuracy")
    logits, labels = eval_pred
    if isinstance(logits, tuple):
        logits = logits[0]
    predictions = np.argmax(logits, axis=-1)
    return metric.compute(predictions=predictions, references=labels)


def label_names(config, probabilities):
    """Recover ordered class names from a model config.

    Every classification task writes ``id2label`` into the config at training
    time, so inference can name the probability columns without the dataset.
    """
    num_labels = probabilities.shape[1] if len(probabilities.shape) > 1 else 0
    id2label = getattr(config, "id2label", None)

    if isinstance(id2label, dict):
        return [
            id2label.get(i) or id2label.get(str(i)) or f"class_{i}"
            for i in range(num_labels)
        ]
    if isinstance(id2label, (list, tuple)):
        return list(id2label)[:num_labels]
    return [f"class_{i}" for i in range(num_labels)]


# =========================================================================
# Callbacks
# =========================================================================

class DynamoDBProgressCallback(TrainerCallback):
    """Reports training progress (0.0-1.0) to DynamoDB.

    Throttles writes to every 10 steps to reduce API calls.
    Fails silently so that DynamoDB issues don't abort training.
    """

    def __init__(self, table_name, region, pk, sk):
        super().__init__()
        self._table_name = table_name
        self._pk = pk
        self._sk = sk
        self._client = None
        if table_name and pk and sk:
            try:
                import boto3

                self._client = boto3.client("dynamodb", region_name=region)
                logger.info(
                    f"DynamoDB progress callback initialized: "
                    f"table={table_name}, region={region}"
                )
            except Exception as e:
                logger.warning(f"Could not create DynamoDB client: {e}")
        else:
            logger.warning(
                f"DynamoDB progress callback disabled — missing config: "
                f"table_name={'set' if table_name else 'EMPTY'}, "
                f"pk={'set' if pk else 'EMPTY'}, "
                f"sk={'set' if sk else 'EMPTY'}"
            )

    def _update_progress(self, progress):
        if not self._client:
            return
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key={
                    "PK": {"S": self._pk},
                    "SK": {"S": self._sk},
                },
                UpdateExpression="SET training_progress = :p",
                ExpressionAttributeValues={
                    ":p": {"N": str(round(progress, 4))},
                },
            )
        except Exception as e:
            logger.warning(f"Failed to update progress in DynamoDB: {e}")

    def _from_state(self, state):
        if getattr(state, "max_steps", 0) and state.max_steps > 0:
            return min(1.0, max(0.0, state.global_step / state.max_steps))
        if getattr(state, "num_train_epochs", 0) and getattr(
            state, "epoch", None
        ) is not None:
            total = float(state.num_train_epochs)
            if total > 0:
                return min(1.0, max(0.0, float(state.epoch) / total))
        return None

    def on_train_begin(self, args, state, control, **kwargs):
        self._update_progress(0.0)

    def on_log(self, args, state, control, logs=None, **kwargs):
        progress = self._from_state(state)
        if progress is not None:
            self._update_progress(progress)

    def on_step_end(self, args, state, control, **kwargs):
        if state.global_step % 10 == 0:
            progress = self._from_state(state)
            if progress is not None:
                self._update_progress(progress)

    def on_train_end(self, args, state, control, **kwargs):
        self._update_progress(1.0)


class SageMakerLoggingCallback(TrainerCallback):
    """Logs epoch accuracy to stdout (captured by CloudWatch)."""

    def on_evaluate(self, args, state, control, metrics=None, **kwargs):
        if state.epoch is not None and state.epoch < args.num_train_epochs:
            if metrics is not None:
                accuracy = metrics.get("eval_accuracy")
                if accuracy is not None:
                    logger.info(
                        f"Epoch {int(state.epoch)} finished with "
                        f"eval_accuracy={accuracy:.4f}"
                    )
            logger.info("Starting next epoch...")


def build_callbacks(args):
    """Assemble the callback list every task uses."""
    return [
        SageMakerLoggingCallback(),
        DynamoDBProgressCallback(
            table_name=args.dynamodb_table_name,
            region=args.dynamodb_region,
            pk=args.job_pk,
            sk=args.job_sk,
        ),
    ]


# =========================================================================
# Inference bundling
# =========================================================================

# Files copied into model.tar.gz so Batch Transform can serve the model.
# SageMaker discovers code/inference.py inside the artifact and uses it as the
# handler; the task modules and the task registry travel with it because the
# handler dispatches on the task type the same way the trainer does.
INFERENCE_BUNDLE_FILES = (
    "inference.py",
    "requirements.txt",
    "task_types.py",
    "task_common.py",
    "task_text_classification.py",
    "task_image_classification.py",
    "task_image_text_classification.py",
)


def copy_inference_bundle(model_output_dir, script_dir=None):
    """Copy the inference handler and its task modules into ``model_dir/code/``.

    ``script_dir`` defaults to the directory this module lives in and exists so
    tests can point the copy at a fixture tree instead of monkeypatching
    ``os.path``.
    """
    code_dir = os.path.join(model_output_dir, "code")
    os.makedirs(code_dir, exist_ok=True)

    script_dir = script_dir or os.path.dirname(os.path.abspath(__file__))
    copied = []
    for filename in INFERENCE_BUNDLE_FILES:
        source = os.path.join(script_dir, filename)
        if not os.path.exists(source):
            # task_types.py lives one level up in the package layout; inside
            # the flat SageMaker sourcedir it sits alongside this file.
            source = os.path.join(os.path.dirname(script_dir), filename)
        if os.path.exists(source):
            shutil.copy2(source, os.path.join(code_dir, filename))
            copied.append(filename)
            logger.info(f"Copied {filename} to {code_dir}")
        else:
            logger.warning(f"Script file not found, skipping: {filename}")
    return copied
