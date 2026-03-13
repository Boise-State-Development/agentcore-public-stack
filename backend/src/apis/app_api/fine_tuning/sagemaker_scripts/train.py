"""SageMaker training entry point for fine-tuning text classification models.

Adapted from the original Flask fine_tune.py for SageMaker HuggingFace DLC.

SageMaker paths:
  - Input data:   /opt/ml/input/data/train/  (CSV with text,label columns)
  - Model output: /opt/ml/model/             (auto-uploaded to S3 as model.tar.gz)
  - Checkpoints:  /opt/ml/checkpoints/       (not used)

Usage:
  The HuggingFace DLC invokes this script with hyperparameters as CLI args:
    python train.py --model_name_or_path bert-base-uncased --epochs 3 ...
"""

import argparse
import os
import sys
import shutil
import logging

# Heavy ML dependencies are imported lazily inside train() since they are only
# available in the SageMaker DLC container.  Utility functions and callbacks
# must remain importable without torch/transformers so they can be unit-tested
# locally.
try:
    from transformers import TrainerCallback
except ImportError:  # pragma: no cover – local dev/test without transformers
    TrainerCallback = object

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)


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
            except Exception as e:
                logger.warning(f"Could not create DynamoDB client: {e}")

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


# =========================================================================
# Helper Functions
# =========================================================================


def resolve_max_context_length(config, tokenizer):
    """Resolve the effective maximum context length from model config.

    Checks multiple config attributes and returns the smallest valid value.
    Returns None if no valid context length can be determined.
    """
    candidates = [
        getattr(config, "max_position_embeddings", None),
        getattr(config, "n_positions", None),
        getattr(config, "seq_length", None),
        getattr(tokenizer, "model_max_length", None),
    ]

    def _valid(v):
        try:
            return v is not None and float(v) > 0 and float(v) < 1_000_000
        except Exception:
            return False

    valid_vals = [int(v) for v in candidates if _valid(v)]
    return min(valid_vals) if valid_vals else None


def find_csv_in_channel(channel_dir):
    """Find the first CSV file in a SageMaker input channel directory.

    Raises FileNotFoundError if no CSV file is found.
    """
    if not os.path.isdir(channel_dir):
        raise FileNotFoundError(f"Channel directory does not exist: {channel_dir}")

    for f in sorted(os.listdir(channel_dir)):
        if f.lower().endswith(".csv"):
            return os.path.join(channel_dir, f)

    raise FileNotFoundError(f"No CSV file found in {channel_dir}")


def copy_inference_script(model_output_dir):
    """Copy inference.py and requirements.txt into model_output_dir/code/.

    SageMaker Batch Transform discovers code/inference.py in model.tar.gz
    and uses it as the custom inference handler.
    """
    code_dir = os.path.join(model_output_dir, "code")
    os.makedirs(code_dir, exist_ok=True)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    for filename in ("inference.py", "requirements.txt"):
        src = os.path.join(script_dir, filename)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(code_dir, filename))
            logger.info(f"Copied {filename} to {code_dir}")
        else:
            logger.warning(f"Script file not found, skipping: {src}")


# =========================================================================
# Main Training Function
# =========================================================================


