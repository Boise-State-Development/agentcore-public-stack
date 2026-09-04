"""Text classification: text -> label.

The original fine-tuning path, moved here unchanged in behaviour.  Every model
trained before task types existed was trained by this code, so its handling of
the pad token and of ``resize_token_embeddings`` is preserved verbatim: the
decoder-only models in the catalog (GPT-2, SmolLM2, EuroLLM) ship without a
pad token and genuinely need one added before they can be batched.

That same manoeuvre is actively harmful on a vision-language model, which is
one of the reasons the tasks are separate modules rather than branches.
"""

import logging
import os

try:  # package context: unit tests and the app-api container
    from . import task_common
except ImportError:  # pragma: no cover - flat sourcedir inside the SageMaker DLC
    import task_common  # type: ignore

logger = logging.getLogger(__name__)

BATCH_SIZE = 64


# =========================================================================
# Training
# =========================================================================

def train(args, spec):
    """Fine-tune a sequence classification model on text records."""
    from transformers import (
        AutoTokenizer,
        AutoConfig,
        AutoModelForSequenceClassification,
        Trainer,
    )

    train_channel = os.environ.get("SM_CHANNEL_TRAIN", "/opt/ml/input/data/train")
    model_dir = os.environ.get("SM_MODEL_DIR", "/opt/ml/model")

    frame, _ = task_common.prepare_dataset(train_channel, spec)
    frame, label2id, id2label = task_common.build_label_mapping(frame, spec)
    num_labels = len(label2id)

    # Load tokenizer and add a PAD token.  Decoder-only checkpoints have none,
    # and padding is required to batch.
    tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path)
    tokenizer.add_special_tokens({"pad_token": "[PAD]"})
    pad_token_id = tokenizer(
        "[PAD]", truncation=True, padding=False, return_tensors="pt"
    )["input_ids"][0][0].item()

    config = AutoConfig.from_pretrained(
        args.model_name_or_path,
        num_labels=num_labels,
        label2id=label2id,
        id2label=id2label,
        pad_token_id=pad_token_id,
    )

    max_ctx = task_common.resolve_max_context_length(config, tokenizer)
    effective_context = (
        min(args.context_length, max_ctx) if max_ctx else args.context_length
    )
    logger.info(
        f"Context length: requested={args.context_length}, "
        f"effective={effective_context}"
        f"{' (capped)' if max_ctx and args.context_length > max_ctx else ''}"
    )

    model = AutoModelForSequenceClassification.from_pretrained(
        args.model_name_or_path,
        config=config,
        torch_dtype="auto",
    )
    model.resize_token_embeddings(len(tokenizer))

    text_column = spec.text_column

    def tokenize_function(examples):
        return tokenizer(
            examples[text_column],
            max_length=effective_context,
            padding="max_length",
            truncation=True,
        )

    train_dataset, eval_dataset = task_common.split_frame(
        frame, args.split_ratio, args.seed
    )
    train_dataset = train_dataset.map(tokenize_function, batched=True)
    eval_dataset = eval_dataset.map(tokenize_function, batched=True)

    training_args = task_common.build_training_arguments(
        output_dir="/opt/ml/checkpoints",
        learning_rate=args.learning_rate,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.per_device_train_batch_size,
        weight_decay=args.weight_decay,
        eval_strategy="epoch",
        save_strategy="no",
        logging_dir="/opt/ml/output/tensorboard",
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        compute_metrics=task_common.compute_accuracy,
        callbacks=task_common.build_callbacks(args),
    )

    logger.info(
        f"Starting fine-tuning: task={spec.task_type}, "
        f"model={args.model_name_or_path}, epochs={args.epochs}, "
        f"batch_size={args.per_device_train_batch_size}"
    )
    trainer.train()

    metrics = trainer.evaluate()
    logger.info(f"Final evaluation: accuracy={metrics.get('eval_accuracy', 'N/A')}")

    trainer.save_model(model_dir)
    tokenizer.save_pretrained(model_dir)
    logger.info(f"Saved model to {model_dir}")

    return metrics


# =========================================================================
# Inference
# =========================================================================

def model_fn(model_dir):
    """Load the model and tokenizer for Batch Transform."""
    import torch
    from transformers import AutoTokenizer, AutoModelForSequenceClassification

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    tokenizer = AutoTokenizer.from_pretrained(model_dir)
    model = AutoModelForSequenceClassification.from_pretrained(
        model_dir,
        torch_dtype="auto",
    )
    model.resize_token_embeddings(len(tokenizer))
    model.to(device)
    model.eval()

    logger.info(f"Loaded text classification model from {model_dir} on {device}")
    return {"model": model, "tokenizer": tokenizer, "device": device}


def input_fn(request_body, content_type, spec):
    """Parse Batch Transform input into records.

    Supports text/plain (one text per line) and application/json (a list of
    strings or ``{"texts": [...]}``).
    """
    import json

    if isinstance(request_body, (bytes, bytearray)):
        request_body = request_body.decode("utf-8")

    if content_type in ("text/plain", "text/csv"):
        texts = [line.strip() for line in request_body.strip().split("\n") if line.strip()]
    elif content_type == "application/json":
        data = json.loads(request_body)
        if isinstance(data, list):
            texts = [str(item) for item in data if str(item).strip()]
        elif isinstance(data, dict) and "texts" in data:
            texts = [str(t) for t in data["texts"] if str(t).strip()]
        else:
            raise ValueError('JSON input must be a list or {"texts": [...]}')
    else:
        raise ValueError(f"Unsupported content type: {content_type}")

    return [{"text": text, "identifier": text} for text in texts]


def predict_fn(records, loaded, spec):
    """Run batched inference, returning identifiers, probabilities and labels."""
    import numpy as np
    import torch

    model, tokenizer, device = loaded["model"], loaded["tokenizer"], loaded["device"]
    texts = [record["text"] for record in records]

    if not texts:
        return {"identifiers": [], "probabilities": np.zeros((0, 0)), "labels": []}

    batches = []
    with torch.no_grad():
        for start in range(0, len(texts), BATCH_SIZE):
            encoded = tokenizer(
                texts[start : start + BATCH_SIZE],
                padding=True,
                truncation=True,
                return_tensors="pt",
            )
            encoded = {k: v.to(device) for k, v in encoded.items()}
            logits = model(**encoded).logits
            batches.append(torch.softmax(logits, dim=-1).cpu().numpy())

    probabilities = np.vstack(batches) if batches else np.zeros((0, 0))

    return {
        "identifiers": [record["identifier"] for record in records],
        "probabilities": probabilities,
        "labels": task_common.label_names(model.config, probabilities),
    }
