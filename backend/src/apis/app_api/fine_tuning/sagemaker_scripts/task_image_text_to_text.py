"""Generative vision-language fine-tuning: (image, prompt) -> free text.

The three classification tasks all end in a softmax over a fixed class list.
This one does not: the model keeps its language-modelling head and learns to
*write* the response, so the dataset has no label column and the result file
carries a text column instead of one probability column per class.

**Why LoRA and not a full fine-tune.**  A generative VLM worth using starts
around 7B parameters and the catalog goes to 34B.  A full fine-tune needs
roughly 16 bytes per parameter once gradients and the AdamW moments are
resident — ~550GB for a 34B model, against the 384GB on the largest instance
we offer.  Quantising the frozen base to 4-bit and training only low-rank
adapters brings the same model to ~18GB of weights, which fits one 48GB card
and leaves the activations room to breathe.  The trade is that the artifact is
an *adapter*, not a model: it is a few hundred MB rather than 69GB, and
inference reloads the base from the Hub and applies the adapter on top.

**Prompt masking.**  Loss is computed on the response only.  Training on the
prompt as well is the usual accident here: the model spends most of its
gradient budget learning to reproduce questions it will always be given.  The
prompt length is measured by rendering each record twice — once with the
generation prompt and no answer, once complete — and masking that many leading
positions.
"""

import json
import logging
import os

try:  # package context: unit tests and the app-api container
    from . import task_common
except ImportError:  # pragma: no cover - flat sourcedir inside the SageMaker DLC
    import task_common  # type: ignore

logger = logging.getLogger(__name__)

#: Written next to the adapter so inference can rebuild base + adapter without
#: being told which base was used.
ADAPTER_CONFIG_FILENAME = "vlm_adapter.json"

#: Ignored index for positions that must not contribute to the loss.
LABEL_IGNORE_INDEX = -100

def prompt_mask_limit(prompt_length, sequence_length):
    """How many leading positions of a row to exclude from the loss.

    Truncation can cut a long prompt so short that nothing of the response
    survives.  Masking the whole row makes its loss NaN, which propagates
    through the batch average and destroys the step — so the last position is
    always left scoreable and such an example contributes almost nothing
    instead of poisoning the batch.
    """
    return max(0, min(int(prompt_length), int(sequence_length) - 1))


#: Generation runs one record at a time.  Batched generation needs left
#: padding and a per-architecture agreement about where image placeholders may
#: sit relative to the pad run; getting it subtly wrong produces fluent
#: garbage rather than an error, which is the worst failure mode for a result
#: file a researcher will treat as data.
GENERATION_BATCH_SIZE = 1


# =========================================================================
# Chat rendering
# =========================================================================

def build_messages(prompt, response=None):
    """Return chat messages for one record.

    ``response=None`` yields the prompt half only, which is what both the
    generation path and the prompt-length measurement need.
    """
    messages = [
        {
            "role": "user",
            "content": [{"type": "image"}, {"type": "text", "text": str(prompt)}],
        }
    ]
    if response is not None:
        messages.append(
            {
                "role": "assistant",
                "content": [{"type": "text", "text": str(response)}],
            }
        )
    return messages


def render_chat(processor, messages, add_generation_prompt):
    """Render messages to a string using the processor's chat template.

    Every model in the catalog ships a chat template.  A checkpoint without
    one would otherwise be rendered with an invented format that does not
    match its pre-training, which trains the model to answer in a dialogue
    shape it has never seen — so this raises instead of guessing.
    """
    template = getattr(processor, "chat_template", None) or getattr(
        getattr(processor, "tokenizer", None), "chat_template", None
    )
    if not template:
        raise ValueError(
            "This checkpoint ships no chat template, so there is no faithful "
            "way to format an image/prompt/response turn for it. Choose a "
            "instruction-tuned vision-language model (the catalog entries all "
            "carry one)."
        )
    return processor.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=add_generation_prompt
    )


# =========================================================================
# Model loading
# =========================================================================

def build_quantization_config(load_in_4bit):
    """Return a BitsAndBytesConfig for 4-bit NF4, or None to load in bf16."""
    if not load_in_4bit:
        return None

    import torch
    from transformers import BitsAndBytesConfig

    return BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=torch.bfloat16,
    )


