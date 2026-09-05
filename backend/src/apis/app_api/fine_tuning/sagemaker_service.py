"""SageMaker service for managing fine-tuning training and inference jobs."""

import os
import logging
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from . import pricing, task_types

logger = logging.getLogger(__name__)

# =========================================================================
# Deep Learning Container images
# =========================================================================

# Keyed by (task DLC family, region).  The two families move independently on
# purpose: every text model in the catalog was trained and validated against
# transformers 4.36, and vision models simply do not load there —
# AutoModelForImageClassification predates it but the modern vision
# checkpoints do not, and SigLIP-class dual encoders arrived much later.
# Bumping one shared image to serve vision would silently re-baseline every
# existing text job.
#
# Tags verified present in us-east-1/us-east-2/us-west-2 via
# `aws ecr describe-images --registry-id 763104351884`.  eu-west-1 and
# ap-southeast-1 follow the same AWS publishing convention but could not be
# confirmed from here (an SCP denies ecr:DescribeImages in those regions);
# override with the environment variables below if a region lags.
_TRAINING_IMAGE_TAGS = {
    task_types.DLC_FAMILY_TEXT: "huggingface-pytorch-training:2.1.0-transformers4.36.0-gpu-py310-cu121-ubuntu20.04",
    task_types.DLC_FAMILY_VISION: "huggingface-pytorch-training:2.8.0-transformers4.56.2-gpu-py312-cu129-ubuntu22.04",
}

_INFERENCE_IMAGE_TAGS = {
    task_types.DLC_FAMILY_TEXT: "huggingface-pytorch-inference:2.1.0-transformers4.37.0-gpu-py310-cu118-ubuntu20.04",
    task_types.DLC_FAMILY_VISION: "huggingface-pytorch-inference:2.6.0-transformers4.51.3-gpu-py312-cu124-ubuntu22.04",
}

# The AWS-owned account that publishes Deep Learning Containers. Same in every
# region we support.
_DLC_REGISTRY_ACCOUNT = "763104351884"

_SUPPORTED_DLC_REGIONS = (
    "us-east-1",
    "us-east-2",
    "us-west-2",
    "eu-west-1",
    "ap-southeast-1",
)


def _image_uri_override(kind: str, family: str) -> str:
    """Read a per-family image override from the environment.

    A DLC tag can be retired or lag in a region.  This is the escape hatch
    that fixes it without a code deploy, e.g.
    ``FINE_TUNING_TRAINING_IMAGE_VISION=<account>.dkr.ecr...``.
    """
    return os.environ.get(f"FINE_TUNING_{kind}_IMAGE_{family.upper()}", "").strip()


def build_image_uri(region: str, tag: str) -> str:
    """Compose a full DLC ECR image URI."""
    return f"{_DLC_REGISTRY_ACCOUNT}.dkr.ecr.{region}.amazonaws.com/{tag}"


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

    def _resolve_image_uri(self, kind: str, task_type: Optional[str]) -> str:
        """Resolve the DLC image for a task, honouring any environment override."""
        spec = task_types.get_task_spec(task_type)
        family = spec.dlc_family

        override = _image_uri_override(kind, family)
        if override:
            logger.info(f"Using overridden {kind.lower()} image for {family}: {override}")
            return override

        tags = _TRAINING_IMAGE_TAGS if kind == "TRAINING" else _INFERENCE_IMAGE_TAGS
        tag = tags.get(family)
        if not tag:
            raise ValueError(f"No {kind.lower()} DLC image configured for task family '{family}'")
        if self._region not in _SUPPORTED_DLC_REGIONS:
            raise ValueError(
                f"No HuggingFace DLC image configured for region {self._region}"
            )
        return build_image_uri(self._region, tag)

    def get_huggingface_image_uri(self, task_type: Optional[str] = None) -> str:
        """Return the training DLC image URI for ``task_type`` in this region."""
        return self._resolve_image_uri("TRAINING", task_type)

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
        task_type: Optional[str] = None,
        volume_size_gb: Optional[int] = None,
    ) -> dict:
        """Create a SageMaker training job.

        When source_dir_s3_uri is provided, injects sagemaker_program and
        sagemaker_submit_directory hyperparameters so the HuggingFace DLC
        uses the custom training script instead of the default.

        ``task_type`` selects the DLC image family; omitting it keeps the
        historical text-classification container.

        Returns the response from create_training_job API call.
        """
        image_uri = self.get_huggingface_image_uri(task_type)
        spec = task_types.get_task_spec(task_type)

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
                # An image dataset holds the archive, the unpacked copy and
                # the model checkpoint at once, so the text default is not
                # enough headroom for the archive-based tasks.
                "VolumeSizeInGB": volume_size_gb
                or (200 if spec.requires_archive else 100),
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
    def calculate_cost(
        instance_type: str, billable_seconds: int, *, transform: bool = False
    ) -> float:
        """Estimated cost for billable time on an instance.

        Training and Batch Transform are priced separately — see
        ``pricing.py`` — so the caller has to say which one it is measuring.
        """
        return pricing.calculate_cost(
            instance_type, billable_seconds, transform=transform
        )

    # =====================================================================
    # Batch Transform (Inference) Methods
    # =====================================================================

    def get_huggingface_inference_image_uri(
        self, task_type: Optional[str] = None
    ) -> str:
        """Return the inference DLC image URI for ``task_type`` in this region."""
        return self._resolve_image_uri("INFERENCE", task_type)

    def create_transform_job(
        self,
        job_name: str,
        model_artifact_s3_uri: str,
        input_s3_uri: str,
        output_s3_uri: str,
        instance_type: str,
        instance_count: int = 1,
        max_runtime: int = 3600,
        task_type: Optional[str] = None,
    ) -> dict:
        """Create a SageMaker Batch Transform job.

        Steps:
        1. Create a SageMaker Model from the training artifact
        2. Create a Transform Job using that model

        ``task_type`` selects both the DLC image family and the payload
        contract — the image tasks send a .zip archive rather than newline
        delimited text, and need a far larger MaxPayloadInMB for it.

        Returns the response from create_transform_job API call.
        """
        image_uri = self.get_huggingface_inference_image_uri(task_type)
        spec = task_types.get_task_spec(task_type)
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
                "ContentType": spec.inference_content_type,
            },
            "TransformOutput": {
                "S3OutputPath": output_s3_uri,
            },
            "TransformResources": {
                "InstanceType": instance_type,
                "InstanceCount": instance_count,
            },
            "MaxPayloadInMB": spec.inference_max_payload_mb,
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
