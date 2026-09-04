"""SageMaker Inference Toolkit handler for Batch Transform.

A dispatcher, mirroring ``train.py``.  It implements the four handler
functions the HuggingFace inference DLC calls, resolves the task type recorded
in the model artifact, and delegates the modality-specific work to the
matching ``task_*`` module.

  - model_fn(model_dir):            load model + processor for the task
  - input_fn(body, content_type):   parse the payload into records
  - predict_fn(records, model):     batched inference with softmax
  - output_fn(prediction, accept):  format as CSV with probability columns

The output shape is deliberately identical across every task — an identifier
column followed by one probability column per class — so a new modality never
breaks the result viewer.
"""

import json
import logging
import os

try:  # package context: unit tests and the app-api container
    from .. import task_types
    from . import task_image_classification
    from . import task_image_text_classification
    from . import task_text_classification
except ImportError:  # pragma: no cover - flat sourcedir inside the SageMaker DLC
    import task_types  # type: ignore
    import task_image_classification  # type: ignore
    import task_image_text_classification  # type: ignore
    import task_text_classification  # type: ignore

logger = logging.getLogger(__name__)

#: Written into the model artifact at training time so the handler can tell
#: which task it is serving without being told by the caller.
TASK_MARKER_FILENAME = "task_type.json"

TASK_MODULES = {
    task_types.TEXT_CLASSIFICATION: task_text_classification,
    task_types.IMAGE_CLASSIFICATION: task_image_classification,
    task_types.IMAGE_TEXT_CLASSIFICATION: task_image_text_classification,
}


def read_task_type(model_dir):
    """Read the task type recorded in a model artifact.

    Falls back to the default task for an artifact trained before task types
    existed — those directories have no marker and are all text classifiers.
    """
    marker = os.path.join(model_dir, TASK_MARKER_FILENAME)
    if not os.path.exists(marker):
        logger.info(
            f"No {TASK_MARKER_FILENAME} in artifact; assuming "
            f"'{task_types.DEFAULT_TASK_TYPE}'"
        )
        return task_types.DEFAULT_TASK_TYPE

    with open(marker) as handle:
        return json.load(handle).get("task_type", task_types.DEFAULT_TASK_TYPE)


def resolve_task_module(task_type):
    """Return the (module, spec) pair serving ``task_type``."""
    spec = task_types.get_task_spec(task_type)
    module = TASK_MODULES.get(spec.task_type)
    if module is None:  # pragma: no cover - registry/module drift
        raise ValueError(
            f"Task type '{spec.task_type}' is registered but has no inference module."
        )
    return module, spec


# =========================================================================
# SageMaker handler functions
# =========================================================================

def model_fn(model_dir):
    """Load the model for whichever task this artifact was trained for."""
    task_type = read_task_type(model_dir)
    module, spec = resolve_task_module(task_type)

    loaded = module.model_fn(model_dir)
    # Carry the task through to input_fn/predict_fn, which SageMaker calls
    # with only the payload and this object.
    loaded["task_type"] = spec.task_type
    loaded["spec"] = spec
    return loaded


def input_fn(request_body, content_type="text/plain", model=None):
    """Parse the Batch Transform payload into task-appropriate records.

    ``model`` is supplied by the SageMaker inference toolkit when the handler
    declares it.  When it is absent the payload is assumed to be text, which
    preserves the pre-task-types behaviour.
    """
    if model is None:
        module, spec = resolve_task_module(task_types.DEFAULT_TASK_TYPE)
    else:
        module, spec = resolve_task_module(model["task_type"])

    return module.input_fn(request_body, content_type, spec)


def predict_fn(input_data, model):
    """Run inference for the loaded task.

    Names the identifier column here rather than in each task module, so every
    task is guaranteed to produce a labelled first column.
    """
    module, spec = resolve_task_module(model["task_type"])
    prediction = module.predict_fn(input_data, model, spec)
    prediction.setdefault(
        "identifier_column", spec.image_column or spec.text_column or "id"
    )
    return prediction


def _sanitize_label(label):
    """Sanitize a label string for use as a CSV column name."""
    if label is None:
        return "class"
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in str(label))


def _escape_csv(value):
    """Quote and escape a value for CSV output."""
    return '"' + str(value).replace('"', '""') + '"'


def output_fn(prediction, accept="text/csv"):
    """Format predictions as CSV with one probability column per class.

    Output format:
        id,prob_label1,prob_label2,...
        "example.jpg",0.850000,0.150000

    The first column is whatever identifies a record for the task: the input
    text for text classification, the archive-relative image path for the
    image tasks.
    """
    identifiers = prediction["identifiers"]
    probabilities = prediction["probabilities"]
    labels = prediction["labels"]
    identifier_column = prediction.get("identifier_column", "id")

    header = identifier_column + "," + ",".join(
        f"prob_{_sanitize_label(label)}" for label in labels
    )

    rows = [header]
    for index, identifier in enumerate(identifiers):
        if probabilities.shape[0] > index and probabilities.shape[1] > 0:
            values = ",".join(
                f"{probabilities[index, column]:.6f}"
                for column in range(probabilities.shape[1])
            )
        else:
            values = ",".join("0.000000" for _ in labels)
        rows.append(f"{_escape_csv(identifier)},{values}")

    return "\n".join(rows)
