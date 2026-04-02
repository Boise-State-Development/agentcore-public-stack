"""S3 presigned URL service for the fine-tuning data bucket."""

import os
import logging
import uuid
from typing import Optional, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class FineTuningS3Service:
    """Generates presigned URLs for dataset upload and model artifact download.

    S3 key patterns:
        Training dataset:   datasets/{userId}/{uuid}/{filename}
        Training output:    output/{userId}/{jobId}/  (SageMaker writes here)
        Inference input:    inference-input/{userId}/{uuid}/{filename}
        Inference output:   inference-output/{userId}/{jobId}/  (Batch Transform writes here)
    """

    def __init__(
        self,
        s3_client=None,
        bucket_name: Optional[str] = None,
    ):
        region = os.environ.get("AWS_REGION", "us-west-2")
        s3_config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "virtual"},
        )
        self._s3_client = s3_client or boto3.client(
            "s3",
            region_name=region,
            config=s3_config,
            endpoint_url=f"https://s3.{region}.amazonaws.com",
        )
        self.bucket_name = bucket_name or os.environ.get(
            "S3_FINE_TUNING_BUCKET_NAME", "fine-tuning-data"
        )
        self.presign_expiration = 15 * 60  # 15 minutes

    def generate_upload_url(
        self, user_id: str, filename: str, content_type: str
    ) -> Tuple[str, str]:
        """Generate a presigned PUT URL for dataset upload.

        Returns (presigned_url, s3_key).
        """
        upload_id = uuid.uuid4().hex
        s3_key = f"datasets/{user_id}/{upload_id}/{filename}"

        presigned_url = self._s3_client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket_name,
                "Key": s3_key,
                "ContentType": content_type,
            },
            ExpiresIn=self.presign_expiration,
        )

        return presigned_url, s3_key

    def generate_download_url(self, s3_key: str) -> str:
        """Generate a presigned GET URL for artifact download."""
        return self._s3_client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket_name,
                "Key": s3_key,
            },
            ExpiresIn=self.presign_expiration,
        )

    def check_object_exists(self, s3_key: str) -> bool:
        """Check if an S3 object exists via HEAD request."""
        try:
            self._s3_client.head_object(Bucket=self.bucket_name, Key=s3_key)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            raise

    def get_output_s3_prefix(self, user_id: str, job_id: str) -> str:
        """Return the S3 prefix where SageMaker should write output."""
        return f"output/{user_id}/{job_id}"

    def get_output_s3_uri(self, user_id: str, job_id: str) -> str:
        """Return the full s3:// URI for SageMaker output config."""
        prefix = self.get_output_s3_prefix(user_id, job_id)
        return f"s3://{self.bucket_name}/{prefix}"

    # =====================================================================
    # Inference (Batch Transform) S3 methods
    # =====================================================================

    def generate_inference_upload_url(
        self, user_id: str, filename: str, content_type: str
    ) -> Tuple[str, str]:
        """Generate a presigned PUT URL for inference input file upload.

        Returns (presigned_url, s3_key).
        """
        upload_id = uuid.uuid4().hex
        s3_key = f"inference-input/{user_id}/{upload_id}/{filename}"

        presigned_url = self._s3_client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket_name,
                "Key": s3_key,
                "ContentType": content_type,
            },
            ExpiresIn=self.presign_expiration,
        )

        return presigned_url, s3_key

    def get_inference_output_s3_prefix(self, user_id: str, job_id: str) -> str:
        """Return the S3 prefix where Batch Transform should write output."""
        return f"inference-output/{user_id}/{job_id}"

    def get_inference_output_s3_uri(self, user_id: str, job_id: str) -> str:
        """Return the full s3:// URI for Batch Transform output config."""
        prefix = self.get_inference_output_s3_prefix(user_id, job_id)
        return f"s3://{self.bucket_name}/{prefix}"


# Singleton access
_s3_service_instance: Optional[FineTuningS3Service] = None


def get_fine_tuning_s3_service() -> FineTuningS3Service:
    """Get or create the global FineTuningS3Service instance."""
    global _s3_service_instance
    if _s3_service_instance is None:
        _s3_service_instance = FineTuningS3Service()
    return _s3_service_instance
