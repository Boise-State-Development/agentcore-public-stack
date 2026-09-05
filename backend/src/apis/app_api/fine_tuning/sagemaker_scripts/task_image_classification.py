"""Image classification: image -> label.

Uses ``AutoModelForImageClassification``, so the trained artifact is an
ordinary HuggingFace model directory that ``from_pretrained`` reloads with no
custom code — the same round-trip the text task gets.

Images are opened lazily in the collator rather than materialised by a
``Dataset.map``.  Mapping pixel tensors over the whole dataset up front is
what turns a modest image corpus into an out-of-memory kill several billed
minutes into training; the dataset carries file paths and the collator reads
each batch's images as it needs them.
"""

import logging
import os

try:  # package context: unit tests and the app-api container
    from . import task_common
except ImportError:  # pragma: no cover - flat sourcedir inside the SageMaker DLC
    import task_common  # type: ignore

logger = logging.getLogger(__name__)

BATCH_SIZE = 32


def load_image(path):
    """Open an image and normalise it to RGB.

    Greyscale and palette images (common in scanned research corpora) have to
    be converted, or the processor emits a tensor with the wrong channel count
    and the batch fails to stack.
    """
    from PIL import Image

    with Image.open(path) as image:
        return image.convert("RGB")


def build_collator(processor, label_column, image_column):
    """Return a collate_fn that opens each batch's images and stacks them."""
    import torch

    def collate(features):
        images = [load_image(feature[image_column]) for feature in features]
        batch = processor(images=images, return_tensors="pt")
        if label_column is not None and label_column in features[0]:
            batch["labels"] = torch.tensor(
                [feature[label_column] for feature in features], dtype=torch.long
            )
        return batch

    return collate


# =========================================================================
# Training
# =========================================================================

def train(args, spec):
    """Fine-tune an image classification model."""
    from transformers import (
        AutoConfig,
        AutoImageProcessor,
        AutoModelForImageClassification,
        Trainer,
    )

    train_channel = os.environ.get("SM_CHANNEL_TRAIN", "/opt/ml/input/data/train")
    model_dir = os.environ.get("SM_MODEL_DIR", "/opt/ml/model")

    frame, _ = task_common.prepare_dataset(train_channel, spec)
    frame, label2id, id2label = task_common.build_label_mapping(frame, spec)
    num_labels = len(label2id)

    processor = AutoImageProcessor.from_pretrained(args.model_name_or_path)
    if args.image_size:
        # Honour the requested resolution where the processor exposes one.
        # Some processors key this "shortest_edge" instead of height/width.
        if isinstance(getattr(processor, "size", None), dict):
            if "height" in processor.size:
                processor.size = {"height": args.image_size, "width": args.image_size}
            elif "shortest_edge" in processor.size:
                processor.size = {"shortest_edge": args.image_size}

    config = AutoConfig.from_pretrained(
        args.model_name_or_path,
        num_labels=num_labels,
        label2id=label2id,
        id2label=id2label,
    )

    model = AutoModelForImageClassification.from_pretrained(
        args.model_name_or_path,
        config=config,
        torch_dtype="auto",
        # The base checkpoint's head has the wrong class count (or none at
        # all). Without this, loading raises instead of re-initialising it.
        ignore_mismatched_sizes=True,
    )

    train_dataset, eval_dataset = task_common.split_frame(
        frame, args.split_ratio, args.seed
    )

    training_args = task_common.build_training_arguments(
        output_dir="/opt/ml/checkpoints",
        learning_rate=args.learning_rate,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.per_device_train_batch_size,
        weight_decay=args.weight_decay,
        eval_strategy="epoch",
        save_strategy="no",
        logging_dir="/opt/ml/output/tensorboard",
        # The collator returns pixel tensors, not model-signature columns;
        # Trainer's default column pruning would strip the image paths it
        # needs before the collator ever sees them.
        remove_unused_columns=False,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        compute_metrics=task_common.compute_accuracy,
        callbacks=task_common.build_callbacks(args),
        data_collator=build_collator(processor, spec.label_column, spec.image_column),
    )

    logger.info(
        f"Starting fine-tuning: task={spec.task_type}, "
        f"model={args.model_name_or_path}, epochs={args.epochs}, "
        f"batch_size={args.per_device_train_batch_size}, "
        f"image_size={args.image_size}"
    )
    trainer.train()

    metrics = trainer.evaluate()
    logger.info(f"Final evaluation: accuracy={metrics.get('eval_accuracy', 'N/A')}")

    trainer.save_model(model_dir)
    processor.save_pretrained(model_dir)
    logger.info(f"Saved model to {model_dir}")

    return metrics


# =========================================================================
# Inference
# =========================================================================

def model_fn(model_dir):
    """Load the model and image processor for Batch Transform."""
    import torch
    from transformers import AutoImageProcessor, AutoModelForImageClassification

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    processor = AutoImageProcessor.from_pretrained(model_dir)
    model = AutoModelForImageClassification.from_pretrained(
        model_dir, torch_dtype="auto"
    )
    model.to(device)
    model.eval()

    logger.info(f"Loaded image classification model from {model_dir} on {device}")
    return {"model": model, "processor": processor, "device": device}


def input_fn(request_body, content_type, spec):
    """Unpack a .zip of images into records.

    Batch Transform hands the archive over as a single payload; unpacking it
    here is what keeps the one-file-in, one-CSV-out contract identical to the
    text tasks, and therefore keeps the whole result viewer unchanged.
    """
    import io
    import tempfile

    if not isinstance(request_body, (bytes, bytearray)):
        raise ValueError(
            f"Expected archive bytes for {spec.task_type}, got {type(request_body).__name__}"
        )

    work_dir = tempfile.mkdtemp(prefix="inference-images-")
    archive_path = os.path.join(work_dir, "input.zip")
    with open(archive_path, "wb") as handle:
        handle.write(bytes(request_body))

    root = task_common.extract_archive(archive_path, os.path.join(work_dir, "extracted"))

    records = []
    for directory, _dirs, files in os.walk(root):
        for name in sorted(files):
            if name.startswith("."):
                continue
            if not name.lower().endswith(task_common.SUPPORTED_IMAGE_EXTENSIONS):
                continue
            path = os.path.join(directory, name)
            records.append(
                {
                    spec.image_column: path,
                    "identifier": os.path.relpath(path, root),
                }
            )

    if not records:
        raise ValueError("Archive contains no readable image files.")

    logger.info(f"Unpacked {len(records)} images for inference")
    return records


def predict_fn(records, loaded, spec):
    """Run batched image inference with softmax probabilities."""
    import numpy as np
    import torch

    model, processor, device = loaded["model"], loaded["processor"], loaded["device"]

    if not records:
        return {"identifiers": [], "probabilities": np.zeros((0, 0)), "labels": []}

    collate = build_collator(processor, None, spec.image_column)

    batches = []
    with torch.no_grad():
        for start in range(0, len(records), BATCH_SIZE):
            batch = collate(records[start : start + BATCH_SIZE])
            batch = {k: v.to(device) for k, v in batch.items()}
            logits = model(**batch).logits
            batches.append(torch.softmax(logits, dim=-1).cpu().numpy())

    probabilities = np.vstack(batches) if batches else np.zeros((0, 0))

    return {
        "identifiers": [record["identifier"] for record in records],
        "probabilities": probabilities,
        "labels": task_common.label_names(model.config, probabilities),
    }