def load_base_model(model_name_or_path, load_in_4bit):
    """Load a vision-language model for training or inference.

    ``device_map="auto"`` shards across whatever GPUs the instance has, which
    is what lets a 34B adapter run on a multi-GPU instance without any
    distributed launcher.
    """
    import torch
    from transformers import AutoModelForImageTextToText

    quantization_config = build_quantization_config(load_in_4bit)
    kwargs = {
        "torch_dtype": torch.bfloat16,
        "device_map": "auto",
    }
    if quantization_config is not None:
        kwargs["quantization_config"] = quantization_config

    logger.info(
        f"Loading {model_name_or_path} "
        f"({'4-bit NF4' if load_in_4bit else 'bfloat16'})"
    )
    return AutoModelForImageTextToText.from_pretrained(model_name_or_path, **kwargs)


def resolve_image_token_ids(processor, model_config):
    """Best-effort set of token ids standing in for image patches.

    These positions carry no text the model should be scored on, so they are
    masked out of the labels.  The attribute moved between transformers
    versions and differs by architecture, so every known spelling is tried and
    an empty set is an acceptable answer: the pad and prompt masking below
    still apply, and a handful of unmasked placeholder positions degrades the
    loss slightly rather than breaking it.
    """
    ids = set()

    for attribute in ("image_token_index", "image_token_id"):
        value = getattr(model_config, attribute, None)
        if isinstance(value, int):
            ids.add(value)

    tokenizer = getattr(processor, "tokenizer", None)
    token = getattr(processor, "image_token", None)
    if tokenizer is not None and isinstance(token, str) and token:
        try:
            resolved = tokenizer.convert_tokens_to_ids(token)
            if isinstance(resolved, int) and resolved >= 0:
                ids.add(resolved)
        except Exception:  # pragma: no cover - tokenizer without the token
            pass

    return ids


# =========================================================================
# Collation
# =========================================================================

def build_collator(processor, spec, context_length, image_token_ids=()):
    """Return a collate_fn producing model inputs with response-only labels."""
    import torch

    try:
        from . import task_image_classification
    except ImportError:  # pragma: no cover - flat sourcedir
        import task_image_classification  # type: ignore

    image_token_ids = set(image_token_ids)
    tokenizer = getattr(processor, "tokenizer", None)
    pad_token_id = getattr(tokenizer, "pad_token_id", None)

    def _process(images, texts):
        return processor(
            images=images,
            text=texts,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=context_length,
        )

    def collate(features):
        images = [
            task_image_classification.load_image(feature[spec.image_column])
            for feature in features
        ]
        prompts = [feature[spec.text_column] for feature in features]
        responses = [feature[spec.response_column] for feature in features]

        full_texts = [
            render_chat(processor, build_messages(p, r), add_generation_prompt=False)
            for p, r in zip(prompts, responses)
        ]
        batch = _process(images, full_texts)

        # Measure the prompt half in the same tokenisation the full render
        # used, so the count is a true prefix length rather than an estimate.
        prompt_texts = [
            render_chat(processor, build_messages(p), add_generation_prompt=True)
            for p in prompts
        ]
        prompt_batch = _process(images, prompt_texts)
        prompt_lengths = prompt_batch["attention_mask"].sum(dim=1).tolist()

        labels = batch["input_ids"].clone()
        if pad_token_id is not None:
            labels[labels == pad_token_id] = LABEL_IGNORE_INDEX
        if "attention_mask" in batch:
            labels[batch["attention_mask"] == 0] = LABEL_IGNORE_INDEX
        for token_id in image_token_ids:
            labels[labels == token_id] = LABEL_IGNORE_INDEX

        for row, prompt_length in enumerate(prompt_lengths):
            limit = prompt_mask_limit(prompt_length, labels.shape[1])
            if limit > 0:
                labels[row, :limit] = LABEL_IGNORE_INDEX

        batch["labels"] = labels
        return batch

    return collate


# =========================================================================
# Training
# =========================================================================

