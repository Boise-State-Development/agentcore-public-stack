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
        model_id="meta-llama-3-8b",
        model_name="Meta Llama 3 8B",
        huggingface_model_id="meta-llama/Meta-Llama-3-8B",
        description="8B parameter model from Meta, good for general fine-tuning tasks",
        default_instance_type="ml.g5.2xlarge",
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
        model_id="mistral-7b-v0.3",
        model_name="Mistral 7B v0.3",
        huggingface_model_id="mistralai/Mistral-7B-v0.3",
        description="7B parameter model from Mistral AI, strong reasoning and instruction following",
        default_instance_type="ml.g5.2xlarge",
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
        model_id="phi-3-mini-4k",
        model_name="Phi-3 Mini 4K",
        huggingface_model_id="microsoft/Phi-3-mini-4k-instruct",
        description="3.8B parameter model from Microsoft, efficient for smaller fine-tuning tasks",
        default_instance_type="ml.g5.xlarge",
        default_hyperparameters={
            "epochs": "3",
            "per_device_train_batch_size": "8",
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
