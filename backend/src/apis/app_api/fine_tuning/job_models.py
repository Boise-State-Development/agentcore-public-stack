"""Pydantic models and the base-model catalog for fine-tuning training jobs."""

from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from . import task_types


# =========================================================================
# Model Catalog
# =========================================================================

class AvailableModel(BaseModel):
    """A base model available for fine-tuning."""
    model_id: str
    model_name: str
    huggingface_model_id: str
    description: str
    #: Which task this checkpoint can be fine-tuned for.  A model is only
    #: offered for its own task: a ViT cannot classify text and a BERT cannot
    #: classify images, and letting the two mix produces a job that fails
    #: several billed minutes into a GPU run.
    task_type: str
    default_instance_type: str
    default_hyperparameters: Dict[str, str]


def _hyperparameters(task_type: str, **overrides: str) -> Dict[str, str]:
    """Task defaults with per-model overrides applied.

    Keeps the catalog readable: a model entry states only what makes it
    different, instead of repeating seven identical keys.
    """
    spec = task_types.get_task_spec(task_type)
    return {**spec.default_hyperparameters, **overrides}


_TEXT = task_types.TEXT_CLASSIFICATION
_IMAGE = task_types.IMAGE_CLASSIFICATION
_IMAGE_TEXT = task_types.IMAGE_TEXT_CLASSIFICATION