def train(args, spec):
    """LoRA fine-tune a generative vision-language model."""
    import torch
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import AutoProcessor, Trainer

    train_channel = os.environ.get("SM_CHANNEL_TRAIN", "/opt/ml/input/data/train")
    model_dir = os.environ.get("SM_MODEL_DIR", "/opt/ml/model")

    frame, _ = task_common.prepare_dataset(train_channel, spec)

    processor = AutoProcessor.from_pretrained(args.model_name_or_path)
    tokenizer = getattr(processor, "tokenizer", None)
    if tokenizer is not None and tokenizer.pad_token_id is None:
        # Several VLM decoders ship no pad token.  Padding with EOS is safe
        # because every padded position is masked out of both the attention
        # mask and the labels.
        tokenizer.pad_token = tokenizer.eos_token
    if tokenizer is not None:
        tokenizer.padding_side = "right"

    model = load_base_model(args.model_name_or_path, args.load_in_4bit)

    max_ctx = task_common.resolve_max_context_length(model.config, tokenizer)
    measured = measure_required_context(processor, spec, frame.to_dict("records"))
    effective_context, raised = resolve_effective_context(
        args.context_length, measured, max_ctx
    )
    logger.info(
        f"Context length: requested={args.context_length}, "
        f"measured={measured}, effective={effective_context}"
    )
    if raised:
        logger.warning(
            f"Raised context length from {args.context_length} to "
            f"{effective_context}: this model spends {measured} tokens on a "
            f"record, mostly on the image. Truncating to the requested length "
            f"would have cut into the image tokens and failed the job."
        )
    if measured and max_ctx and measured > max_ctx:
        raise ValueError(
            f"A single record needs {measured} tokens but "
            f"{args.model_name_or_path} supports at most {max_ctx}. The image "
            f"alone does not fit. Use smaller images, or a model that tiles "
            f"less aggressively."
        )

    if args.load_in_4bit:
        model = prepare_model_for_kbit_training(
            model, use_gradient_checkpointing=True
        )
    # Gradient checkpointing recomputes activations instead of storing them,
    # which is what makes a multi-thousand-token image sequence fit at all.
    # With a frozen, quantised base no input tensor requires grad, so the
    # recomputation graph would be empty without this.
    model.gradient_checkpointing_enable()
    if hasattr(model, "enable_input_require_grads"):
        model.enable_input_require_grads()

    # peft accepts either the "all-linear" sentinel or an explicit list; a
    # comma-separated hyperparameter is the only way to spell a list through
    # SageMaker, which passes every value as a string.
    raw_targets = (args.lora_target_modules or "").strip()
    if not raw_targets:
        target_modules = "all-linear"
    elif "," in raw_targets:
        target_modules = [t.strip() for t in raw_targets.split(",") if t.strip()]
    else:
        target_modules = raw_targets
    lora_config = LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=target_modules,
    )
    model = get_peft_model(model, lora_config)
    trainable, total = model.get_nb_trainable_parameters()
    logger.info(
        f"LoRA: r={args.lora_r}, alpha={args.lora_alpha}, "
        f"targets={target_modules}, trainable={trainable:,}/{total:,} "
        f"({100 * trainable / total:.3f}%)"
    )

    train_dataset, eval_dataset = task_common.split_frame(
        frame, args.split_ratio, args.seed
    )

    image_token_ids = resolve_image_token_ids(processor, model.config)
    logger.info(f"Masking image placeholder token ids: {sorted(image_token_ids)}")

    training_args = task_common.build_training_arguments(
        output_dir="/opt/ml/checkpoints",
        learning_rate=args.learning_rate,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.per_device_train_batch_size,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        weight_decay=args.weight_decay,
        eval_strategy="epoch",
        save_strategy="no",
        logging_dir="/opt/ml/output/tensorboard",
        remove_unused_columns=False,
        label_names=["labels"],
        bf16=torch.cuda.is_available(),
        gradient_checkpointing=True,
        # Paged optimiser states survive the memory spikes a long image
        # sequence causes; it is a bitsandbytes optimiser, so it is only
        # available on the quantised path.
        optim="paged_adamw_8bit" if args.load_in_4bit else "adamw_torch",
    )

    collator = build_collator(processor, spec, effective_context, image_token_ids)
    check_collation(collator, train_dataset)

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        callbacks=task_common.build_callbacks(args),
        data_collator=collator,
    )

    logger.info(
        f"Starting fine-tuning: task={spec.task_type}, "
        f"model={args.model_name_or_path}, epochs={args.epochs}, "
        f"batch_size={args.per_device_train_batch_size} x "
        f"{args.gradient_accumulation_steps} accumulation"
    )
    trainer.train()

    metrics = trainer.evaluate()
    logger.info(f"Final evaluation: loss={metrics.get('eval_loss', 'N/A')}")

    # Only the adapter is saved.  The base stays on the Hub and is named in
    # the sidecar so model_fn can rebuild the pair.
    model.save_pretrained(model_dir)
    processor.save_pretrained(model_dir)
    save_adapter_config(model_dir, args)
    logger.info(f"Saved LoRA adapter to {model_dir}")

    return metrics


