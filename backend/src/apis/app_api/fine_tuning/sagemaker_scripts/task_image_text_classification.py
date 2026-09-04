"""Image + text classification: (image, text) -> label.

Transformers has no general auto-class for this shape.  ``AutoModelFor
SequenceClassification`` is text-only, and ``AutoModelForImageTextToText`` is
generative — it emits tokens, not a class distribution.  So this task builds
an explicit small model instead of pretending an auto-class fits:

    frozen-or-tuned dual encoder (CLIP / SigLIP / ALIGN)
        -> pooled image embedding  ++  pooled text embedding
        -> dropout -> linear classifier -> logits

That composition works across the whole CLIP-family of checkpoints rather than
a single architecture, and it keeps the output contract — a softmax over the
dataset's own classes — byte-identical to the other two tasks, so the Batch
Transform result CSV and the entire result viewer stay unchanged.

Because the result is not a ``PreTrainedModel``, ``Trainer.save_model`` would
write a bare state dict and lose the backbone config.  The artifact is
therefore saved explicitly: the backbone via ``save_pretrained``, the head via
``torch.save``, and the wiring via ``fusion_head.json``.  :func:`load` is the
exact inverse and is what ``model_fn`` calls at inference.
"""

import json
import logging
import os

try:  # package context: unit tests and the app-api container
    from . import task_common
except ImportError:  # pragma: no cover - flat sourcedir inside the SageMaker DLC
    import task_common  # type: ignore

logger = logging.getLogger(__name__)

BATCH_SIZE = 32

#: Written next to the backbone so :func:`load` can rebuild the head without
#: the training arguments.
HEAD_CONFIG_FILENAME = "fusion_head.json"
HEAD_WEIGHTS_FILENAME = "fusion_head.pt"


# =========================================================================
# Model
# =========================================================================

def build_fusion_model(backbone, image_dim, text_dim, num_labels, dropout=0.1):
    """Wrap a dual encoder with a concat-and-classify head.

    Defined as a factory rather than a module-level class so this file stays
    importable without torch — the backend venv and the unit tests have no ML
    stack, and the dataset/artifact contract has to be testable there.
    """
    import torch
    import torch.nn as nn

    class ImageTextClassifier(nn.Module):
        def __init__(self):
            super().__init__()
            self.backbone = backbone
            self.image_dim = image_dim
            self.text_dim = text_dim
            self.num_labels = num_labels
            self.dropout = nn.Dropout(dropout)
            self.classifier = nn.Linear(image_dim + text_dim, num_labels)

        def encode(self, pixel_values, input_ids, attention_mask=None):
            image_features = self.backbone.get_image_features(pixel_values=pixel_values)
            text_features = self.backbone.get_text_features(
                input_ids=input_ids, attention_mask=attention_mask
            )
            return image_features, text_features

        def forward(self, pixel_values=None, input_ids=None, attention_mask=None, labels=None, **_ignored):
            image_features, text_features = self.encode(
                pixel_values, input_ids, attention_mask
            )
            fused = torch.cat([image_features, text_features], dim=-1)
            logits = self.classifier(self.dropout(fused))

            loss = None
            if labels is not None:
                loss = nn.functional.cross_entropy(logits, labels)

            # Trainer accepts a dict output as long as "loss" is present when
            # labels were supplied.
            return {"loss": loss, "logits": logits} if loss is not None else {"logits": logits}

        def save(self, output_dir):
            os.makedirs(output_dir, exist_ok=True)
            self.backbone.save_pretrained(output_dir)
            torch.save(
                self.classifier.state_dict(),
                os.path.join(output_dir, HEAD_WEIGHTS_FILENAME),
            )
            with open(os.path.join(output_dir, HEAD_CONFIG_FILENAME), "w") as handle:
                json.dump(
                    {
                        "image_dim": self.image_dim,
                        "text_dim": self.text_dim,
                        "num_labels": self.num_labels,
                        "dropout": dropout,
                    },
                    handle,
                    indent=2,
                )

    return ImageTextClassifier()


def load(model_dir, id2label):
    """Rebuild a saved fusion model. The exact inverse of ``model.save``."""
    import torch
    from transformers import AutoModel

    with open(os.path.join(model_dir, HEAD_CONFIG_FILENAME)) as handle:
        head_config = json.load(handle)

    backbone = AutoModel.from_pretrained(model_dir)
    model = build_fusion_model(
        backbone,
        image_dim=head_config["image_dim"],
        text_dim=head_config["text_dim"],
        num_labels=head_config["num_labels"],
        dropout=head_config.get("dropout", 0.1),
    )
    model.classifier.load_state_dict(
        torch.load(
            os.path.join(model_dir, HEAD_WEIGHTS_FILENAME), map_location="cpu"
        )
    )
    model.id2label = id2label
    return model


