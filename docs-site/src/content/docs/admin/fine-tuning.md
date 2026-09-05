---
title: Fine-Tuning
description: Task types, datasets, quota and cost for the fine-tuning feature.
sidebar:
  order: 4
---

Researchers fine-tune a pre-trained model on their own labelled data, then run
the result over new data as a batch job. Everything executes on SageMaker; the
platform owns the catalog, the dataset contract, access control and the budget.

## Task types

A **task type** is the organizing primitive. It determines the dataset
contract, the accepted upload formats, which base models are offered, and which
Deep Learning Container the job runs in.

| Task | Input → output | Dataset upload | Manifest columns |
|---|---|---|---|
| `text-classification` | text → label | `.csv` / `.jsonl` / `.json` | `text`, `label` |
| `image-classification` | image → label | `.zip` | `image`, `label` |
| `image-text-classification` | image + text → label | `.zip` | `image`, `text`, `label` |

All three produce the same output: a softmax over the dataset's own classes,
written as a CSV with one probability column per class. That shared contract is
deliberate — it means a new modality never breaks the result viewer.

### Image datasets

An image task takes a single `.zip` containing a manifest plus the image files
it references:

```
dataset.zip
├── manifest.csv        image,label
│                       images/cat-01.jpg,cat
│                       images/dog-04.jpg,dog
└── images/
    ├── cat-01.jpg
    └── dog-04.jpg
```

The `image` column holds each file's path **relative to the archive root**. The
manifest may be `.csv`, `.jsonl` or `.json`. Archives are treated as untrusted
input: entries that use absolute paths or `../` traversal are refused, as are
manifest rows pointing outside the archive.

Batch Transform receives the archive as one payload, so inference input is
capped at 100 MB per job. Larger inference runs should be split across jobs.

## Adding a task type

The registry lives in `backend/src/apis/app_api/fine_tuning/task_types.py`.
Adding a task means registering a `TaskSpec` and adding a
`sagemaker_scripts/task_<name>.py` module implementing `train`, `model_fn`,
`input_fn` and `predict_fn`. `train.py` and `inference.py` are dispatchers and
should not need editing.

`task_types.py` must stay importable without torch, transformers, pandas or
PIL — it is read by the app-api container and the unit tests, neither of which
has the ML stack.

## Deep Learning Containers

Each task declares a DLC *family*, and the two families move independently:

| Family | Training image | Why |
|---|---|---|
| `text` | PyTorch 2.1 / transformers 4.36 | Every existing text model was trained and validated here. Bumping it would re-baseline all of them at once. |
| `vision` | PyTorch 2.8 / transformers 4.56 | Modern vision checkpoints do not load on 4.36. |

If a tag is retired or lags in a region, override it without a code deploy:

```bash
FINE_TUNING_TRAINING_IMAGE_VISION=<account>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag>
FINE_TUNING_INFERENCE_IMAGE_VISION=...
```

## Quota

Quota is denominated in **US dollars per calendar month**, not GPU-hours.
Hours stopped describing the budget the moment more than one instance type was
offered: ten hours buys about $14 on an `ml.g5.xlarge` and roughly $450 on an
`ml.g6e.24xlarge`.

Grants written before this change are migrated lazily — read in the new shape
and rewritten on the next quota check, converting at the `ml.g5.xlarge` rate
those hours were actually spent at. No backfill run is needed, and a migrated
user's existing spend carries across rather than resetting.

### How spend is bounded

A job's real cost is unknown until it stops, so the budget becomes its
**stopping condition**: `MaxRuntimeInSeconds` is clamped to the hours the
user's remaining quota affords. SageMaker kills the job at that point, which
bounds spend exactly with no reservation bookkeeping.

Rejecting on worst-case cost instead would block essentially everything — the
default 24-hour stopping condition is ~$27 even on the cheapest instance, more
than an ordinary monthly quota, though such a job usually finishes in minutes.
A job is refused outright only when the remaining budget buys less than 30
minutes.

Failed and stopped runs are billed by AWS and are charged against the quota
accordingly.

### Configuration

| Variable | Meaning |
|---|---|
| `FINE_TUNING_DEFAULT_QUOTA_USD` | Monthly quota auto-granted to any authenticated user. `0` (default) means whitelist-only. |
| `FINE_TUNING_DEFAULT_QUOTA_HOURS` | Legacy fallback, converted to dollars at the `ml.g5.xlarge` rate. |

## Instance pricing

Rates live in `fine_tuning/pricing.py` as literal maps, sourced from the AWS
Price List API (**not** the pricing web page, which rounds and lags). Resync
them with:

```bash
python backend/scripts/refresh_instance_pricing.py --profile dev-ai
```

Two things the map gets right that a single flat table could not:

- **Training and Batch Transform are priced separately.** They agree across the
  g5 family but diverge on g6e.
- **Not every training instance can run Batch Transform.** `ml.p4d.24xlarge`
  and `ml.p5.48xlarge` publish a training rate and no transform rate, so
  offering them would let a researcher fine-tune a model they then cannot run
  inference on. They are deliberately absent, as is `ml.p3.*`, which has no
  on-demand SageMaker rate at all.

Pricing accuracy is load-bearing: a wrong rate does not just misreport spend,
it mis-enforces the budget.

## Choosing a base model

The catalog offers vetted models per task. Researchers may also supply any
HuggingFace model id, which is pre-flighted against the Hub before a GPU is
provisioned. A custom model is refused when it does not exist, publishes no
loadable weights, or is tagged for a different modality.

The commonest refusal is a **GGUF-only repository**. GGUF is a llama.cpp
inference format and cannot be fine-tuned by transformers at all — look for the
original, unquantised repository instead.

Note that generative vision-language models (`image-text-to-text`, e.g. LLaVA
or Qwen-VL) are **not** supported. They emit tokens rather than a class
distribution, and would need a separate generative task type with LoRA/PEFT.

## Admin surface

Grant access and review spend under **Usage & Spend**, backed by
`admin/fine_tuning` and the `/admin/fine-tuning` pages. A grant is a monthly
dollar quota against an email address; the `AppRole` record is not involved.
