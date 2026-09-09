"""Task-type registry for the fine-tuning feature.

A *task type* bundles everything that varies between one kind of fine-tuning
job and another: what a training record looks like, how the dataset is
packaged for upload, which HuggingFace auto-class loads the model, which Deep
Learning Container it runs in, and what the inference output looks like.

Before this module existed those answers were hardcoded inline in ``train.py``
and ``inference.py``, which is why adding a second modality meant a rewrite
rather than a registration.

**This module must stay importable without torch, transformers, pandas or
PIL.**  It is imported three different ways:

* by ``routes.py`` in the app-api container, to validate a job *before* it is
  submitted and a GPU is billed;
* by the training and inference scripts running inside the SageMaker DLC;
* by the unit tests, which run in the backend venv where the ML stack is
  absent.

Keep it pure data and stdlib.  The same reasoning already governs
``DATASET_READERS`` in the training script: the supported-format contract has
to be assertable without importing pandas.
"""

from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Tuple


# =========================================================================
# Task type identifiers
# =========================================================================

TEXT_CLASSIFICATION = "text-classification"
IMAGE_CLASSIFICATION = "image-classification"
IMAGE_TEXT_CLASSIFICATION = "image-text-classification"
IMAGE_TEXT_TO_TEXT = "image-text-to-text"

#: Task assumed for a job record written before task types existed, and for a
#: request that omits the field.  Must stay ``TEXT_CLASSIFICATION`` — every
#: historical job in DynamoDB is one of these and has no ``task_type``
#: attribute to read.
DEFAULT_TASK_TYPE = TEXT_CLASSIFICATION


# =========================================================================
# Deep Learning Container families
# =========================================================================

# Which DLC image family a task runs in.  Vision tasks need a far newer
# transformers than the text tasks were built against, and bumping a single
# shared image would re-baseline every existing text job.  Keying the image
# map by family lets the two move independently: text jobs keep running the
# exact container they were validated on.
DLC_FAMILY_TEXT = "text"
DLC_FAMILY_VISION = "vision"
# Generative VLMs need PEFT and bitsandbytes on top of the vision stack.
# bitsandbytes requires torch>=2.4, which the text container (torch 2.1)
# cannot satisfy, so the dependency set cannot simply be added to the shared
# requirements file — the family is what selects the right one at packaging
# time.  See ``script_packaging_service.REQUIREMENTS_BY_FAMILY``.
DLC_FAMILY_VLM = "vlm"


# =========================================================================
# Spec
# =========================================================================

@dataclass(frozen=True)
class TaskSpec:
    """Everything the platform needs to know about one fine-tuning task type."""

    task_type: str
    display_name: str
    description: str

    # --- Training record contract -------------------------------------
    #: Columns every training record must carry.
    required_columns: Tuple[str, ...]
    #: Column holding the target class, or None for a generative task, which
    #: has no fixed label set.
    label_column: Optional[str]
    #: Column holding an image path relative to the archive root, or None for
    #: text-only tasks.
    image_column: Optional[str]
    #: Column holding free text, or None for image-only tasks.
    text_column: Optional[str]

    # --- Upload contract ----------------------------------------------
    #: Extensions the user may upload for training.
    upload_extensions: Tuple[str, ...]
    #: Extensions the manifest itself may use.  For archive-based tasks the
    #: manifest lives *inside* the archive, so this differs from
    #: ``upload_extensions``.
    manifest_extensions: Tuple[str, ...]
    #: True when the upload is an archive bundling a manifest plus image files.
    requires_archive: bool

    # --- Inference contract -------------------------------------------
    #: Extensions the user may upload as Batch Transform input.
    inference_upload_extensions: Tuple[str, ...]
    #: ContentType handed to Batch Transform for this task.
    inference_content_type: str
    #: MaxPayloadInMB for the transform job.  Batch Transform caps this at 100.
    inference_max_payload_mb: int

    # --- Runtime -------------------------------------------------------
    dlc_family: str
    #: HuggingFace Hub pipeline tags whose models can serve this task.  Drives
    #: both the model-search filter and the pre-flight check on a custom id.
    hf_pipeline_tags: Tuple[str, ...]
    default_instance_type: str
    default_hyperparameters: Mapping[str, str]

    # --- Generative tasks ----------------------------------------------
    #: Column holding the target text a generative task learns to produce.
    #: None for the classification tasks.  Defaulted so the three existing
    #: specs are untouched.
    response_column: Optional[str] = None
    #: True when the model emits free text rather than a class distribution.
    #: Drives the output contract: probability columns vs a text column.
    is_generative: bool = False

    def supports_extension(self, filename: str) -> bool:
        """True when ``filename`` is an acceptable training upload."""
        return filename.lower().endswith(self.upload_extensions)

    def supports_inference_extension(self, filename: str) -> bool:
        """True when ``filename`` is an acceptable Batch Transform input."""
        return filename.lower().endswith(self.inference_upload_extensions)


