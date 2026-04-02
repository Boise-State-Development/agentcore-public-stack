"""User-facing routes for fine-tuning."""

import math
import os
import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status

from apis.shared.auth import User
from apis.shared.auth.dependencies import get_current_user
from .models import FineTuningAccessResponse
from .repository import (
    FineTuningAccessRepository,
    get_fine_tuning_access_repository,
)
from .job_models import (
    AVAILABLE_MODELS,
    MODEL_CATALOG,
    PresignRequest,
    PresignResponse,
    CreateJobRequest,
    JobResponse,
    JobListResponse,
)
from .job_repository import FineTuningJobsRepository, get_fine_tuning_jobs_repository
from .s3_service import FineTuningS3Service, get_fine_tuning_s3_service
from .sagemaker_service import SageMakerService, get_sagemaker_service
from .inference_models import (
    CreateInferenceJobRequest,
    InferenceJobResponse,
    InferenceJobListResponse,
    TrainedModelResponse,
)
from .inference_repository import InferenceRepository, get_inference_repository
from .script_packaging_service import ScriptPackagingService, get_script_packaging_service
from .dependencies import require_fine_tuning_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fine-tuning", tags=["fine-tuning"])


# =========================================================================
# Access Check (no fine-tuning access required)
# =========================================================================

@router.get("/access", response_model=FineTuningAccessResponse)
async def check_access(
    user: User = Depends(get_current_user),
    repo: FineTuningAccessRepository = Depends(get_fine_tuning_access_repository),
):
    """Check if the current user has fine-tuning access and return quota info.

    This endpoint does NOT require fine-tuning access — it is used by
    the frontend to decide whether to show the fine-tuning UI.
    """
    from .dependencies import DEFAULT_MONTHLY_QUOTA_HOURS

    grant = repo.check_and_reset_quota(user.email)

    if grant is not None:
        return FineTuningAccessResponse(
            has_access=True,
            monthly_quota_hours=grant["monthly_quota_hours"],
            current_month_usage_hours=grant["current_month_usage_hours"],
            quota_period=grant["quota_period"],
        )

    # No explicit grant — check if open-access mode is enabled.
    if DEFAULT_MONTHLY_QUOTA_HOURS > 0:
        return FineTuningAccessResponse(
            has_access=True,
            monthly_quota_hours=DEFAULT_MONTHLY_QUOTA_HOURS,
            current_month_usage_hours=0.0,
            quota_period=None,
        )

    return FineTuningAccessResponse(has_access=False)


# =========================================================================
# Model Catalog
# =========================================================================

@router.get("/models")
async def list_models(
    grant: dict = Depends(require_fine_tuning_access),
):
    """List available base models for fine-tuning."""
    return [m.model_dump() for m in AVAILABLE_MODELS]


# =========================================================================
# HuggingFace Model Search (proxy)
# =========================================================================

# Pipeline tags compatible with AutoModelForSequenceClassification
COMPATIBLE_PIPELINE_TAGS = [
    "fill-mask",
    "text-classification",
    "feature-extraction",
    "token-classification",
    "text-generation",
]