#: Tokens left for the prompt and response after the image is accounted for,
#: when a context length has to be raised to fit. Generous on purpose: the
#: cost of overshooting is a little wasted padding, the cost of undershooting
#: is a failed job on a billed GPU.
TEXT_TOKEN_HEADROOM = 256


def measure_required_context(processor, spec, dataset, sample_size=4):
    """Longest untruncated rendering across a sample of records.

    A vision-language model spends most of its sequence on the image, and how
    much is not knowable in advance: it depends on the checkpoint's tiling
    strategy *and* on the resolution of the images the user uploaded.
    SmolVLM-Instruct spends 1377 tokens on a single image; LLaVA-1.6's AnyRes
    tiling can spend more than twice that.

    That is why a fixed default context length cannot be right for every
    model, and why guessing one and letting truncation cut into the image
    placeholder run is a job that fails minutes into a billed GPU.  Measuring
    is cheap — a handful of CPU-side processor calls — so measure.

    Returns None when the sample cannot be processed at all; the caller keeps
    the requested length and lets :func:`check_collation` produce the error.
    """
    try:
        from . import task_image_classification
    except ImportError:  # pragma: no cover - flat sourcedir
        import task_image_classification  # type: ignore

    longest = 0
    for index in range(min(sample_size, len(dataset))):
        record = dataset[index]
        try:
            image = task_image_classification.load_image(record[spec.image_column])
            text = render_chat(
                processor,
                build_messages(record[spec.text_column], record[spec.response_column]),
                add_generation_prompt=False,
            )
            # No truncation: the point is to find out how long it really is.
            batch = processor(images=[image], text=[text], return_tensors="pt")
            longest = max(longest, int(batch["input_ids"].shape[1]))
        except Exception as error:  # pragma: no cover - defer to check_collation
            logger.warning(f"Could not measure record {index}: {error}")
            return None

    return longest or None


def resolve_effective_context(requested, measured, model_max):
    """Context length to actually use.

    Raises the requested length to fit the measured records, then clamps to
    what the model supports.  Returns ``(effective, raised)``.
    """
    effective = int(requested)
    raised = False

    if measured and measured > effective:
        effective = measured + TEXT_TOKEN_HEADROOM
        raised = True

    if model_max:
        effective = min(effective, int(model_max))

    return effective, raised


def check_collation(collator, dataset, sample_size=2):
    """Collate a couple of records before the Trainer starts.

    The expensive failure here is a processor/template mismatch — most often
    truncation cutting into the run of image placeholder tokens, which makes
    the count of placeholders disagree with the number of image features and
    fails inside the first forward pass.  Without this the job has already
    downloaded tens of GB of weights and provisioned a GPU before finding out.

    Raising early costs one CPU-side batch and turns a mid-run stack trace
    into a message naming the fix.
    """
    if len(dataset) == 0:  # pragma: no cover - prepare_dataset rejects this
        return

    sample = [dataset[i] for i in range(min(sample_size, len(dataset)))]
    try:
        collator(sample)
    except Exception as error:
        raise ValueError(
            f"Could not build a training batch from this dataset and model: "
            f"{error}. The usual cause is a context length too short for the "
            f"model's image tokens — a high-resolution VLM can spend a "
            f"thousand or more tokens on the image alone, before your prompt. "
            f"Raise context_length, or choose a model that tiles less "
            f"aggressively."
        ) from error
    logger.info(f"Collation check passed on {len(sample)} record(s)")


def save_adapter_config(model_dir, args):
    """Record what the adapter was trained against, for the inference side."""
    os.makedirs(model_dir, exist_ok=True)
    payload = {
        "base_model_id": args.model_name_or_path,
        "load_in_4bit": bool(args.load_in_4bit),
        "max_new_tokens": int(args.max_new_tokens),
    }
    path = os.path.join(model_dir, ADAPTER_CONFIG_FILENAME)
    with open(path, "w") as handle:
        json.dump(payload, handle, indent=2)
    return path