# =========================================================================
# Shared hyperparameter defaults
# =========================================================================

_COMMON_HYPERPARAMETERS = {
    "epochs": "3",
    "learning_rate": "5e-5",
    "weight_decay": "0.01",
    "split_ratio": "0.8",
    "seed": "42",
}

# Manifest formats a task can read.  Kept identical across tasks so a
# researcher who already has a CSV workflow keeps it when they add images.
_MANIFEST_EXTENSIONS = (".csv", ".jsonl", ".json")


# =========================================================================
# Registry
# =========================================================================

TASK_SPECS: Dict[str, TaskSpec] = {
    TEXT_CLASSIFICATION: TaskSpec(
        task_type=TEXT_CLASSIFICATION,
        display_name="Text classification",
        description=(
            "Assign a label to a piece of text. Upload a CSV/JSONL/JSON file "
            'where each record has a "text" and a "label" field.'
        ),
        required_columns=("text", "label"),
        label_column="label",
        image_column=None,
        text_column="text",
        upload_extensions=_MANIFEST_EXTENSIONS,
        manifest_extensions=_MANIFEST_EXTENSIONS,
        requires_archive=False,
        inference_upload_extensions=(".txt", ".csv", ".jsonl", ".json"),
        inference_content_type="text/plain",
        inference_max_payload_mb=6,
        dlc_family=DLC_FAMILY_TEXT,
        hf_pipeline_tags=(
            "fill-mask",
            "text-classification",
            "feature-extraction",
            "token-classification",
            "text-generation",
        ),
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters={
            **_COMMON_HYPERPARAMETERS,
            "per_device_train_batch_size": "16",
            "context_length": "512",
        },
    ),
    IMAGE_CLASSIFICATION: TaskSpec(
        task_type=IMAGE_CLASSIFICATION,
        display_name="Image classification",
        description=(
            "Assign a label to an image. Upload a .zip containing a "
            'manifest (CSV/JSONL/JSON) with "image" and "label" fields, plus '
            "the image files the manifest points at."
        ),
        required_columns=("image", "label"),
        label_column="label",
        image_column="image",
        text_column=None,
        upload_extensions=(".zip",),
        manifest_extensions=_MANIFEST_EXTENSIONS,
        requires_archive=True,
        inference_upload_extensions=(".zip",),
        # Batch Transform receives the whole archive as one payload and the
        # handler unpacks it, which keeps the single-CSV result contract (and
        # therefore the whole result viewer) identical to the text tasks.
        inference_content_type="application/zip",
        inference_max_payload_mb=100,
        dlc_family=DLC_FAMILY_VISION,
        hf_pipeline_tags=(
            "image-classification",
            "image-feature-extraction",
            "zero-shot-image-classification",
        ),
        default_instance_type="ml.g6.xlarge",
        default_hyperparameters={
            **_COMMON_HYPERPARAMETERS,
            "per_device_train_batch_size": "16",
            "image_size": "224",
        },
    ),
    IMAGE_TEXT_CLASSIFICATION: TaskSpec(
        task_type=IMAGE_TEXT_CLASSIFICATION,
        display_name="Image + text classification",
        description=(
            "Assign a label to an image/text pair. Upload a .zip containing a "
            'manifest (CSV/JSONL/JSON) with "image", "text" and "label" '
            "fields, plus the image files the manifest points at."
        ),
        required_columns=("image", "text", "label"),
        label_column="label",
        image_column="image",
        text_column="text",
        upload_extensions=(".zip",),
        manifest_extensions=_MANIFEST_EXTENSIONS,
        requires_archive=True,
        inference_upload_extensions=(".zip",),
        inference_content_type="application/zip",
        inference_max_payload_mb=100,
        dlc_family=DLC_FAMILY_VISION,
        # Deliberately narrow. This task needs a *dual encoder* exposing both
        # get_image_features and get_text_features, which in practice means
        # the CLIP/SigLIP/ALIGN family — exactly what carries the
        # zero-shot-image-classification tag.
        #
        # "image-text-to-text" (LLaVA, Qwen-VL) and "visual-question-answering"
        # (ViLT, BLIP) are generative or fusion models with no text tower to
        # pool, and stay excluded here: allowing them would let the pre-flight
        # pass a model that only fails later, on a billed GPU, inside the
        # trainer's dual-encoder check.  Generative VLMs have their own task —
        # see IMAGE_TEXT_TO_TEXT below.
        hf_pipeline_tags=("zero-shot-image-classification",),
        default_instance_type="ml.g6.xlarge",
        default_hyperparameters={
            **_COMMON_HYPERPARAMETERS,
            "per_device_train_batch_size": "16",
            "image_size": "224",
            "context_length": "77",
        },
    ),
    IMAGE_TEXT_TO_TEXT: TaskSpec(
        task_type=IMAGE_TEXT_TO_TEXT,
        display_name="Image + text to text",
        description=(
            "Teach a vision-language model to answer about an image in your "
            "own style or vocabulary. Upload a .zip containing a manifest "
            '(CSV/JSONL/JSON) with "image", "prompt" and "response" fields, '
            "plus the image files the manifest points at."
        ),
        required_columns=("image", "prompt", "response"),
        # Generative: there is no class list, so no label column and no
        # softmax.  ``response`` is the target text, not a category.
        label_column=None,
        image_column="image",
        text_column="prompt",
        response_column="response",
        is_generative=True,
        upload_extensions=(".zip",),
        manifest_extensions=_MANIFEST_EXTENSIONS,
        requires_archive=True,
        inference_upload_extensions=(".zip",),
        inference_content_type="application/zip",
        inference_max_payload_mb=100,
        dlc_family=DLC_FAMILY_VLM,
        hf_pipeline_tags=("image-text-to-text",),
        # 48GB (L40S).  A 7B VLM in 4-bit needs ~5GB of weights but the
        # activations for a high-resolution image are what actually size the
        # card: LLaVA-1.6's AnyRes tiling emits up to 2880 image tokens per
        # image.  The 24GB g6/g5 instances OOM on the larger checkpoints, so
        # the default starts where the whole catalog fits.
        default_instance_type="ml.g6e.xlarge",
        default_hyperparameters={
            **_COMMON_HYPERPARAMETERS,
            # LoRA wants a markedly higher LR than full fine-tuning: only the
            # adapter matrices move, and they start at zero.
            "learning_rate": "1e-4",
            # One sequence per step, recovered to an effective batch of 8 by
            # accumulation.  A VLM sequence is thousands of tokens, so a
            # literal batch of 16 OOMs on any instance we offer.
            "per_device_train_batch_size": "1",
            "gradient_accumulation_steps": "8",
            "context_length": "1024",
            "load_in_4bit": "true",
            "lora_r": "16",
            "lora_alpha": "32",
            "lora_dropout": "0.05",
            "max_new_tokens": "256",
        },
    ),
}