@router.get("/huggingface-models")
async def search_huggingface_models(
    search: str = Query(..., min_length=2, max_length=200),
    compatible_only: bool = Query(True),
    grant: dict = Depends(require_fine_tuning_access),
):
    """Search HuggingFace Hub models. Proxied to avoid CORS issues.

    When compatible_only=True (default), makes parallel requests for each
    compatible pipeline_tag and merges results sorted by downloads.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if compatible_only:
                # Query each compatible pipeline_tag in parallel
                import asyncio

                async def _fetch_tag(tag: str):
                    resp = await client.get(
                        "https://huggingface.co/api/models",
                        params={
                            "search": search,
                            "pipeline_tag": tag,
                            "library": "transformers",
                            "limit": 5,
                            "sort": "downloads",
                            "direction": "-1",
                        },
                    )
                    resp.raise_for_status()
                    return resp.json()

                results = await asyncio.gather(
                    *[_fetch_tag(tag) for tag in COMPATIBLE_PIPELINE_TAGS],
                    return_exceptions=True,
                )

                # Merge, deduplicate, and sort by downloads
                seen = set()
                models = []
                for result in results:
                    if isinstance(result, Exception):
                        continue
                    for m in result:
                        mid = m.get("id", "")
                        if mid and mid not in seen:
                            seen.add(mid)
                            models.append(m)
                models.sort(key=lambda m: m.get("downloads", 0), reverse=True)
                models = models[:15]
            else:
                response = await client.get(
                    "https://huggingface.co/api/models",
                    params={
                        "search": search,
                        "limit": 15,
                        "sort": "downloads",
                        "direction": "-1",
                    },
                )
                response.raise_for_status()
                models = response.json()

        return [
            {
                "id": m.get("id", ""),
                "downloads": m.get("downloads", 0),
                "likes": m.get("likes", 0),
                "pipeline_tag": m.get("pipeline_tag"),
                "library_name": m.get("library_name"),
                "author": m.get("author"),
                "model_type": (m.get("config") or {}).get("model_type"),
            }
            for m in models
            if m.get("id")
        ]
    except httpx.HTTPError as e:
        logger.warning(f"HuggingFace API search failed: {e}")
        raise HTTPException(status_code=502, detail="Failed to search HuggingFace models")


# =========================================================================
# Presigned URL
# =========================================================================

@router.post("/presign", response_model=PresignResponse)
async def presign_upload(
    request: PresignRequest,
    user: User = Depends(get_current_user),
    grant: dict = Depends(require_fine_tuning_access),
    s3_service: FineTuningS3Service = Depends(get_fine_tuning_s3_service),
):
    """Generate a presigned PUT URL for dataset upload."""
    try:
        presigned_url, s3_key = s3_service.generate_upload_url(
            user_id=user.user_id,
            filename=request.filename,
            content_type=request.content_type,
        )
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=s3_service.presign_expiration)
        ).isoformat()

        return PresignResponse(
            presigned_url=presigned_url,
            s3_key=s3_key,
            expires_at=expires_at,
        )
    except Exception as e:
        logger.error(f"Error generating presigned URL: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate upload URL")


# =========================================================================
# Training Jobs
# =========================================================================

@router.post("/jobs", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    request: CreateJobRequest,
    user: User = Depends(get_current_user),
    grant: dict = Depends(require_fine_tuning_access),
    jobs_repo: FineTuningJobsRepository = Depends(get_fine_tuning_jobs_repository),
    s3_service: FineTuningS3Service = Depends(get_fine_tuning_s3_service),
    sagemaker: SageMakerService = Depends(get_sagemaker_service),
    access_repo: FineTuningAccessRepository = Depends(get_fine_tuning_access_repository),
    script_service: ScriptPackagingService = Depends(get_script_packaging_service),
):
    """Create a new fine-tuning training job."""
    # Validate model — either from catalog or custom HuggingFace model
    model = MODEL_CATALOG.get(request.model_id)
    if not model and not request.custom_huggingface_model_id:
        raise HTTPException(status_code=400, detail=f"Unknown model_id: {request.model_id}")

    if request.custom_huggingface_model_id:
        # Validate the custom HuggingFace model ID format (org/model or just model)
        hf_id = request.custom_huggingface_model_id.strip()
        if not hf_id or len(hf_id) > 200:
            raise HTTPException(status_code=400, detail="Invalid HuggingFace model ID.")

    # Verify dataset exists in S3
    if not s3_service.check_object_exists(request.dataset_s3_key):
        raise HTTPException(status_code=400, detail="Dataset not found in S3. Upload your dataset first.")

    # Check quota (need at least 1 hour remaining)
    remaining = grant["monthly_quota_hours"] - grant["current_month_usage_hours"]
    if remaining < 1.0:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient quota. You have {remaining:.1f} hours remaining, minimum 1.0 required.",
        )

    # Resolve instance type and hyperparameters
    if model:
        instance_type = request.instance_type or model.default_instance_type
        hyperparameters = {**model.default_hyperparameters}
        model_name = model.model_name
        huggingface_id = model.huggingface_model_id
    else:
        # Custom HuggingFace model — use sensible defaults
        instance_type = request.instance_type or "ml.g5.xlarge"
        hyperparameters = {
            "epochs": "3",
            "per_device_train_batch_size": "8",
            "learning_rate": "2e-5",
            "weight_decay": "0.01",
            "split_ratio": "0.8",
            "seed": "42",
            "context_length": "512",
        }
        huggingface_id = request.custom_huggingface_model_id.strip()
        model_name = huggingface_id

    if request.hyperparameters:
        hyperparameters.update(request.hyperparameters)
    hyperparameters["model_name_or_path"] = huggingface_id

    # Generate identifiers
    job_id = uuid.uuid4().hex
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    project_prefix = os.environ.get("PROJECT_PREFIX", "agentcore")
    sagemaker_job_name = f"{project_prefix}-ft-{job_id[:8]}-{timestamp}"

    # Add DynamoDB progress reporting hyperparameters
    jobs_table_name = os.environ.get("DYNAMODB_FINE_TUNING_JOBS_TABLE_NAME", "fine-tuning-jobs")
    hyperparameters["dynamodb_table_name"] = jobs_table_name
    hyperparameters["dynamodb_region"] = os.environ.get("AWS_REGION", "us-west-2")
    hyperparameters["job_pk"] = f"USER#{user.user_id}"
    hyperparameters["job_sk"] = f"JOB#{job_id}"

    # Ensure training scripts are uploaded and get the S3 URI
    scripts_s3_uri = script_service.ensure_scripts_uploaded()

    # S3 paths
    output_s3_prefix = s3_service.get_output_s3_prefix(user.user_id, job_id)
    output_s3_uri = s3_service.get_output_s3_uri(user.user_id, job_id)
    input_s3_uri = f"s3://{s3_service.bucket_name}/{request.dataset_s3_key}"

    # Create DynamoDB job record
    job = jobs_repo.create_job(
        user_id=user.user_id,
        email=user.email,
        job_id=job_id,
        model_id=request.model_id,
        model_name=model_name,
        dataset_s3_key=request.dataset_s3_key,
        instance_type=instance_type,
        hyperparameters=hyperparameters,
        sagemaker_job_name=sagemaker_job_name,
        output_s3_prefix=output_s3_prefix,
        max_runtime_seconds=request.max_runtime_seconds,
    )

    # Start SageMaker training job
    try:
        sagemaker.create_training_job(
            job_name=sagemaker_job_name,
            hyperparameters=hyperparameters,
            input_s3_uri=input_s3_uri,
            output_s3_uri=output_s3_uri,
            instance_type=instance_type,
            max_runtime=request.max_runtime_seconds,
            source_dir_s3_uri=scripts_s3_uri,
        )
        job = jobs_repo.update_job_status(user.user_id, job_id, "TRAINING")
    except Exception as e:
        logger.error(f"Failed to start SageMaker job {sagemaker_job_name}: {e}")
        jobs_repo.update_job_status(
            user.user_id, job_id, "FAILED",
            error_message=f"Failed to start training: {str(e)}",
        )
        raise HTTPException(status_code=500, detail="Failed to start training job")

    return JobResponse(**job)


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    user: User = Depends(get_current_user),
    grant: dict = Depends(require_fine_tuning_access),
    jobs_repo: FineTuningJobsRepository = Depends(get_fine_tuning_jobs_repository),
):
    """List the current user's training jobs."""
    jobs = jobs_repo.list_user_jobs(user.user_id)
    return JobListResponse(
        jobs=[JobResponse(**j) for j in jobs],
        total_count=len(jobs),
    )


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: str,
    user: User = Depends(get_current_user),
    grant: dict = Depends(require_fine_tuning_access),
    jobs_repo: FineTuningJobsRepository = Depends(get_fine_tuning_jobs_repository),
    sagemaker: SageMakerService = Depends(get_sagemaker_service),
    access_repo: FineTuningAccessRepository = Depends(get_fine_tuning_access_repository),
):
    """Get job details. Syncs status from SageMaker if the job is still training."""
    job = jobs_repo.get_job(user.user_id, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Sync status from SageMaker for active jobs
    if job["status"] in ("PENDING", "TRAINING") and job.get("sagemaker_job_name"):
        job = _sync_job_status(
            job, jobs_repo, sagemaker, access_repo
        )

    return JobResponse(**job)


@router.get("/jobs/{job_id}/logs")
async def get_job_logs(
    job_id: str,
    user: User = Depends(get_current_user),
    grant: dict = Depends(require_fine_tuning_access),
    jobs_repo: FineTuningJobsRepository = Depends(get_fine_tuning_jobs_repository),
    sagemaker: SageMakerService = Depends(get_sagemaker_service),
):
    """Get CloudWatch training logs for a job."""
    job = jobs_repo.get_job(user.user_id, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if not job.get("sagemaker_job_name"):
        return {"logs": []}

    logs = sagemaker.get_training_logs(job["sagemaker_job_name"])
    return {"logs": logs}


@router.get("/jobs/{job_id}/download")
async def download_artifact(
    job_id: str,
    user: User = Depends(get_current_user),
    grant: dict = Depends(require_fine_tuning_access),
    jobs_repo: FineTuningJobsRepository = Depends(get_fine_tuning_jobs_repository),
    s3_service: FineTuningS3Service = Depends(get_fine_tuning_s3_service),
):
    """Get a presigned download URL for the model artifact."""
    job = jobs_repo.get_job(user.user_id, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["status"] != "COMPLETED":
        raise HTTPException(status_code=400, detail="Job has not completed successfully")

    # SageMaker writes output to: {output_s3_prefix}/{sagemaker_job_name}/output/model.tar.gz
    s3_key = f"{job['output_s3_prefix']}/{job['sagemaker_job_name']}/output/model.tar.gz"
    if not s3_service.check_object_exists(s3_key):
        raise HTTPException(status_code=404, detail="Model artifact not found")

    download_url = s3_service.generate_download_url(s3_key)
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=s3_service.presign_expiration)
    ).isoformat()

    return {"download_url": download_url, "expires_at": expires_at}


@router.delete("/jobs/{job_id}")
async def stop_job(
    job_id: str,
    user: User = Depends(get_current_user),
    grant: dict = Depends(require_fine_tuning_access),
    jobs_repo: FineTuningJobsRepository = Depends(get_fine_tuning_jobs_repository),
    sagemaker: SageMakerService = Depends(get_sagemaker_service),
):
    """Stop a running training job."""
    job = jobs_repo.get_job(user.user_id, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["status"] not in ("PENDING", "TRAINING"):
        raise HTTPException(status_code=400, detail=f"Cannot stop job in {job['status']} status")

    if job.get("sagemaker_job_name"):
        sagemaker.stop_training_job(job["sagemaker_job_name"])

    updated = jobs_repo.update_job_status(user.user_id, job_id, "STOPPED")
    return JobResponse(**updated)


# =========================================================================
# Internal Helpers
# =========================================================================

def _estimate_training_progress(sm_status: dict, job: dict) -> Optional[float]:
    """Estimate training progress from SageMaker secondary status and elapsed time.

    Returns progress as a percentage (0-100) or None.
    Used as a fallback when the in-container DynamoDB callback hasn't reported progress.
    """
    secondary = sm_status.get("secondary_status")

    # Fixed progress for pre/post-training phases
    phase_progress = {
        "Starting": 2.0,
        "LaunchingMLInstances": 3.0,
        "PreparingTrainingStack": 5.0,
        "DownloadingTrainingImage": 6.0,
        "Downloading": 8.0,
        "Uploading": 92.0,
    }
    if secondary in phase_progress:
        return phase_progress[secondary]

    # For "Training" phase, estimate from elapsed time using a logarithmic curve
    # that moves quickly at first then slows down (feels natural to users).
    # Approximate values: ~22% at 5m, ~40% at 15m, ~57% at 30m, ~75% at 1h, ~84% at 2h
    start_time = sm_status.get("training_start_time") or job.get("training_start_time")
    if start_time:
        try:
            start_dt = datetime.fromisoformat(start_time)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            elapsed_minutes = max(0.0, (datetime.now(timezone.utc) - start_dt).total_seconds() / 60.0)
            return round(min(85.0, 10.0 + 75.0 * (1.0 - math.exp(-elapsed_minutes / 30.0))), 1)
        except Exception:
            pass

    # Default fallback when we have no timing info
    if secondary == "Training":
        return 15.0
    return 5.0


def _sync_job_status(
    job: dict,
    jobs_repo: FineTuningJobsRepository,
    sagemaker: SageMakerService,
    access_repo: FineTuningAccessRepository,
) -> dict:
    """Sync job status from SageMaker and update DynamoDB if status changed."""
    try:
        sm_status = sagemaker.describe_training_job(job["sagemaker_job_name"])
    except Exception as e:
        logger.warning(f"Failed to describe SageMaker job {job['sagemaker_job_name']}: {e}")
        return job

    status_map = {
        "Completed": "COMPLETED",
        "Failed": "FAILED",
        "Stopped": "STOPPED",
        "InProgress": "TRAINING",
    }
    new_status = status_map.get(sm_status["status"], job["status"])

    if new_status == job["status"]:
        # For active training jobs, capture start time and estimate progress
        if new_status == "TRAINING":
            # Persist training_start_time from SageMaker if not already set
            if sm_status.get("training_start_time") and not job.get("training_start_time"):
                updated = jobs_repo.update_job_status(
                    job["user_id"], job["job_id"], job["status"],
                    training_start_time=sm_status["training_start_time"],
                )
                if updated:
                    job = updated

            # Provide fallback progress estimate when the in-container
            # DynamoDB callback hasn't reported real progress
            if job.get("training_progress") is None:
                estimated = _estimate_training_progress(sm_status, job)
                if estimated is not None:
                    job["training_progress"] = estimated

        return job

    update_kwargs = {}
    if sm_status.get("training_start_time"):
        update_kwargs["training_start_time"] = sm_status["training_start_time"]
    if sm_status.get("training_end_time"):
        update_kwargs["training_end_time"] = sm_status["training_end_time"]
    if sm_status.get("billable_seconds"):
        update_kwargs["billable_seconds"] = sm_status["billable_seconds"]
        cost = sagemaker.calculate_cost(job["instance_type"], sm_status["billable_seconds"])
        update_kwargs["estimated_cost_usd"] = cost
    if sm_status.get("failure_reason"):
        update_kwargs["error_message"] = sm_status["failure_reason"]
    if new_status == "COMPLETED":
        update_kwargs["training_progress"] = 1.0

    updated = jobs_repo.update_job_status(
        job["user_id"], job["job_id"], new_status, **update_kwargs
    )

    # Increment usage quota on completion/failure/stop (if billable time exists)
    if new_status in ("COMPLETED", "FAILED", "STOPPED") and sm_status.get("billable_seconds"):
        billable_hours = sm_status["billable_seconds"] / 3600
        access_repo.increment_usage(job["email"], billable_hours)
        logger.info(f"Incremented usage for {job['email']} by {billable_hours:.2f} hours")

    return updated


def _sync_inference_status(
    job: dict,
    inf_repo: InferenceRepository,
    sagemaker: SageMakerService,
    access_repo: FineTuningAccessRepository,
) -> dict:
    """Sync inference job status from SageMaker and update DynamoDB if changed."""
    try:
        sm_status = sagemaker.describe_transform_job(job["transform_job_name"])
    except Exception as e:
        logger.warning(f"Failed to describe transform job {job['transform_job_name']}: {e}")
        return job

    status_map = {
        "Completed": "COMPLETED",
        "Failed": "FAILED",
        "Stopped": "STOPPED",
        "InProgress": "TRANSFORMING",
    }
    new_status = status_map.get(sm_status["status"], job["status"])

    if new_status == job["status"]:
        return job

    update_kwargs = {}
    if sm_status.get("transform_start_time"):
        update_kwargs["transform_start_time"] = sm_status["transform_start_time"]
    if sm_status.get("transform_end_time"):
        update_kwargs["transform_end_time"] = sm_status["transform_end_time"]
    if sm_status.get("billable_seconds"):
        update_kwargs["billable_seconds"] = sm_status["billable_seconds"]
        cost = sagemaker.calculate_cost(job["instance_type"], sm_status["billable_seconds"])
        update_kwargs["estimated_cost_usd"] = cost
    if sm_status.get("failure_reason"):
        update_kwargs["error_message"] = sm_status["failure_reason"]

    updated = inf_repo.update_inference_status(
        job["user_id"], job["job_id"], new_status, **update_kwargs
    )

    # Increment usage quota on terminal status (if billable time exists)
    if new_status in ("COMPLETED", "FAILED", "STOPPED") and sm_status.get("billable_seconds"):
        billable_hours = sm_status["billable_seconds"] / 3600
        access_repo.increment_usage(job["email"], billable_hours)
        logger.info(f"Incremented inference usage for {job['email']} by {billable_hours:.2f} hours")

    return updated


# =========================================================================
# Trained Models (for inference model selection)
# =========================================================================

@router.get("/trained-models")
async def list_trained_models(
    user: User = Depends(get_current_user),
    grant: dict = Depends(require_fine_tuning_access),
    jobs_repo: FineTuningJobsRepository = Depends(get_fine_tuning_jobs_repository),
    s3_service: FineTuningS3Service = Depends(get_fine_tuning_s3_service),
):
    """List COMPLETED training jobs as model options for inference."""
    all_jobs = jobs_repo.list_user_jobs(user.user_id)
    completed = [j for j in all_jobs if j["status"] == "COMPLETED"]

    models = []
    for job in completed:
        # Build the model artifact S3 path
        model_s3_path = f"s3://{s3_service.bucket_name}/{job['output_s3_prefix']}/{job['sagemaker_job_name']}/output/model.tar.gz"
        models.append(
            TrainedModelResponse(
                training_job_id=job["job_id"],
                model_id=job["model_id"],
                model_name=job["model_name"],
                model_s3_path=model_s3_path,
                instance_type=job["instance_type"],
                completed_at=job.get("training_end_time"),
                estimated_cost_usd=job.get("estimated_cost_usd"),
            ).model_dump()
        )

    return models


# =========================================================================
# Inference (Batch Transform) Endpoints
# =========================================================================

@router.post("/inference/presign", response_model=PresignResponse)
async def inference_presign_upload(
    request: PresignRequest,
    user: User = Depends(get_current_user),
    grant: dict = Depends(require_fine_tuning_access),
    s3_service: FineTuningS3Service = Depends(get_fine_tuning_s3_service),
):
    """Generate a presigned PUT URL for inference input file upload."""
    try:
        presigned_url, s3_key = s3_service.generate_inference_upload_url(
            user_id=user.user_id,
            filename=request.filename,
            content_type=request.content_type,
        )
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=s3_service.presign_expiration)
        ).isoformat()

        return PresignResponse(
            presigned_url=presigned_url,
            s3_key=s3_key,
            expires_at=expires_at,
        )
    except Exception as e:
        logger.error(f"Error generating inference presigned URL: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate upload URL")