# =========================================================================
# Inference
# =========================================================================

def model_fn(model_dir):
    """Rebuild base + adapter. The inverse of what ``train`` saved."""
    import torch
    from peft import PeftModel
    from transformers import AutoProcessor

    with open(os.path.join(model_dir, ADAPTER_CONFIG_FILENAME)) as handle:
        adapter_config = json.load(handle)

    processor = AutoProcessor.from_pretrained(model_dir)
    tokenizer = getattr(processor, "tokenizer", None)
    if tokenizer is not None:
        if tokenizer.pad_token_id is None:
            tokenizer.pad_token = tokenizer.eos_token
        # Generation continues from the right-hand edge, so any padding has to
        # sit on the left or the model continues from pad tokens.
        tokenizer.padding_side = "left"

    base = load_base_model(
        adapter_config["base_model_id"], adapter_config.get("load_in_4bit", True)
    )
    model = PeftModel.from_pretrained(base, model_dir)
    model.eval()

    device = next(model.parameters()).device
    logger.info(
        f"Loaded LoRA adapter from {model_dir} over "
        f"{adapter_config['base_model_id']} on {device}"
    )
    return {
        "model": model,
        "processor": processor,
        "device": device,
        "max_new_tokens": adapter_config.get("max_new_tokens", 256),
    }


def input_fn(request_body, content_type, spec):
    """Unpack a .zip holding a manifest plus images into records.

    The manifest needs ``image`` and ``prompt``; ``response`` is the training
    target and is not required at inference.
    """
    import tempfile

    if not isinstance(request_body, (bytes, bytearray)):
        raise ValueError(
            f"Expected archive bytes for {spec.task_type}, got "
            f"{type(request_body).__name__}"
        )

    work_dir = tempfile.mkdtemp(prefix="inference-vlm-")
    archive_path = os.path.join(work_dir, "input.zip")
    with open(archive_path, "wb") as handle:
        handle.write(bytes(request_body))

    root = task_common.extract_archive(archive_path, os.path.join(work_dir, "extracted"))
    manifest_path = task_common.find_file_in_dir(
        root, spec.manifest_extensions, "manifest"
    )

    import pandas as pd

    reader_name, reader_kwargs = task_common.resolve_dataset_reader(manifest_path)
    frame = getattr(pd, reader_name)(manifest_path, **reader_kwargs)

    missing = [
        c for c in (spec.image_column, spec.text_column) if c not in frame.columns
    ]
    if missing:
        raise ValueError(
            f"Inference manifest is missing required column(s): "
            f"{', '.join(missing)}."
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

    logger.info(f"Unpacked {len(records)} image/prompt pairs for inference")
    return records


def predict_fn(records, loaded, spec):
    """Generate a response for each record."""
    import torch

    try:
        from . import task_image_classification
    except ImportError:  # pragma: no cover - flat sourcedir
        import task_image_classification  # type: ignore

    model, processor = loaded["model"], loaded["processor"]
    device, max_new_tokens = loaded["device"], loaded["max_new_tokens"]

    if not records:
        return {"identifiers": [], "prompts": [], "generations": []}

    generations = []
    with torch.no_grad():
        for start in range(0, len(records), GENERATION_BATCH_SIZE):
            chunk = records[start : start + GENERATION_BATCH_SIZE]
            images = [
                task_image_classification.load_image(record[spec.image_column])
                for record in chunk
            ]
            texts = [
                render_chat(
                    processor,
                    build_messages(record[spec.text_column]),
                    add_generation_prompt=True,
                )
                for record in chunk
            ]
            batch = processor(
                images=images, text=texts, return_tensors="pt", padding=True
            ).to(device)

            output_ids = model.generate(
                **batch, max_new_tokens=max_new_tokens, do_sample=False
            )
            # generate() returns the prompt followed by the continuation, so
            # slice the prompt off rather than decoding it back to the user.
            prompt_length = batch["input_ids"].shape[1]
            for row in output_ids:
                generations.append(
                    processor.decode(
                        row[prompt_length:], skip_special_tokens=True
                    ).strip()
                )

    logger.info(f"Generated {len(generations)} responses")
    return {
        "identifiers": [record["identifier"] for record in records],
        "prompts": [record[spec.text_column] for record in records],
        "generations": generations,
    }
