"""SageMaker service for managing fine-tuning training and inference jobs."""

import os
import logging
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from .job_models import INSTANCE_COST_PER_HOUR

logger = logging.getLogger(__name__)

# HuggingFace Deep Learning Container image URIs by region
# PyTorch 2.1 + Transformers 4.36 (GPU, Python 3.10)
_HF_DLC_IMAGES = {
    "us-east-1": "763104351884.dkr.ecr.us-east-1.amazonaws.com/huggingface-pytorch-training:2.1.0-transformers4.36.0-gpu-py310-cu121-ubuntu20.04",
    "us-east-2": "763104351884.dkr.ecr.us-east-2.amazonaws.com/huggingface-pytorch-training:2.1.0-transformers4.36.0-gpu-py310-cu121-ubuntu20.04",
    "us-west-2": "763104351884.dkr.ecr.us-west-2.amazonaws.com/huggingface-pytorch-training:2.1.0-transformers4.36.0-gpu-py310-cu121-ubuntu20.04",
    "eu-west-1": "763104351884.dkr.ecr.eu-west-1.amazonaws.com/huggingface-pytorch-training:2.1.0-transformers4.36.0-gpu-py310-cu121-ubuntu20.04",
    "ap-southeast-1": "763104351884.dkr.ecr.ap-southeast-1.amazonaws.com/huggingface-pytorch-training:2.1.0-transformers4.36.0-gpu-py310-cu121-ubuntu20.04",
}


_HF_DLC_INFERENCE_IMAGES = {
    "us-east-1": "763104351884.dkr.ecr.us-east-1.amazonaws.com/huggingface-pytorch-inference:2.1.0-transformers4.37.0-gpu-py310-cu118-ubuntu20.04",
    "us-east-2": "763104351884.dkr.ecr.us-east-2.amazonaws.com/huggingface-pytorch-inference:2.1.0-transformers4.37.0-gpu-py310-cu118-ubuntu20.04",
    "us-west-2": "763104351884.dkr.ecr.us-west-2.amazonaws.com/huggingface-pytorch-inference:2.1.0-transformers4.37.0-gpu-py310-cu118-ubuntu20.04",
    "eu-west-1": "763104351884.dkr.ecr.eu-west-1.amazonaws.com/huggingface-pytorch-inference:2.1.0-transformers4.37.0-gpu-py310-cu118-ubuntu20.04",
    "ap-southeast-1": "763104351884.dkr.ecr.ap-southeast-1.amazonaws.com/huggingface-pytorch-inference:2.1.0-transformers4.37.0-gpu-py310-cu118-ubuntu20.04",
}


