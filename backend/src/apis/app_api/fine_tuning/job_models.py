"""Pydantic models, model catalog, and cost map for fine-tuning training jobs."""

from pydantic import BaseModel, Field
from typing import Dict, List, Optional


# =========================================================================
# Model Catalog
# =========================================================================

class AvailableModel(BaseModel):
    """A base model available for fine-tuning."""
    model_id: str
    model_name: str
    huggingface_model_id: str
    description: str
    default_instance_type: str
    default_hyperparameters: Dict[str, str]


AVAILABLE_MODELS: List[AvailableModel] = [
    AvailableModel(
        model_id="bert-base-uncased",
        model_name="BERT Base Uncased",
        huggingface_model_id="bert-base-uncased",
        description="110M parameter masked language model from Google, widely used baseline for NLP tasks",
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters={
            "epochs": "3",
            "per_device_train_batch_size": "16",
            "learning_rate": "5e-5",
            "weight_decay": "0.01",
            "split_ratio": "0.8",
            "seed": "42",
            "context_length": "512",
        },
    ),
    AvailableModel(
        model_id="roberta-base",
        model_name="RoBERTa Base",
        huggingface_model_id="roberta-base",
        description="125M parameter robustly optimized BERT from Meta, strong on classification and NLU",
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters={
            "epochs": "3",
            "per_device_train_batch_size": "16",
            "learning_rate": "5e-5",
            "weight_decay": "0.01",
            "split_ratio": "0.8",
            "seed": "42",
            "context_length": "512",
        },
    ),
    AvailableModel(
        model_id="electra-base",
        model_name="ELECTRA",
        huggingface_model_id="google/electra-base-discriminator",
        description="110M parameter discriminative model from Google, efficient pre-training with replaced token detection",
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters={
            "epochs": "3",
            "per_device_train_batch_size": "16",
            "learning_rate": "5e-5",
            "weight_decay": "0.01",
            "split_ratio": "0.8",
            "seed": "42",
            "context_length": "512",
        },
    ),
    AvailableModel(
        model_id="electra-tiny",
        model_name="ELECTRA Tiny",
        huggingface_model_id="bsu-slim/electra-tiny",
        description="Tiny ELECTRA variant, very fast training for prototyping and experimentation",
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters={
            "epochs": "3",
            "per_device_train_batch_size": "32",
            "learning_rate": "5e-5",
            "weight_decay": "0.01",
            "split_ratio": "0.8",
            "seed": "42",
            "context_length": "512",
        },
    ),
    AvailableModel(
        model_id="electra-tiny-mm",
        model_name="ELECTRA Tiny Multimodal",
        huggingface_model_id="bsu-slim/electra-tiny-mm",
        description="Multimodal tiny ELECTRA variant for cross-modal experimentation",
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters={
            "epochs": "3",
            "per_device_train_batch_size": "32",
            "learning_rate": "5e-5",
            "weight_decay": "0.01",
            "split_ratio": "0.8",
            "seed": "42",
            "context_length": "512",
        },
    ),
    AvailableModel(
        model_id="childes-bert",
        model_name="BERT ChildES",
        huggingface_model_id="smeylan/childes-bert",
        description="BERT model pre-trained on child-directed speech, suited for developmental language research",
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters={
            "epochs": "3",
            "per_device_train_batch_size": "16",
            "learning_rate": "5e-5",
            "weight_decay": "0.01",
            "split_ratio": "0.8",
            "seed": "42",
            "context_length": "512",
        },
    ),
    AvailableModel(
        model_id="distilgpt2",
        model_name="Distilled GPT2",
        huggingface_model_id="distilbert/distilgpt2",
        description="82M parameter distilled GPT-2, lightweight causal language model for fast iteration",
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters={
            "epochs": "3",
            "per_device_train_batch_size": "16",
            "learning_rate": "5e-5",
            "weight_decay": "0.01",
            "split_ratio": "0.8",
            "seed": "42",
            "context_length": "512",
        },
    ),
    AvailableModel(
        model_id="childgpt",
        model_name="ChildGPT",
        huggingface_model_id="Aunsiels/ChildGPT",
        description="GPT model trained on child language data for developmental linguistics research",
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters={
            "epochs": "3",
            "per_device_train_batch_size": "16",
            "learning_rate": "5e-5",
            "weight_decay": "0.01",
            "split_ratio": "0.8",
            "seed": "42",
            "context_length": "512",
        },
    ),
    AvailableModel(
        model_id="gpt2-medium",
        model_name="GPT2 Medium",
        huggingface_model_id="openai-community/gpt2-medium",
        description="355M parameter GPT-2 medium from OpenAI, good balance of capability and efficiency",
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters={
            "epochs": "3",
            "per_device_train_batch_size": "8",
            "learning_rate": "2e-5",
            "weight_decay": "0.01",
            "split_ratio": "0.8",
            "seed": "42",
            "context_length": "512",
        },
    ),
    AvailableModel(
        model_id="eurollm-1.7b-instruct",
        model_name="EuroLLM 1.7B Instruct",
        huggingface_model_id="utter-project/EuroLLM-1.7B-Instruct",
        description="1.7B parameter multilingual European LLM with instruction tuning",
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters={
            "epochs": "3",
            "per_device_train_batch_size": "4",
            "learning_rate": "2e-5",
            "weight_decay": "0.01",
            "split_ratio": "0.8",
            "seed": "42",
            "context_length": "512",
        },
    ),
    AvailableModel(
        model_id="smollm2-135m-instruct",
        model_name="SmolLM2 135M Instruct",
        huggingface_model_id="HuggingFaceTB/SmolLM2-135M-Instruct",
        description="135M parameter instruction-tuned model from HuggingFace, ultra-lightweight for fast experiments",
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters={
            "epochs": "3",
            "per_device_train_batch_size": "32",
            "learning_rate": "5e-5",
            "weight_decay": "0.01",
            "split_ratio": "0.8",
            "seed": "42",
            "context_length": "512",
        },
    ),
]

MODEL_CATALOG: Dict[str, AvailableModel] = {m.model_id: m for m in AVAILABLE_MODELS}


# =========================================================================
# Instance Cost Map (on-demand USD/hour, us-west-2 pricing)
# =========================================================================

INSTANCE_COST_PER_HOUR: Dict[str, float] = {
    "ml.g5.xlarge": 1.41,
    "ml.g5.2xlarge": 1.515,
    "ml.g5.4xlarge": 2.03,
    "ml.g5.8xlarge": 3.06,
    "ml.g5.12xlarge": 7.09,
    "ml.g5.16xlarge": 6.10,
    "ml.g5.24xlarge": 10.18,
    "ml.g5.48xlarge": 20.36,
    "ml.p3.2xlarge": 3.825,
    "ml.p3.8xlarge": 14.688,
    "ml.p3.16xlarge": 28.152,
}


# =========================================================================
# Request / Response Models
# =========================================================================

class PresignRequest(BaseModel):
    """Request for a presigned upload URL for a training dataset."""
    filename: str
    content_type: str


class PresignResponse(BaseModel):
    """Response with presigned URL for dataset upload."""
    presigned_url: str
    s3_key: str
    expires_at: str


class CreateJobRequest(BaseModel):
    """Request to create a new fine-tuning training job."""
    model_id: str
    dataset_s3_key: str
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
