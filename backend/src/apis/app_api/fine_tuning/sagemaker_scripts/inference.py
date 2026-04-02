"""SageMaker Inference Toolkit handler for Batch Transform.

Implements the four handler functions required by the HuggingFace inference DLC:
  - model_fn(model_dir):  Load model and tokenizer
  - input_fn(body, type):  Parse input text
  - predict_fn(data, model):  Run batched inference with softmax
  - output_fn(prediction, accept):  Format as CSV with probability columns

SageMaker Batch Transform:
  - Extracts model.tar.gz to a directory, finds code/inference.py
  - Calls model_fn once to load the model
  - For each input record: input_fn -> predict_fn -> output_fn
"""

import json
import logging

# Heavy ML dependencies are imported lazily inside functions since they are only
# available in the SageMaker DLC container.  input_fn, output_fn, and
# _sanitize_label must remain importable without torch/transformers so they
# can be unit-tested locally.

logger = logging.getLogger(__name__)

BATCH_SIZE = 64


def model_fn(model_dir):
    """Load model and tokenizer from the model directory.

    Returns a tuple of (model, tokenizer, device).
    """
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

    logger.info(f"Loaded model from {model_dir} on {device}")
    return (model, tokenizer, device)


def input_fn(request_body, content_type="text/plain"):
    """Parse input data.

    Supports:
      - text/plain: one text string per line
      - application/json: list of strings or {"texts": [...]}

    Returns a list of non-empty text strings.
    """
    # SageMaker HuggingFace DLC passes request_body as bytes, not str.
    if isinstance(request_body, (bytes, bytearray)):
        request_body = request_body.decode("utf-8")

    if content_type == "text/plain":
        lines = request_body.strip().split("\n")
        texts = [line.strip() for line in lines if line.strip()]
        return texts
    elif content_type == "application/json":
        data = json.loads(request_body)
        if isinstance(data, list):
            return [str(item) for item in data if str(item).strip()]
        elif isinstance(data, dict) and "texts" in data:
            return [str(t) for t in data["texts"] if str(t).strip()]
        raise ValueError('JSON input must be a list or {"texts": [...]}')
    else:
        raise ValueError(f"Unsupported content type: {content_type}")


def predict_fn(input_data, model_tuple):
    """Run batched inference with softmax probabilities.

    Args:
        input_data: List of text strings from input_fn
        model_tuple: (model, tokenizer, device) from model_fn

    Returns a dict with 'texts', 'probabilities' (numpy array), and 'labels'.
    """
    import torch
    import numpy as np

    model, tokenizer, device = model_tuple
    texts = input_data

    if not texts:
        return {"texts": [], "probabilities": np.zeros((0, 0)), "labels": []}

    # Batched inference
    all_probs = []
    with torch.no_grad():
        for start in range(0, len(texts), BATCH_SIZE):
            batch_texts = texts[start : start + BATCH_SIZE]
            enc = tokenizer(
                batch_texts,
                padding=True,
                truncation=True,
                return_tensors="pt",
            )
            enc = {k: v.to(device) for k, v in enc.items()}
            outputs = model(**enc)
            probs = torch.softmax(outputs.logits, dim=-1).cpu().numpy()
            all_probs.append(probs)

    probabilities = np.vstack(all_probs) if all_probs else np.zeros((0, 0))

    # Build label names from model config
    num_labels = (
        probabilities.shape[1] if len(probabilities.shape) > 1 else 0
    )
    id2label = getattr(model.config, "id2label", None)
    if isinstance(id2label, dict):
        labels = [
            id2label.get(i) or id2label.get(str(i)) or f"class_{i}"
            for i in range(num_labels)
        ]
    elif isinstance(id2label, (list, tuple)):
        labels = list(id2label)[:num_labels]
    else:
        labels = [f"class_{i}" for i in range(num_labels)]

    return {"texts": texts, "probabilities": probabilities, "labels": labels}


def _sanitize_label(label):
    """Sanitize a label string for use as a CSV column name."""
    if label is None:
        return "class"
    return "".join(
        c if (c.isalnum() or c == "_") else "_" for c in str(label)
    )


def output_fn(prediction, accept="text/csv"):
    """Format prediction output as CSV with probability columns.

    Output format:
        text,prob_label1,prob_label2,...
        "example text",0.850000,0.150000
    """
    texts = prediction["texts"]
    probs = prediction["probabilities"]
    labels = prediction["labels"]

    # Build CSV header
    prob_columns = [f"prob_{_sanitize_label(l)}" for l in labels]
    header = "text," + ",".join(prob_columns)

    # Build rows
    rows = [header]
    for i, text in enumerate(texts):
        # Escape text for CSV (handle commas and quotes)
        escaped_text = '"' + text.replace('"', '""') + '"'
        if probs.shape[0] > i and probs.shape[1] > 0:
            prob_values = ",".join(
                f"{probs[i, j]:.6f}" for j in range(probs.shape[1])
            )
        else:
            prob_values = ",".join("0.000000" for _ in labels)
        rows.append(f"{escaped_text},{prob_values}")

    return "\n".join(rows)