AVAILABLE_MODELS: List[AvailableModel] = [
    # ---------------------------------------------------------------
    # Text classification
    # ---------------------------------------------------------------
    AvailableModel(
        model_id="bert-base-uncased",
        model_name="BERT Base Uncased",
        huggingface_model_id="bert-base-uncased",
        description="110M parameter masked language model from Google, widely used baseline for NLP tasks",
        task_type=_TEXT,
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters=_hyperparameters(_TEXT),
    ),
    AvailableModel(
        model_id="roberta-base",
        model_name="RoBERTa Base",
        huggingface_model_id="roberta-base",
        description="125M parameter robustly optimized BERT from Meta, strong on classification and NLU",
        task_type=_TEXT,
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters=_hyperparameters(_TEXT),
    ),
    AvailableModel(
        model_id="electra-base",
        model_name="ELECTRA",
        huggingface_model_id="google/electra-base-discriminator",
        description="110M parameter discriminative model from Google, efficient pre-training with replaced token detection",
        task_type=_TEXT,
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters=_hyperparameters(_TEXT),
    ),
    AvailableModel(
        model_id="electra-tiny",
        model_name="ELECTRA Tiny",
        huggingface_model_id="bsu-slim/electra-tiny",
        description="Tiny ELECTRA variant, very fast training for prototyping and experimentation",
        task_type=_TEXT,
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters=_hyperparameters(_TEXT, per_device_train_batch_size="32"),
    ),
    AvailableModel(
        model_id="electra-tiny-mm",
        model_name="ELECTRA Tiny Multimodal",
        huggingface_model_id="bsu-slim/electra-tiny-mm",
        description="Multimodal tiny ELECTRA variant for cross-modal experimentation",
        task_type=_TEXT,
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters=_hyperparameters(_TEXT, per_device_train_batch_size="32"),
    ),
    AvailableModel(
        model_id="childes-bert",
        model_name="BERT ChildES",
        huggingface_model_id="smeylan/childes-bert",
        description="BERT model pre-trained on child-directed speech, suited for developmental language research",
        task_type=_TEXT,
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters=_hyperparameters(_TEXT),
    ),
    AvailableModel(
        model_id="distilgpt2",
        model_name="Distilled GPT2",
        huggingface_model_id="distilbert/distilgpt2",
        description="82M parameter distilled GPT-2, lightweight causal language model for fast iteration",
        task_type=_TEXT,
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters=_hyperparameters(_TEXT),
    ),
    AvailableModel(
        model_id="childgpt",
        model_name="ChildGPT",
        huggingface_model_id="Aunsiels/ChildGPT",
        description="GPT model trained on child language data for developmental linguistics research",
        task_type=_TEXT,
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters=_hyperparameters(_TEXT),
    ),
    AvailableModel(
        model_id="gpt2-medium",
        model_name="GPT2 Medium",
        huggingface_model_id="openai-community/gpt2-medium",
        description="355M parameter GPT-2 medium from OpenAI, good balance of capability and efficiency",
        task_type=_TEXT,
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters=_hyperparameters(
            _TEXT, per_device_train_batch_size="8", learning_rate="2e-5"
        ),
    ),
    AvailableModel(
        model_id="eurollm-1.7b-instruct",
        model_name="EuroLLM 1.7B Instruct",
        huggingface_model_id="utter-project/EuroLLM-1.7B-Instruct",
        description="1.7B parameter multilingual European LLM with instruction tuning",
        task_type=_TEXT,
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters=_hyperparameters(
            _TEXT, per_device_train_batch_size="4", learning_rate="2e-5"
        ),
    ),
    AvailableModel(
        model_id="smollm2-135m-instruct",
        model_name="SmolLM2 135M Instruct",
        huggingface_model_id="HuggingFaceTB/SmolLM2-135M-Instruct",
        description="135M parameter instruction-tuned model from HuggingFace, ultra-lightweight for fast experiments",
        task_type=_TEXT,
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters=_hyperparameters(_TEXT, per_device_train_batch_size="32"),
    ),
    # ---------------------------------------------------------------
    # Image classification
    # ---------------------------------------------------------------
    AvailableModel(
        model_id="vit-base",
        model_name="ViT Base",
        huggingface_model_id="google/vit-base-patch16-224",
        description="86M parameter Vision Transformer from Google, the standard baseline for image classification",
        task_type=_IMAGE,
        default_instance_type="ml.g6.xlarge",
        default_hyperparameters=_hyperparameters(_IMAGE),
    ),
    AvailableModel(
        model_id="resnet-50",
        model_name="ResNet-50",
        huggingface_model_id="microsoft/resnet-50",
        description="25M parameter convolutional network, fast to train and a strong classical baseline",
        task_type=_IMAGE,
        default_instance_type="ml.g6.xlarge",
        default_hyperparameters=_hyperparameters(_IMAGE, per_device_train_batch_size="32"),
    ),
    AvailableModel(
        model_id="convnext-tiny",
        model_name="ConvNeXt Tiny",
        huggingface_model_id="facebook/convnext-tiny-224",
        description="29M parameter modernised convolutional network from Meta, competitive with transformers at low cost",
        task_type=_IMAGE,
        default_instance_type="ml.g6.xlarge",
        default_hyperparameters=_hyperparameters(_IMAGE, per_device_train_batch_size="32"),
    ),
    AvailableModel(
        model_id="swin-tiny",
        model_name="Swin Tiny",
        huggingface_model_id="microsoft/swin-tiny-patch4-window7-224",
        description="28M parameter hierarchical vision transformer from Microsoft, strong on fine-grained detail",
        task_type=_IMAGE,
        default_instance_type="ml.g6.xlarge",
        default_hyperparameters=_hyperparameters(_IMAGE),
    ),
    # ---------------------------------------------------------------
    # Image + text classification
    #
    # These must be dual encoders exposing get_image_features and
    # get_text_features — the fusion head reads both towers.
    # ---------------------------------------------------------------
    AvailableModel(
        model_id="clip-vit-base",
        model_name="CLIP ViT-B/32",
        huggingface_model_id="openai/clip-vit-base-patch32",
        description="151M parameter image/text dual encoder from OpenAI, the standard baseline for cross-modal tasks",
        task_type=_IMAGE_TEXT,
        default_instance_type="ml.g6.xlarge",
        default_hyperparameters=_hyperparameters(_IMAGE_TEXT),
    ),
    AvailableModel(
        model_id="clip-vit-large",
        model_name="CLIP ViT-L/14",
        huggingface_model_id="openai/clip-vit-large-patch14",
        description="428M parameter CLIP from OpenAI, higher accuracy at meaningfully higher training cost",
        task_type=_IMAGE_TEXT,
        default_instance_type="ml.g6.xlarge",
        default_hyperparameters=_hyperparameters(_IMAGE_TEXT, per_device_train_batch_size="8"),
    ),
    AvailableModel(
        model_id="siglip-base",
        model_name="SigLIP Base",
        huggingface_model_id="google/siglip-base-patch16-224",
        description="203M parameter dual encoder from Google using a sigmoid loss, stronger than CLIP at equal size",
        task_type=_IMAGE_TEXT,
        default_instance_type="ml.g6.xlarge",
        default_hyperparameters=_hyperparameters(_IMAGE_TEXT, context_length="64"),
    ),
    AvailableModel(
        model_id="clip-laion-b32",
        model_name="CLIP ViT-B/32 (LAION-2B)",
        huggingface_model_id="laion/CLIP-ViT-B-32-laion2B-s34B-b79K",
        description="Open-data CLIP trained on LAION-2B, a reproducible alternative to the OpenAI weights",
        task_type=_IMAGE_TEXT,
        default_instance_type="ml.g6.xlarge",
        default_hyperparameters=_hyperparameters(_IMAGE_TEXT),
    ),
]

