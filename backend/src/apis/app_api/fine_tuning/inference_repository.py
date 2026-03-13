"""DynamoDB repository for inference (Batch Transform) jobs.

Uses the SAME fine-tuning-jobs table as training jobs, but with a
different SK prefix (INF#{jobId} instead of JOB#{jobId}).
"""

import os
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Dict, Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class InferenceRepository:
    """Repository for inference jobs in the fine-tuning-jobs DynamoDB table.

    Table schema (shared with training jobs):
        PK: USER#{userId}
        SK: INF#{jobId}

    GSI StatusIndex:
        PK: status
        SK: createdAt
    """

    def __init__(self, table_name: Optional[str] = None):
        self.table_name = table_name or os.environ.get(
            "DYNAMODB_FINE_TUNING_JOBS_TABLE_NAME", "fine-tuning-jobs"
        )
        self._dynamodb = boto3.resource("dynamodb")
        self._table = self._dynamodb.Table(self.table_name)

    @staticmethod
    def _make_pk(user_id: str) -> str:
        return f"USER#{user_id}"

    @staticmethod
    def _make_sk(job_id: str) -> str:
        return f"INF#{job_id}"

    def _item_to_dict(self, item: dict) -> dict:
        """Convert DynamoDB item to a plain dict, converting Decimals to float/int."""
        return {
            "job_id": item["job_id"],
            "user_id": item["user_id"],
            "email": item["email"],
            "job_type": item.get("job_type", "inference"),
            "training_job_id": item["training_job_id"],
            "model_name": item["model_name"],
            "model_s3_path": item["model_s3_path"],
            "status": item["status"],
            "input_s3_key": item["input_s3_key"],
            "output_s3_prefix": item.get("output_s3_prefix"),
            "result_s3_key": item.get("result_s3_key"),
            "instance_type": item["instance_type"],
            "transform_job_name": item.get("transform_job_name"),
            "transform_start_time": item.get("transform_start_time"),
            "transform_end_time": item.get("transform_end_time"),
            "billable_seconds": int(item["billable_seconds"]) if item.get("billable_seconds") is not None else None,
            "estimated_cost_usd": float(item["estimated_cost_usd"]) if item.get("estimated_cost_usd") is not None else None,
            "created_at": item["createdAt"],
            "updated_at": item["updatedAt"],
            "error_message": item.get("error_message"),
            "max_runtime_seconds": int(item.get("max_runtime_seconds", 3600)),
        }

    def create_inference_job(
        self,
        user_id: str,
        email: str,
        job_id: str,
        training_job_id: str,
        model_name: str,
        model_s3_path: str,
        input_s3_key: str,
        instance_type: str,
        transform_job_name: str,
        output_s3_prefix: str,
        max_runtime_seconds: int = 3600,
    ) -> dict:
        """Create a new inference job record."""
        now = datetime.now(timezone.utc).isoformat()

        item: Dict[str, Any] = {
            "PK": self._make_pk(user_id),
            "SK": self._make_sk(job_id),
            "job_id": job_id,
            "user_id": user_id,
            "email": email,
            "job_type": "inference",
            "training_job_id": training_job_id,
            "model_name": model_name,
            "model_s3_path": model_s3_path,
            "status": "PENDING",
            "input_s3_key": input_s3_key,
            "output_s3_prefix": output_s3_prefix,
            "instance_type": instance_type,
            "transform_job_name": transform_job_name,
            "max_runtime_seconds": max_runtime_seconds,
            "createdAt": now,
            "updatedAt": now,
        }

        try:
            self._table.put_item(Item=item)
            logger.info(f"Created inference job {job_id} for user {user_id}")
            return self._item_to_dict(item)
        except ClientError as e:
            logger.error(f"Error creating inference job {job_id}: {e}")
            raise

    def get_inference_job(self, user_id: str, job_id: str) -> Optional[dict]:
        """Get an inference job by user_id and job_id. Returns None if not found."""
        try:
            response = self._table.get_item(
                Key={"PK": self._make_pk(user_id), "SK": self._make_sk(job_id)}
            )
            item = response.get("Item")
            if not item:
                return None
            return self._item_to_dict(item)
        except ClientError as e:
            logger.error(f"Error getting inference job {job_id}: {e}")
            raise

    def list_user_inference_jobs(self, user_id: str) -> List[dict]:
        """List all inference jobs for a user, newest first."""
        try:
            response = self._table.query(
                KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
                ExpressionAttributeValues={
                    ":pk": self._make_pk(user_id),
                    ":sk_prefix": "INF#",
                },
                ScanIndexForward=False,
            )
            items = response.get("Items", [])

            while "LastEvaluatedKey" in response:
                response = self._table.query(
                    KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
                    ExpressionAttributeValues={
                        ":pk": self._make_pk(user_id),
                        ":sk_prefix": "INF#",
                    },
                    ScanIndexForward=False,
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))

            return [self._item_to_dict(item) for item in items]
        except ClientError as e:
            logger.error(f"Error listing inference jobs for user {user_id}: {e}")
            raise

    def list_all_inference_jobs(self, status_filter: Optional[str] = None) -> List[dict]:
        """List all inference jobs (admin). Optionally filter by status using GSI."""
        try:
            if status_filter:
                response = self._table.query(
                    IndexName="StatusIndex",
                    KeyConditionExpression="#s = :status",
                    FilterExpression="job_type = :jt",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":status": status_filter,
                        ":jt": "inference",
                    },
                    ScanIndexForward=False,
                )
                items = response.get("Items", [])

                while "LastEvaluatedKey" in response:
                    response = self._table.query(
                        IndexName="StatusIndex",
                        KeyConditionExpression="#s = :status",
                        FilterExpression="job_type = :jt",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={
                            ":status": status_filter,
                            ":jt": "inference",
                        },
                        ScanIndexForward=False,
                        ExclusiveStartKey=response["LastEvaluatedKey"],
                    )
                    items.extend(response.get("Items", []))
            else:
                response = self._table.scan(
                    FilterExpression="job_type = :jt",
                    ExpressionAttributeValues={":jt": "inference"},
                )
                items = response.get("Items", [])

                while "LastEvaluatedKey" in response:
                    response = self._table.scan(
                        FilterExpression="job_type = :jt",
                        ExpressionAttributeValues={":jt": "inference"},
                        ExclusiveStartKey=response["LastEvaluatedKey"],
                    )
                    items.extend(response.get("Items", []))

            return [self._item_to_dict(item) for item in items]
        except ClientError as e:
            logger.error(f"Error listing all inference jobs: {e}")
            raise

    def update_inference_status(
        self,
        user_id: str,
        job_id: str,
        status: str,
        **kwargs,
    ) -> Optional[dict]:
        """Update inference job status and optional fields.

        Supported kwargs: transform_start_time, transform_end_time,
        billable_seconds, estimated_cost_usd, error_message, result_s3_key.
        """
        now = datetime.now(timezone.utc).isoformat()

        update_parts = ["#s = :status", "updatedAt = :now"]
        attr_names: Dict[str, str] = {"#s": "status"}
        attr_values: Dict[str, Any] = {":status": status, ":now": now}

        field_map = {
            "transform_start_time": ("transform_start_time", None),
            "transform_end_time": ("transform_end_time", None),
            "billable_seconds": ("billable_seconds", lambda v: int(v)),
            "estimated_cost_usd": ("estimated_cost_usd", lambda v: Decimal(str(v))),
            "error_message": ("error_message", None),
            "result_s3_key": ("result_s3_key", None),
        }

        for kwarg_key, (attr_name, transform) in field_map.items():
            if kwarg_key in kwargs and kwargs[kwarg_key] is not None:
                placeholder = f":{kwarg_key}"
                value = kwargs[kwarg_key]
                if transform:
                    value = transform(value)
                update_parts.append(f"{attr_name} = {placeholder}")
                attr_values[placeholder] = value

        update_expr = "SET " + ", ".join(update_parts)

        try:
            response = self._table.update_item(
                Key={"PK": self._make_pk(user_id), "SK": self._make_sk(job_id)},
                UpdateExpression=update_expr,
                ExpressionAttributeNames=attr_names,
                ExpressionAttributeValues=attr_values,
                ConditionExpression="attribute_exists(PK)",
                ReturnValues="ALL_NEW",
            )
            return self._item_to_dict(response["Attributes"])
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise


# Singleton access
_inference_repo_instance: Optional[InferenceRepository] = None


def get_inference_repository() -> InferenceRepository:
    """Get or create the global InferenceRepository instance."""
    global _inference_repo_instance
    if _inference_repo_instance is None:
        _inference_repo_instance = InferenceRepository()
    return _inference_repo_instance
