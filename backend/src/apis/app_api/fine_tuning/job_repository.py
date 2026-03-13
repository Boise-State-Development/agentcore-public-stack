"""DynamoDB repository for fine-tuning training jobs table."""

import os
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Dict, Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class FineTuningJobsRepository:
    """Repository for the fine-tuning-jobs DynamoDB table.

    Table schema:
        PK: USER#{userId}
        SK: JOB#{jobId}

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
        return f"JOB#{job_id}"

    def _item_to_dict(self, item: dict) -> dict:
        """Convert DynamoDB item to a plain dict, converting Decimals to float/int."""
        result = {
            "job_id": item["job_id"],
            "user_id": item["user_id"],
            "email": item["email"],
            "model_id": item["model_id"],
            "model_name": item["model_name"],
            "status": item["status"],
            "dataset_s3_key": item["dataset_s3_key"],
            "output_s3_prefix": item.get("output_s3_prefix"),
            "instance_type": item["instance_type"],
            "instance_count": int(item.get("instance_count", 1)),
            "hyperparameters": item.get("hyperparameters"),
            "sagemaker_job_name": item.get("sagemaker_job_name"),
            "training_start_time": item.get("training_start_time"),
            "training_end_time": item.get("training_end_time"),
            "billable_seconds": int(item["billable_seconds"]) if item.get("billable_seconds") is not None else None,
            "estimated_cost_usd": float(item["estimated_cost_usd"]) if item.get("estimated_cost_usd") is not None else None,
            "created_at": item["createdAt"],
            "updated_at": item["updatedAt"],
            "error_message": item.get("error_message"),
            "max_runtime_seconds": int(item.get("max_runtime_seconds", 86400)),
            "training_progress": float(item["training_progress"]) if item.get("training_progress") is not None else None,
        }
        return result

    def create_job(
        self,
        user_id: str,
        email: str,
        job_id: str,
        model_id: str,
        model_name: str,
        dataset_s3_key: str,
        instance_type: str,
        hyperparameters: Optional[Dict[str, str]],
        sagemaker_job_name: str,
        output_s3_prefix: str,
        max_runtime_seconds: int = 86400,
    ) -> dict:
        """Create a new training job record."""
        now = datetime.now(timezone.utc).isoformat()

        item: Dict[str, Any] = {
            "PK": self._make_pk(user_id),
            "SK": self._make_sk(job_id),
            "job_id": job_id,
            "user_id": user_id,
            "email": email,
            "model_id": model_id,
            "model_name": model_name,
            "status": "PENDING",
            "dataset_s3_key": dataset_s3_key,
            "output_s3_prefix": output_s3_prefix,
            "instance_type": instance_type,
            "instance_count": 1,
            "sagemaker_job_name": sagemaker_job_name,
            "max_runtime_seconds": max_runtime_seconds,
            "createdAt": now,
            "updatedAt": now,
        }

        if hyperparameters:
            item["hyperparameters"] = hyperparameters

        try:
            self._table.put_item(Item=item)
            logger.info(f"Created fine-tuning job {job_id} for user {user_id}")
            return self._item_to_dict(item)
        except ClientError as e:
            logger.error(f"Error creating job {job_id}: {e}")
            raise

    def get_job(self, user_id: str, job_id: str) -> Optional[dict]:
        """Get a job by user_id and job_id. Returns None if not found."""
        try:
            response = self._table.get_item(
                Key={"PK": self._make_pk(user_id), "SK": self._make_sk(job_id)}
            )
            item = response.get("Item")
            if not item:
                return None
            return self._item_to_dict(item)
        except ClientError as e:
            logger.error(f"Error getting job {job_id}: {e}")
            raise

    def list_user_jobs(self, user_id: str) -> List[dict]:
        """List all jobs for a user, newest first."""
        try:
            response = self._table.query(
                KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
                ExpressionAttributeValues={
                    ":pk": self._make_pk(user_id),
                    ":sk_prefix": "JOB#",
                },
                ScanIndexForward=False,
            )
            items = response.get("Items", [])

            while "LastEvaluatedKey" in response:
                response = self._table.query(
                    KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
                    ExpressionAttributeValues={
                        ":pk": self._make_pk(user_id),
                        ":sk_prefix": "JOB#",
                    },
                    ScanIndexForward=False,
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))

            return [self._item_to_dict(item) for item in items]
        except ClientError as e:
            logger.error(f"Error listing jobs for user {user_id}: {e}")
            raise

    def list_all_jobs(self, status_filter: Optional[str] = None) -> List[dict]:
        """List all jobs (admin). Optionally filter by status using GSI."""
        try:
            if status_filter:
                response = self._table.query(
                    IndexName="StatusIndex",
                    KeyConditionExpression="#s = :status",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":status": status_filter},
                    ScanIndexForward=False,
                )
                items = response.get("Items", [])

                while "LastEvaluatedKey" in response:
                    response = self._table.query(
                        IndexName="StatusIndex",
                        KeyConditionExpression="#s = :status",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":status": status_filter},
                        ScanIndexForward=False,
                        ExclusiveStartKey=response["LastEvaluatedKey"],
                    )
                    items.extend(response.get("Items", []))
            else:
                response = self._table.scan()
                items = response.get("Items", [])

                while "LastEvaluatedKey" in response:
                    response = self._table.scan(
                        ExclusiveStartKey=response["LastEvaluatedKey"],
                    )
                    items.extend(response.get("Items", []))

            return [self._item_to_dict(item) for item in items]
        except ClientError as e:
            logger.error(f"Error listing all jobs: {e}")
            raise

    def update_job_status(
        self,
        user_id: str,
        job_id: str,
        status: str,
        **kwargs,
    ) -> Optional[dict]:
        """Update job status and optional fields.

        Supported kwargs: training_start_time, training_end_time,
        billable_seconds, estimated_cost_usd, error_message.
        """
        now = datetime.now(timezone.utc).isoformat()

        update_parts = ["#s = :status", "updatedAt = :now"]
        attr_names: Dict[str, str] = {"#s": "status"}
        attr_values: Dict[str, Any] = {":status": status, ":now": now}

        field_map = {
            "training_start_time": ("training_start_time", None),
            "training_end_time": ("training_end_time", None),
            "billable_seconds": ("billable_seconds", lambda v: int(v)),
            "estimated_cost_usd": ("estimated_cost_usd", lambda v: Decimal(str(v))),
            "error_message": ("error_message", None),
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

    def delete_job(self, user_id: str, job_id: str) -> bool:
        """Delete a job record. Returns False if not found."""
        try:
            self._table.delete_item(
                Key={"PK": self._make_pk(user_id), "SK": self._make_sk(job_id)},
                ConditionExpression="attribute_exists(PK)",
            )
            logger.info(f"Deleted job {job_id} for user {user_id}")
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise


# Singleton access
_jobs_repository_instance: Optional[FineTuningJobsRepository] = None


def get_fine_tuning_jobs_repository() -> FineTuningJobsRepository:
    """Get or create the global FineTuningJobsRepository instance."""
    global _jobs_repository_instance
    if _jobs_repository_instance is None:
        _jobs_repository_instance = FineTuningJobsRepository()
    return _jobs_repository_instance
