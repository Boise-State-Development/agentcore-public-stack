"""Pydantic models for SageMaker Batch Transform inference jobs."""

from pydantic import BaseModel, Field
from typing import List, Optional


# =========================================================================
# Request / Response Models
# =========================================================================

class CreateInferenceJobRequest(BaseModel):
    """Request to create a new inference (Batch Transform) job."""
    training_job_id: str
    input_s3_key: str
    instance_type: Optional[str] = None
    max_runtime_seconds: int = Field(default=3600, le=86400, gt=0)


class InferenceJobResponse(BaseModel):
    """Full inference job record for API responses."""
    job_id: str
    user_id: str
    email: str
    job_type: str = "inference"
    training_job_id: str
    model_name: str
    model_s3_path: str
    status: str
    input_s3_key: str
    output_s3_prefix: Optional[str] = None
    result_s3_key: Optional[str] = None
    instance_type: str
    transform_job_name: Optional[str] = None
    transform_start_time: Optional[str] = None
    transform_end_time: Optional[str] = None
    billable_seconds: Optional[int] = None
    estimated_cost_usd: Optional[float] = None
    created_at: str
    updated_at: str
    error_message: Optional[str] = None
    max_runtime_seconds: int = 3600


class InferenceJobListResponse(BaseModel):
    """Response for listing inference jobs."""
    jobs: List[InferenceJobResponse]
    total_count: int


class TrainedModelResponse(BaseModel):
    """Summary of a completed training job for model selection."""
    training_job_id: str
    model_id: str
    model_name: str
    model_s3_path: str
    instance_type: str
    completed_at: Optional[str] = None
    estimated_cost_usd: Optional[float] = None