class SageMakerService:
    """Wrapper around boto3 SageMaker client for training and inference operations."""

    def __init__(
        self,
        sagemaker_client=None,
        logs_client=None,
        role_arn: Optional[str] = None,
        security_group_id: Optional[str] = None,
        subnet_ids: Optional[str] = None,
    ):
        region = os.environ.get("AWS_REGION", "us-west-2")
        self._sagemaker = sagemaker_client or boto3.client("sagemaker", region_name=region)
        self._logs = logs_client or boto3.client("logs", region_name=region)
        self._region = region
        self._role_arn = role_arn or os.environ.get("SAGEMAKER_EXECUTION_ROLE_ARN", "")
        self._security_group_id = security_group_id or os.environ.get("SAGEMAKER_SECURITY_GROUP_ID", "")
        self._subnet_ids = subnet_ids or os.environ.get("SAGEMAKER_SUBNET_IDS", "")

    def get_huggingface_image_uri(self) -> str:
        """Return the HuggingFace training DLC image URI for the current region."""
        uri = _HF_DLC_IMAGES.get(self._region)
        if not uri:
            raise ValueError(f"No HuggingFace DLC image configured for region {self._region}")
        return uri

    def create_training_job(
        self,
        job_name: str,
        hyperparameters: Dict[str, str],
        input_s3_uri: str,
        output_s3_uri: str,
        instance_type: str,
        instance_count: int = 1,
        max_runtime: int = 86400,
        source_dir_s3_uri: str = "",
    ) -> dict:
        """Create a SageMaker training job.

        When source_dir_s3_uri is provided, injects sagemaker_program and
        sagemaker_submit_directory hyperparameters so the HuggingFace DLC
        uses the custom training script instead of the default.

        Returns the response from create_training_job API call.
        """
        image_uri = self.get_huggingface_image_uri()

        # Inject custom script hyperparameters if source_dir provided
        if source_dir_s3_uri:
            hyperparameters = {**hyperparameters}  # Copy to avoid mutation
            hyperparameters["sagemaker_program"] = "train.py"
            hyperparameters["sagemaker_submit_directory"] = source_dir_s3_uri

        subnets = [s.strip() for s in self._subnet_ids.split(",") if s.strip()]
        security_groups = [self._security_group_id] if self._security_group_id else []

        params = {
            "TrainingJobName": job_name,
            "AlgorithmSpecification": {
                "TrainingImage": image_uri,
                "TrainingInputMode": "File",
            },
            "RoleArn": self._role_arn,
            "InputDataConfig": [
                {
                    "ChannelName": "train",
                    "DataSource": {
                        "S3DataSource": {
                            "S3DataType": "S3Prefix",
                            "S3Uri": input_s3_uri,
                            "S3DataDistributionType": "FullyReplicated",
                        }
                    },
                }
            ],
            "OutputDataConfig": {
                "S3OutputPath": output_s3_uri,
            },
            "ResourceConfig": {
                "InstanceType": instance_type,
                "InstanceCount": instance_count,
                "VolumeSizeInGB": 100,
            },
            "StoppingCondition": {
                "MaxRuntimeInSeconds": max_runtime,
            },
            "HyperParameters": hyperparameters,
        }

        if subnets and security_groups:
            params["VpcConfig"] = {
                "SecurityGroupIds": security_groups,
                "Subnets": subnets,
            }

        try:
            response = self._sagemaker.create_training_job(**params)
            logger.info(f"Created SageMaker training job: {job_name}")
            return response
        except ClientError as e:
            logger.error(f"Error creating training job {job_name}: {e}")
            raise

    def describe_training_job(self, job_name: str) -> dict:
        """Describe a SageMaker training job.

        Returns a normalized dict with status, timestamps, and failure reason.
        """
        try:
            response = self._sagemaker.describe_training_job(
                TrainingJobName=job_name
            )

            result = {
                "status": response["TrainingJobStatus"],
                "secondary_status": response.get("SecondaryStatus"),
            }

            if "TrainingStartTime" in response:
                result["training_start_time"] = response["TrainingStartTime"].isoformat()
            if "TrainingEndTime" in response:
                result["training_end_time"] = response["TrainingEndTime"].isoformat()
            if "BillableTimeInSeconds" in response:
                result["billable_seconds"] = response["BillableTimeInSeconds"]
            if "FailureReason" in response:
                result["failure_reason"] = response["FailureReason"]

            return result
        except ClientError as e:
            logger.error(f"Error describing training job {job_name}: {e}")
            raise

    def stop_training_job(self, job_name: str) -> bool:
        """Stop a SageMaker training job. Returns True if stop was requested."""
        try:
            self._sagemaker.stop_training_job(TrainingJobName=job_name)
            logger.info(f"Requested stop for training job: {job_name}")
            return True
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "ValidationException":
                logger.warning(f"Training job {job_name} cannot be stopped (may already be stopped)")
                return False
            raise

    def get_training_logs(self, job_name: str, limit: int = 100) -> List[str]:
        """Read CloudWatch training logs for a job.

        Returns a list of log messages (most recent last).
        """
        log_group = "/aws/sagemaker/TrainingJobs"
        log_stream_prefix = f"{job_name}/algo-1"

        try:
            streams_response = self._logs.describe_log_streams(
                logGroupName=log_group,
                logStreamNamePrefix=log_stream_prefix,
                orderBy="LogStreamName",
                descending=False,
            )
            streams = streams_response.get("logStreams", [])
            if not streams:
                return []

            log_stream_name = streams[0]["logStreamName"]
            events_response = self._logs.get_log_events(
                logGroupName=log_group,
                logStreamName=log_stream_name,
                limit=limit,
                startFromHead=False,
            )

            return [event["message"] for event in events_response.get("events", [])]
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "ResourceNotFoundException":
                return []
            logger.error(f"Error reading logs for {job_name}: {e}")
            raise

    @staticmethod
    def calculate_cost(instance_type: str, billable_seconds: int) -> float:
        """Calculate estimated cost based on instance type and billable time."""
        cost_per_hour = INSTANCE_COST_PER_HOUR.get(instance_type, 0.0)
        return round(cost_per_hour * (billable_seconds / 3600), 4)

    # =====================================================================
    # Batch Transform (Inference) Methods
    # =====================================================================

    def get_huggingface_inference_image_uri(self) -> str:
        """Return the HuggingFace inference DLC image URI for the current region."""
        uri = _HF_DLC_INFERENCE_IMAGES.get(self._region)
        if not uri:
            raise ValueError(f"No HuggingFace inference DLC image configured for region {self._region}")
        return uri

    def create_transform_job(
        self,
        job_name: str,
        model_artifact_s3_uri: str,
        input_s3_uri: str,
        output_s3_uri: str,
        instance_type: str,
        instance_count: int = 1,
        max_runtime: int = 3600,
    ) -> dict:
        """Create a SageMaker Batch Transform job.

        Steps:
        1. Create a SageMaker Model from the training artifact
        2. Create a Transform Job using that model

        Returns the response from create_transform_job API call.
        """
        image_uri = self.get_huggingface_inference_image_uri()
        model_name = f"model-{job_name}"

        subnets = [s.strip() for s in self._subnet_ids.split(",") if s.strip()]
        security_groups = [self._security_group_id] if self._security_group_id else []

        # Step 1: Create SageMaker Model
        model_params = {
            "ModelName": model_name,
            "PrimaryContainer": {
                "Image": image_uri,
                "ModelDataUrl": model_artifact_s3_uri,
            },
            "ExecutionRoleArn": self._role_arn,
        }

        if subnets and security_groups:
            model_params["VpcConfig"] = {
                "SecurityGroupIds": security_groups,
                "Subnets": subnets,
            }

        try:
            self._sagemaker.create_model(**model_params)
            logger.info(f"Created SageMaker model: {model_name}")
        except ClientError as e:
            logger.error(f"Error creating model {model_name}: {e}")
            raise

        # Step 2: Create Transform Job
        transform_params = {
            "TransformJobName": job_name,
            "ModelName": model_name,
            "TransformInput": {
                "DataSource": {
                    "S3DataSource": {
                        "S3DataType": "S3Prefix",
                        "S3Uri": input_s3_uri,
                    }
                },
                "ContentType": "text/plain",
            },
            "TransformOutput": {
                "S3OutputPath": output_s3_uri,
            },
            "TransformResources": {
                "InstanceType": instance_type,
                "InstanceCount": instance_count,
            },
            "MaxPayloadInMB": 6,
        }

        if max_runtime:
            transform_params["TransformJobName"] = job_name
            transform_params["ModelName"] = model_name

        try:
            response = self._sagemaker.create_transform_job(**transform_params)
            logger.info(f"Created SageMaker transform job: {job_name}")
            return response
        except ClientError as e:
            logger.error(f"Error creating transform job {job_name}: {e}")
            raise

    def describe_transform_job(self, job_name: str) -> dict:
        """Describe a SageMaker Batch Transform job.

        Returns a normalized dict with status, timestamps, and failure reason.
        """
        try:
            response = self._sagemaker.describe_transform_job(
                TransformJobName=job_name
            )

            result = {
                "status": response["TransformJobStatus"],
            }

            if "TransformStartTime" in response:
                result["transform_start_time"] = response["TransformStartTime"].isoformat()
            if "TransformEndTime" in response:
                result["transform_end_time"] = response["TransformEndTime"].isoformat()
            if "FailureReason" in response:
                result["failure_reason"] = response["FailureReason"]

            # Calculate billable seconds from start/end times
            if "TransformStartTime" in response and "TransformEndTime" in response:
                delta = response["TransformEndTime"] - response["TransformStartTime"]
                result["billable_seconds"] = int(delta.total_seconds())

            return result
        except ClientError as e:
            logger.error(f"Error describing transform job {job_name}: {e}")
            raise

    def stop_transform_job(self, job_name: str) -> bool:
        """Stop a SageMaker Batch Transform job. Returns True if stop was requested."""
        try:
            self._sagemaker.stop_transform_job(TransformJobName=job_name)
            logger.info(f"Requested stop for transform job: {job_name}")
            return True
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "ValidationException":
                logger.warning(f"Transform job {job_name} cannot be stopped (may already be stopped)")
                return False
            raise

    def get_transform_logs(self, job_name: str, limit: int = 100) -> List[str]:
        """Read CloudWatch logs for a Batch Transform job.

        Returns a list of log messages (most recent last).
        """
        log_group = "/aws/sagemaker/TransformJobs"
        log_stream_prefix = f"{job_name}/"

        try:
            streams_response = self._logs.describe_log_streams(
                logGroupName=log_group,
                logStreamNamePrefix=log_stream_prefix,
                orderBy="LogStreamName",
                descending=False,
            )
            streams = streams_response.get("logStreams", [])
            if not streams:
                return []

            log_stream_name = streams[0]["logStreamName"]
            events_response = self._logs.get_log_events(
                logGroupName=log_group,
                logStreamName=log_stream_name,
                limit=limit,
                startFromHead=False,
            )

            return [event["message"] for event in events_response.get("events", [])]
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "ResourceNotFoundException":
                return []
            logger.error(f"Error reading logs for transform job {job_name}: {e}")
            raise


# Singleton access
_sagemaker_service_instance: Optional[SageMakerService] = None


def get_sagemaker_service() -> SageMakerService:
    """Get or create the global SageMakerService instance."""
    global _sagemaker_service_instance
    if _sagemaker_service_instance is None:
        _sagemaker_service_instance = SageMakerService()
    return _sagemaker_service_instance