@router.post("/inference", response_model=InferenceJobResponse, status_code=status.HTTP_201_CREATED)
async def create_inference_job(
    request: CreateInferenceJobRequest,
    user: User = Depends(get_current_user),
    grant: dict = Depends(require_fine_tuning_access),
    jobs_repo: FineTuningJobsRepository = Depends(get_fine_tuning_jobs_repository),
    inf_repo: InferenceRepository = Depends(get_inference_repository),
    s3_service: FineTuningS3Service = Depends(get_fine_tuning_s3_service),
    sagemaker: SageMakerService = Depends(get_sagemaker_service),
    access_repo: FineTuningAccessRepository = Depends(get_fine_tuning_access_repository),
):
    """Create a new inference (Batch Transform) job."""
    # Look up the referenced training job — must be COMPLETED and owned by user
    training_job = jobs_repo.get_job(user.user_id, request.training_job_id)
    if not training_job:
        raise HTTPException(status_code=400, detail="Training job not found")
    if training_job["status"] != "COMPLETED":
        raise HTTPException(status_code=400, detail="Training job has not completed successfully")

    # Verify input file exists in S3
    if not s3_service.check_object_exists(request.input_s3_key):
        raise HTTPException(status_code=400, detail="Input file not found in S3. Upload your input file first.")

    # Check quota (need at least 0.5 hours remaining for inference)
    remaining = grant["monthly_quota_hours"] - grant["current_month_usage_hours"]
    if remaining < 0.5:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient quota. You have {remaining:.1f} hours remaining, minimum 0.5 required.",
        )

    # Build model artifact S3 path from training job's output
    model_s3_path = f"s3://{s3_service.bucket_name}/{training_job['output_s3_prefix']}/{training_job['sagemaker_job_name']}/output/model.tar.gz"

    # Resolve instance type (default to training job's instance type)
    instance_type = request.instance_type or training_job["instance_type"]

    # Generate identifiers
    job_id = uuid.uuid4().hex
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    project_prefix = os.environ.get("PROJECT_PREFIX", "agentcore")
    transform_job_name = f"{project_prefix}-inf-{job_id[:8]}-{timestamp}"

    # S3 paths
    output_s3_prefix = s3_service.get_inference_output_s3_prefix(user.user_id, job_id)
    output_s3_uri = s3_service.get_inference_output_s3_uri(user.user_id, job_id)
    input_s3_uri = f"s3://{s3_service.bucket_name}/{request.input_s3_key}"

    # Create DynamoDB record
    inf_repo.create_inference_job(
        user_id=user.user_id,
        email=user.email,
        job_id=job_id,
        training_job_id=request.training_job_id,
        model_name=training_job["model_name"],
        model_s3_path=model_s3_path,
        input_s3_key=request.input_s3_key,
        instance_type=instance_type,
        transform_job_name=transform_job_name,
        output_s3_prefix=output_s3_prefix,
        max_runtime_seconds=request.max_runtime_seconds,
    )

    # Start SageMaker Batch Transform job
    try:
        sagemaker.create_transform_job(
            job_name=transform_job_name,
            model_artifact_s3_uri=model_s3_path,
            input_s3_uri=input_s3_uri,
            output_s3_uri=output_s3_uri,
            instance_type=instance_type,
            max_runtime=request.max_runtime_seconds,
        )
        job = inf_repo.update_inference_status(user.user_id, job_id, "TRANSFORMING")
    except Exception as e:
        logger.error(f"Failed to start transform job {transform_job_name}: {e}")
        inf_repo.update_inference_status(
            user.user_id, job_id, "FAILED",
            error_message=f"Failed to start inference: {str(e)}",
        )
        raise HTTPException(status_code=500, detail="Failed to start inference job")

    return InferenceJobResponse(**job)