def resolve_projection_dims(config):
    """Determine the pooled image and text embedding widths for a dual encoder.

    CLIP-family configs expose a shared ``projection_dim``; others fall back to
    the per-tower hidden sizes.  Getting this wrong only surfaces as a shape
    error deep inside the first forward pass, so it is resolved up front.
    """
    projection_dim = getattr(config, "projection_dim", None)
    if projection_dim:
        return int(projection_dim), int(projection_dim)

    vision_config = getattr(config, "vision_config", None)
    text_config = getattr(config, "text_config", None)
    image_dim = getattr(vision_config, "hidden_size", None)
    text_dim = getattr(text_config, "hidden_size", None)

    if not image_dim or not text_dim:
        raise ValueError(
            "Could not determine image/text embedding widths from the model "
            "config. This task needs a dual-encoder checkpoint that exposes "
            "get_image_features and get_text_features (CLIP, SigLIP, ALIGN)."
        )
    return int(image_dim), int(text_dim)


# =========================================================================
# Collation
# =========================================================================

def build_collator(processor, spec, context_length, include_labels=True):
    """Return a collate_fn producing pixel_values, input_ids and labels."""
    import torch

    try:
        from . import task_image_classification
    except ImportError:  # pragma: no cover - flat sourcedir
        import task_image_classification  # type: ignore

    def collate(features):
        images = [
            task_image_classification.load_image(feature[spec.image_column])
            for feature in features
        ]
        texts = [str(feature[spec.text_column]) for feature in features]

        batch = processor(
            images=images,
            text=texts,
            return_tensors="pt",
            padding="max_length",
            truncation=True,
            max_length=context_length,
        )
        batch = {
            key: value
            for key, value in batch.items()
            if key in ("pixel_values", "input_ids", "attention_mask")
        }

        if include_labels and spec.label_column in features[0]:
            batch["labels"] = torch.tensor(
                [feature[spec.label_column] for feature in features], dtype=torch.long
            )
        return batch

    return collate


# =========================================================================
# Training
# =========================================================================

def train(args, spec):
    """Fine-tune a dual encoder plus fusion head on image/text pairs."""
    from transformers import AutoConfig, AutoModel, AutoProcessor, Trainer

    train_channel = os.environ.get("SM_CHANNEL_TRAIN", "/opt/ml/input/data/train")
    model_dir = os.environ.get("SM_MODEL_DIR", "/opt/ml/model")

    frame, _ = task_common.prepare_dataset(train_channel, spec)
    frame, label2id, id2label = task_common.build_label_mapping(frame, spec)
    num_labels = len(label2id)

    processor = AutoProcessor.from_pretrained(args.model_name_or_path)
    config = AutoConfig.from_pretrained(args.model_name_or_path)
    backbone = AutoModel.from_pretrained(args.model_name_or_path, torch_dtype="auto")

    if not (hasattr(backbone, "get_image_features") and hasattr(backbone, "get_text_features")):
        raise ValueError(
            f"{args.model_name_or_path} is not a dual-encoder model: it does "
            f"not expose get_image_features/get_text_features. Choose a "
            f"CLIP, SigLIP or ALIGN style checkpoint for "
            f"{spec.display_name.lower()}."
        )

    image_dim, text_dim = resolve_projection_dims(config)
    logger.info(f"Fusion head: image_dim={image_dim}, text_dim={text_dim}, classes={num_labels}")

    tokenizer = getattr(processor, "tokenizer", None)
    max_ctx = task_common.resolve_max_context_length(config, tokenizer)
    effective_context = (
        min(args.context_length, max_ctx) if max_ctx else args.context_length
    )
    logger.info(
        f"Context length: requested={args.context_length}, "
        f"effective={effective_context}"
        f"{' (capped)' if max_ctx and args.context_length > max_ctx else ''}"
    )

    model = build_fusion_model(backbone, image_dim, text_dim, num_labels)

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
        remove_unused_columns=False,
        label_names=["labels"],
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        compute_metrics=task_common.compute_accuracy,
        callbacks=task_common.build_callbacks(args),
        data_collator=build_collator(processor, spec, effective_context),
    )

    logger.info(
        f"Starting fine-tuning: task={spec.task_type}, "
        f"model={args.model_name_or_path}, epochs={args.epochs}, "
        f"batch_size={args.per_device_train_batch_size}"
    )
    trainer.train()

    metrics = trainer.evaluate()
    logger.info(f"Final evaluation: accuracy={metrics.get('eval_accuracy', 'N/A')}")

    # Trainer.save_model cannot round-trip a plain nn.Module, so save the
    # backbone, head and wiring explicitly.
    model.save(model_dir)
    processor.save_pretrained(model_dir)
    with open(os.path.join(model_dir, "label_mapping.json"), "w") as handle:
        json.dump({"label2id": label2id, "id2label": id2label}, handle, indent=2)
    logger.info(f"Saved model to {model_dir}")

    return metrics