def train(args):
    """Main training logic adapted from the original fine_tune.py."""
    import numpy as np
    import pandas as pd
    from transformers import (
        AutoTokenizer,
        AutoConfig,
        AutoModelForSequenceClassification,
        Trainer,
        TrainingArguments,
    )
    from datasets import Dataset
    import evaluate

    # SageMaker paths
    train_channel = os.environ.get(
        "SM_CHANNEL_TRAIN", "/opt/ml/input/data/train"
    )
    model_dir = os.environ.get("SM_MODEL_DIR", "/opt/ml/model")

    # Find and load CSV dataset
    csv_path = find_csv_in_channel(train_channel)
    logger.info(f"Loading dataset from {csv_path}")
    df = pd.read_csv(csv_path)

    # Label normalization — support non-numeric class labels
    label_names = sorted(list(pd.Series(df["label"]).astype(str).unique()))
    label2id = {name: i for i, name in enumerate(label_names)}
    id2label = {i: name for name, i in label2id.items()}
    df["label"] = df["label"].astype(str).map(label2id)
    num_labels = len(label_names)
    logger.info(f"Label mapping ({num_labels} classes): {label2id}")

    # Load tokenizer and add PAD token
    tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path)
    tokenizer.add_special_tokens({"pad_token": "[PAD]"})
    pad_token_id = tokenizer(
        "[PAD]", truncation=True, padding=False, return_tensors="pt"
    )["input_ids"][0][0].item()

    # Load model config with label mappings
    config = AutoConfig.from_pretrained(
        args.model_name_or_path,
        num_labels=num_labels,
        label2id=label2id,
        id2label=id2label,
        pad_token_id=pad_token_id,
    )

    # Resolve effective context length
    max_ctx = resolve_max_context_length(config, tokenizer)
    effective_context = (
        min(args.context_length, max_ctx) if max_ctx else args.context_length
    )
    logger.info(
        f"Context length: requested={args.context_length}, "
        f"effective={effective_context}"
        f"{ ' (capped)' if max_ctx and args.context_length > max_ctx else ''}"
    )

    # Load model
    model = AutoModelForSequenceClassification.from_pretrained(
        args.model_name_or_path,
        config=config,
        torch_dtype="auto",
    )
    model.resize_token_embeddings(len(tokenizer))

    # Tokenization
    def tokenize_function(examples):
        return tokenizer(
            examples["text"],
            max_length=effective_context,
            padding="max_length",
            truncation=True,
        )

    # Train/test split
    dataset = Dataset.from_pandas(df)
    dataset = dataset.train_test_split(
        test_size=1 - args.split_ratio, seed=args.seed
    )
    logger.info(
        f"Data split: {len(dataset['train'])} train / "
        f"{len(dataset['test'])} test"
    )

    tokenized_datasets = dataset.map(tokenize_function, batched=True)
    train_dataset = tokenized_datasets["train"].shuffle(seed=args.seed)
    eval_dataset = tokenized_datasets["test"].shuffle(seed=args.seed)

    # Training arguments
    training_args = TrainingArguments(
        output_dir="/opt/ml/checkpoints",
        learning_rate=args.learning_rate,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.per_device_train_batch_size,
        weight_decay=args.weight_decay,
        eval_strategy="epoch",
        save_strategy="no",
        logging_dir="/opt/ml/output/tensorboard",
    )

    # Accuracy metric
    metric = evaluate.load("accuracy")

    def compute_metrics(eval_pred):
        logits, labels = eval_pred
        predictions = np.argmax(logits, axis=-1)
        return metric.compute(predictions=predictions, references=labels)

    # Callbacks
    callbacks = [SageMakerLoggingCallback()]
    progress_cb = DynamoDBProgressCallback(
        table_name=args.dynamodb_table_name,
        region=args.dynamodb_region,
        pk=args.job_pk,
        sk=args.job_sk,
    )
    callbacks.append(progress_cb)

    # Train
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        compute_metrics=compute_metrics,
        callbacks=callbacks,
    )

    logger.info(
        f"Starting fine-tuning: model={args.model_name_or_path}, "
        f"epochs={args.epochs}, batch_size={args.per_device_train_batch_size}"
    )
    trainer.train()

    # Final evaluation
    metrics = trainer.evaluate()
    logger.info(
        f"Final evaluation: accuracy={metrics.get('eval_accuracy', 'N/A')}"
    )

    # Save model and tokenizer to /opt/ml/model/
    trainer.save_model(model_dir)
    tokenizer.save_pretrained(model_dir)
    logger.info(f"Saved model to {model_dir}")

    # Copy inference handler into model artifact for Batch Transform
    copy_inference_script(model_dir)

    logger.info("Training complete.")


# =========================================================================
# Argument Parsing
# =========================================================================


def parse_args():
    """Parse command-line arguments (passed as hyperparameters by SageMaker)."""
    parser = argparse.ArgumentParser()

    # Model
    parser.add_argument("--model_name_or_path", type=str, required=True)

    # Training hyperparameters
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--per_device_train_batch_size", type=int, default=4)
    parser.add_argument("--learning_rate", type=float, default=2e-5)
    parser.add_argument("--weight_decay", type=float, default=0.01)
    parser.add_argument("--split_ratio", type=float, default=0.8)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--context_length", type=int, default=512)

    # DynamoDB progress reporting
    parser.add_argument("--dynamodb_table_name", type=str, default="")
    parser.add_argument("--dynamodb_region", type=str, default="us-west-2")
    parser.add_argument("--job_pk", type=str, default="")
    parser.add_argument("--job_sk", type=str, default="")

    # SageMaker environment (passed automatically, ignored by our script)
    parser.add_argument(
        "--model_dir",
        type=str,
        default=os.environ.get("SM_MODEL_DIR", "/opt/ml/model"),
    )

    args, _ = parser.parse_known_args()
    return args


if __name__ == "__main__":
    args = parse_args()
    train(args)