@router.get("/inference", response_model=InferenceJobListResponse)
async def list_inference_jobs(
    user: User = Depends(get_current_user),
    grant: dict = Depends(require_fine_tuning_access),
    inf_repo: InferenceRepository = Depends(get_inference_repository),
):
    """List the current user's inference jobs."""
    jobs = inf_repo.list_user_inference_jobs(user.user_id)
    return InferenceJobListResponse(
        jobs=[InferenceJobResponse(**j) for j in jobs],
        total_count=len(jobs),
    )


@router.get("/inference/{job_id}", response_model=InferenceJobResponse)
async def get_inference_job(
    job_id: str,
    user: User = Depends(get_current_user),
    grant: dict = Depends(require_fine_tuning_access),
    inf_repo: InferenceRepository = Depends(get_inference_repository),
    sagemaker: SageMakerService = Depends(get_sagemaker_service),
    access_repo: FineTuningAccessRepository = Depends(get_fine_tuning_access_repository),
):
    """Get inference job details. Syncs status from SageMaker if still transforming."""
    job = inf_repo.get_inference_job(user.user_id, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Inference job not found")

    # Sync status from SageMaker for active jobs
    if job["status"] == "TRANSFORMING" and job.get("transform_job_name"):
        job = _sync_inference_status(job, inf_repo, sagemaker, access_repo)

    return InferenceJobResponse(**job)


@router.get("/inference/{job_id}/logs")
async def get_inference_logs(
    job_id: str,
    user: User = Depends(get_current_user),
    grant: dict = Depends(require_fine_tuning_access),
    inf_repo: InferenceRepository = Depends(get_inference_repository),
    sagemaker: SageMakerService = Depends(get_sagemaker_service),
):
    """Get CloudWatch logs for an inference job."""
    job = inf_repo.get_inference_job(user.user_id, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Inference job not found")

    if not job.get("transform_job_name"):
        return {"logs": []}

    logs = sagemaker.get_transform_logs(job["transform_job_name"])
    return {"logs": logs}


@router.get("/inference/{job_id}/download")
async def download_inference_result(
    job_id: str,
    user: User = Depends(get_current_user),
    grant: dict = Depends(require_fine_tuning_access),
    inf_repo: InferenceRepository = Depends(get_inference_repository),
    s3_service: FineTuningS3Service = Depends(get_fine_tuning_s3_service),
):
    """Get a presigned download URL for inference results."""
    job = inf_repo.get_inference_job(user.user_id, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Inference job not found")

    if job["status"] != "COMPLETED":
        raise HTTPException(status_code=400, detail="Inference job has not completed successfully")

    # Batch Transform writes output based on the input filename
    # Try result_s3_key if set, otherwise construct from output prefix
    result_key = job.get("result_s3_key")
    if not result_key:
        # Batch Transform appends ".out" to the input filename
        input_filename = job["input_s3_key"].rsplit("/", 1)[-1]
        result_key = f"{job['output_s3_prefix']}/{input_filename}.out"

    if not s3_service.check_object_exists(result_key):
        raise HTTPException(status_code=404, detail="Inference results not found")

    download_url = s3_service.generate_download_url(result_key)
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=s3_service.presign_expiration)
    ).isoformat()

    return {"download_url": download_url, "expires_at": expires_at, "result_s3_key": result_key}


@router.delete("/inference/{job_id}")
async def stop_inference_job(
    job_id: str,
    user: User = Depends(get_current_user),
    grant: dict = Depends(require_fine_tuning_access),
    inf_repo: InferenceRepository = Depends(get_inference_repository),
    sagemaker: SageMakerService = Depends(get_sagemaker_service),
):
    """Stop a running inference job."""
    job = inf_repo.get_inference_job(user.user_id, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Inference job not found")

    if job["status"] not in ("PENDING", "TRANSFORMING"):
        raise HTTPException(status_code=400, detail=f"Cannot stop job in {job['status']} status")

    if job.get("transform_job_name"):
        sagemaker.stop_transform_job(job["transform_job_name"])

    updated = inf_repo.update_inference_status(user.user_id, job_id, "STOPPED")
    return InferenceJobResponse(**updated)