# =========================================================================
# Inference
# =========================================================================

def model_fn(model_dir):
    """Load the fusion model, processor and label mapping."""
    import torch
    from transformers import AutoProcessor

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    with open(os.path.join(model_dir, "label_mapping.json")) as handle:
        mapping = json.load(handle)
    id2label = {int(k): v for k, v in mapping["id2label"].items()}

    processor = AutoProcessor.from_pretrained(model_dir)
    model = load(model_dir, id2label)
    model.to(device)
    model.eval()

    logger.info(f"Loaded image+text classification model from {model_dir} on {device}")
    return {
        "model": model,
        "processor": processor,
        "device": device,
        "id2label": id2label,
    }


def input_fn(request_body, content_type, spec):
    """Unpack a .zip holding a manifest plus images into records.

    The manifest needs the task's ``image`` and ``text`` columns; ``label`` is
    not required, since inference input is unlabelled.
    """
    import tempfile

    if not isinstance(request_body, (bytes, bytearray)):
        raise ValueError(
            f"Expected archive bytes for {spec.task_type}, got {type(request_body).__name__}"
        )

    work_dir = tempfile.mkdtemp(prefix="inference-image-text-")
    archive_path = os.path.join(work_dir, "input.zip")
    with open(archive_path, "wb") as handle:
        handle.write(bytes(request_body))

    root = task_common.extract_archive(archive_path, os.path.join(work_dir, "extracted"))
    manifest_path = task_common.find_file_in_dir(root, spec.manifest_extensions, "manifest")

    import pandas as pd

    reader_name, reader_kwargs = task_common.resolve_dataset_reader(manifest_path)
    frame = getattr(pd, reader_name)(manifest_path, **reader_kwargs)

    missing = [c for c in (spec.image_column, spec.text_column) if c not in frame.columns]
    if missing:
        raise ValueError(
            f"Inference manifest is missing required column(s): {', '.join(missing)}."
        )

    records = []
    for _index, row in frame.iterrows():
        relative = str(row[spec.image_column]).strip()
        records.append(
            {
                spec.image_column: task_common.resolve_image_path(root, relative),
                spec.text_column: str(row[spec.text_column]),
                "identifier": relative,
            }
        )

    if not records:
        raise ValueError("Inference manifest contains no records.")

    logger.info(f"Unpacked {len(records)} image/text pairs for inference")
    return records


def predict_fn(records, loaded, spec):
    """Run batched image+text inference with softmax probabilities."""
    import numpy as np
    import torch

    model, processor, device = loaded["model"], loaded["processor"], loaded["device"]
    id2label = loaded["id2label"]

    if not records:
        return {"identifiers": [], "probabilities": np.zeros((0, 0)), "labels": []}

    tokenizer = getattr(processor, "tokenizer", None)
    context_length = getattr(tokenizer, "model_max_length", 77) or 77
    collate = build_collator(processor, spec, context_length, include_labels=False)

    batches = []
    with torch.no_grad():
        for start in range(0, len(records), BATCH_SIZE):
            batch = collate(records[start : start + BATCH_SIZE])
            batch = {k: v.to(device) for k, v in batch.items()}
            logits = model(**batch)["logits"]
            batches.append(torch.softmax(logits, dim=-1).cpu().numpy())

    probabilities = np.vstack(batches) if batches else np.zeros((0, 0))
    num_labels = probabilities.shape[1] if len(probabilities.shape) > 1 else 0

    return {
        "identifiers": [record["identifier"] for record in records],
        "probabilities": probabilities,
        "labels": [id2label.get(i, f"class_{i}") for i in range(num_labels)],
    }
