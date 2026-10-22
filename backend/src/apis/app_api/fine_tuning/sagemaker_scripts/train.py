"""SageMaker training entry point.

A dispatcher.  It parses the hyperparameters SageMaker passes as CLI args,
resolves the task type against the registry, and hands off to the matching
``task_*`` module.  All model-specific behaviour lives in those modules; this
file deliberately knows nothing about auto-classes or collators.

SageMaker paths:
  - Input data:   /opt/ml/input/data/train/   (dataset file, or .zip archive)
  - Model output: /opt/ml/model/              (auto-uploaded as model.tar.gz)
  - Checkpoints:  /opt/ml/checkpoints/

The HuggingFace DLC invokes this script with hyperparameters as CLI args:
    python train.py --model_name_or_path google/vit-base-patch16-224 \
        --task_type image-classification --epochs 3 ...
"""

import argparse
import json
import logging
import os
import sys

try:  # package context: unit tests and the app-api container
    from .. import task_types
    from . import task_common
    from . import task_image_classification
    from . import task_image_text_classification
    from . import task_image_text_to_text
    from . import task_text_classification
except ImportError:  # pragma: no cover - flat sourcedir inside the SageMaker DLC
    import task_types  # type: ignore
    import task_common  # type: ignore
    import task_image_classification  # type: ignore
    import task_image_text_classification  # type: ignore
    import task_image_text_to_text  # type: ignore
    import task_text_classification  # type: ignore

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)


# Maps a task type to the module implementing it.  Adding a task means adding
# a module and one entry here — no branching inside the trainer.
TASK_MODULES = {
    task_types.TEXT_CLASSIFICATION: task_text_classification,
    task_types.IMAGE_CLASSIFICATION: task_image_classification,
    task_types.IMAGE_TEXT_CLASSIFICATION: task_image_text_classification,
    task_types.IMAGE_TEXT_TO_TEXT: task_image_text_to_text,
}


#: Re-exported so the container script and app-api parse boolean
#: hyperparameters identically — a disagreement here would let app-api admit a
#: job the trainer then runs with different settings.
str2bool = task_types.str2bool


def resolve_task_module(task_type):
    """Return the module implementing ``task_type``.

    Raises ValueError for a task the registry knows but no module implements,
    which would otherwise surface as a confusing KeyError mid-training.
    """
    spec = task_types.get_task_spec(task_type)
    module = TASK_MODULES.get(spec.task_type)
    if module is None:  # pragma: no cover - registry/module drift
        raise ValueError(
            f"Task type '{spec.task_type}' is registered but has no trainer module."
        )
    return module, spec


def parse_args(argv=None):
    """Parse the hyperparameters SageMaker passes as command-line arguments."""
    parser = argparse.ArgumentParser()

    # Model and task
    parser.add_argument("--model_name_or_path", type=str, required=True)
    parser.add_argument(
        "--task_type",
        type=str,
        default=task_types.DEFAULT_TASK_TYPE,
        choices=list(task_types.TASK_TYPES),
    )

    # Training hyperparameters
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--per_device_train_batch_size", type=int, default=4)
    parser.add_argument("--learning_rate", type=float, default=2e-5)
    parser.add_argument("--weight_decay", type=float, default=0.01)
    parser.add_argument("--split_ratio", type=float, default=0.8)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--context_length", type=int, default=512)
    parser.add_argument("--image_size", type=int, default=224)
    parser.add_argument("--gradient_accumulation_steps", type=int, default=1)
    # Kill switch. Checkpointing is what makes a restart — a spot interruption,
    # or a job killed at the budget-clamped MaxRuntime — resume instead of
    # starting over, so it is on unless deliberately turned off.
    parser.add_argument("--checkpointing", type=str2bool, default=True)

    # Generative VLM (LoRA) hyperparameters.  Ignored by the classification
    # tasks, which train every weight of a much smaller model.
    parser.add_argument("--load_in_4bit", type=str2bool, default=True)
    parser.add_argument("--lora_r", type=int, default=16)
    parser.add_argument("--lora_alpha", type=int, default=32)
    parser.add_argument("--lora_dropout", type=float, default=0.05)
    parser.add_argument("--lora_target_modules", type=str, default="")
    parser.add_argument("--max_new_tokens", type=int, default=256)

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

    args, _unknown = parser.parse_known_args(argv)
    return args


def write_task_marker(model_dir, task_type):
    """Record the task type inside the model artifact for the inference handler."""
    os.makedirs(model_dir, exist_ok=True)
    marker = os.path.join(model_dir, "task_type.json")
    with open(marker, "w") as handle:
        json.dump({"task_type": task_type}, handle, indent=2)
    logger.info(f"Recorded task type '{task_type}' in {marker}")
    return marker


def main(argv=None):
    args = parse_args(argv)
    module, spec = resolve_task_module(args.task_type)

    logger.info(f"Dispatching to task '{spec.task_type}' ({spec.display_name})")
    module.train(args, spec)

    # Bundle the inference handler into the artifact so Batch Transform can
    # serve this model, and record which task it serves.  Without the marker
    # the handler would fall back to text classification and mis-parse an
    # image payload.
    model_dir = os.environ.get("SM_MODEL_DIR", "/opt/ml/model")
    write_task_marker(model_dir, spec.task_type)
    task_common.copy_inference_bundle(model_dir)

    logger.info("Training complete.")


if __name__ == "__main__":
    main()