#: Stable, deterministic ordering for anything user-facing.
TASK_TYPES: Tuple[str, ...] = (
    TEXT_CLASSIFICATION,
    IMAGE_CLASSIFICATION,
    IMAGE_TEXT_CLASSIFICATION,
    IMAGE_TEXT_TO_TEXT,
)

#: Task types whose upload is an archive of a manifest plus image files.
ARCHIVE_TASK_TYPES: Tuple[str, ...] = tuple(
    t for t in TASK_TYPES if TASK_SPECS[t].requires_archive
)

#: Task types that emit free text instead of a class distribution.  These are
#: the ones whose result file carries an output column rather than one
#: probability column per class.
GENERATIVE_TASK_TYPES: Tuple[str, ...] = tuple(
    t for t in TASK_TYPES if TASK_SPECS[t].is_generative
)


def get_task_spec(task_type: Optional[str]) -> TaskSpec:
    """Return the spec for ``task_type``.

    ``None`` and the empty string resolve to :data:`DEFAULT_TASK_TYPE` so that
    a job record written before task types existed still loads.

    Raises ValueError for an unknown task type.
    """
    resolved = task_type or DEFAULT_TASK_TYPE
    spec = TASK_SPECS.get(resolved)
    if spec is None:
        supported = ", ".join(TASK_TYPES)
        raise ValueError(
            f"Unknown task type '{task_type}'. Supported task types: {supported}"
        )
    return spec


def requires_images(task_type: Optional[str]) -> bool:
    """True when the task's training records reference image files."""
    return get_task_spec(task_type).image_column is not None


def is_generative(task_type: Optional[str]) -> bool:
    """True when the task produces free text rather than class probabilities."""
    return get_task_spec(task_type).is_generative