MODEL_CATALOG: Dict[str, AvailableModel] = {m.model_id: m for m in AVAILABLE_MODELS}


def models_for_task(task_type: Optional[str]) -> List[AvailableModel]:
    """Catalog entries trainable for ``task_type``, in catalog order."""
    resolved = task_types.get_task_spec(task_type).task_type
    return [m for m in AVAILABLE_MODELS if m.task_type == resolved]


# =========================================================================
# Request / Response Models
# =========================================================================

class PresignRequest(BaseModel):
    """Request for a presigned upload URL for a training dataset."""
    filename: str
    content_type: str
    task_type: str = task_types.DEFAULT_TASK_TYPE


class PresignResponse(BaseModel):
    """Response with presigned URL for dataset upload."""
    presigned_url: str
    s3_key: str
    expires_at: str


class CreateJobRequest(BaseModel):
    """Request to create a new fine-tuning training job."""
    model_id: str
    dataset_s3_key: str
    task_type: str = task_types.DEFAULT_TASK_TYPE
    instance_type: Optional[str] = None
    hyperparameters: Optional[Dict[str, str]] = None
    max_runtime_seconds: int = Field(default=86400, le=432000, gt=0)
    custom_huggingface_model_id: Optional[str] = None


class JobResponse(BaseModel):
    """Full job record for API responses."""
    job_id: str
    user_id: str
    email: str
    model_id: str
    model_name: str
    task_type: str = task_types.DEFAULT_TASK_TYPE
    status: str
    dataset_s3_key: str
    output_s3_prefix: Optional[str] = None
    instance_type: str
    instance_count: int = 1
    hyperparameters: Optional[Dict[str, str]] = None
    sagemaker_job_name: Optional[str] = None
    training_start_time: Optional[str] = None
    training_end_time: Optional[str] = None
    billable_seconds: Optional[int] = None
    estimated_cost_usd: Optional[float] = None
    created_at: str
    updated_at: str
    error_message: Optional[str] = None
    max_runtime_seconds: int = 86400
    training_progress: Optional[float] = None


class JobListResponse(BaseModel):
    """Response for listing training jobs."""
    jobs: List[JobResponse]
    total_count: int


class TaskTypeResponse(BaseModel):
    """A task type offered by the platform, for the create-job UI."""
    task_type: str
    display_name: str
    description: str
    required_columns: List[str]
    upload_extensions: List[str]
    requires_archive: bool
    inference_upload_extensions: List[str]
    default_instance_type: str
