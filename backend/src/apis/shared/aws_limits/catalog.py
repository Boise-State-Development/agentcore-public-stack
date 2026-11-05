"""The curated set of AWS quotas the AWS Limits page monitors.

Grouped by the stack component that depends on each quota. A group whose
feature is not deployed is hidden, and its quotas are then also outside the
catalog for increase requests: ``POST /admin/aws-limits/requests`` accepts only
quotas this module says are monitored, so the endpoint cannot become a "raise
any AWS quota" API.

Quota codes were read from the dev account (us-west-2, Nov 3 2026). A code is
the same in every Region; a quota that does not exist in a Region simply does
not appear.

Two groups select quotas by name instead of code because the sets are large
and grow with AWS's catalogue: Bedrock inference quotas (one per model and
inference type) and SageMaker instance quotas (one per instance type and job
kind).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional, Pattern, Tuple

from apis.shared.feature_flags import dictation_enabled

from .bedrock_match import is_inference_quota
from .models import DocLink

# Every service code a request may target. Mirrors the
# ``servicequotas:service`` condition on the app-api task role's
# RequestServiceQuotaIncrease grant (app-api-iam-grants.ts); keep both in step.
MONITORED_SERVICE_CODES: Tuple[str, ...] = (
    "bedrock",
    "bedrock-agentcore",
    "lambda",
    "fargate",
    "ecs",
    "dynamodb",
    "sagemaker",
    "transcribe",
)

GUIDANCE_URL = "https://boise-state-development.github.io/agentcore-public-stack/admin/aws-limits/"

S3_VECTORS_LIMITS_URL = "https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-limitations.html"

# SageMaker training and Batch Transform quotas for the GPU families the
# fine-tuning feature offers (apis/app_api/fine_tuning/pricing.py: G5, G6,
# G6e). Matched by name because there is one quota per size and job kind.
SAGEMAKER_INSTANCE_PATTERN = re.compile(r"^ml\.(?:g5|g6|g6e)\.\d*xlarge for (?:training|transform) job usage$")


def _always() -> bool:
    return True


def fine_tuning_enabled() -> bool:
    # Read the same way app_api/main.py mounts the fine-tuning routers.
    return os.environ.get("FINE_TUNING_ENABLED", "false").strip().lower() == "true"


@dataclass(frozen=True)
class CatalogGroup:
    id: str
    label: str
    description: str
    # (service code, quota code) pairs shown in this group.
    quotas: Tuple[Tuple[str, str], ...] = ()
    # (service code, name predicate) for name-selected groups.
    name_selectors: Tuple[Tuple[str, Callable[[str], bool]], ...] = ()
    doc_links: Tuple[DocLink, ...] = ()
    enabled: Callable[[], bool] = field(default=_always)
    # Bedrock only: filter the default view to managed-catalog models.
    filter_to_managed_models: bool = False
    # Bedrock only: with "Show all quotas" on, every quota of these services.
    show_all_services: Tuple[str, ...] = ()

    @property
    def service_codes(self) -> Tuple[str, ...]:
        codes = [s for s, _ in self.quotas] + [s for s, _ in self.name_selectors] + list(self.show_all_services)
        return tuple(dict.fromkeys(codes))

    def selects(self, service_code: str, quota_code: str, quota_name: str, *, all_quotas: bool) -> bool:
        """Whether this group shows the quota (before any model filtering)."""
        if all_quotas and service_code in self.show_all_services:
            return True
        if (service_code, quota_code) in self.quotas:
            return True
        return any(s == service_code and pred(quota_name) for s, pred in self.name_selectors)


def _matches(pattern: Pattern[str]) -> Callable[[str], bool]:
    return lambda name: bool(pattern.match(name))


CATALOG: Tuple[CatalogGroup, ...] = (
    CatalogGroup(
        id="model_inference",
        label="Model inference",
        description=(
            "Bedrock requests and tokens per minute: on-demand, cross-Region and global "
            "inference, and the bedrock-mantle endpoint. Filtered to the models in the "
            "managed-model catalog unless all quotas are shown."
        ),
        name_selectors=(("bedrock", is_inference_quota),),
        filter_to_managed_models=True,
        show_all_services=("bedrock",),
    ),
    CatalogGroup(
        id="agent_runtime",
        label="Agent runtime",
        description="AgentCore Runtime sessions, Memory, Code Interpreter, Browser and Gateway.",
        quotas=(
            ("bedrock-agentcore", "L-3E5722B2"),  # Active Session Workloads per Account
            ("bedrock-agentcore", "L-8EE2AEA2"),  # Rate of new Runtime session creation
            ("bedrock-agentcore", "L-46ED137C"),  # Rate of Runtime data plane APIs
            ("bedrock-agentcore", "L-81002DCC"),  # Memories
            ("bedrock-agentcore", "L-E3D6644C"),  # Tokens per minute for long-term memory extraction
            ("bedrock-agentcore", "L-CAF6F552"),  # Concurrent active Code Interpreter sessions
            ("bedrock-agentcore", "L-1CB82154"),  # Concurrent active Browser sessions
            ("bedrock-agentcore", "L-410A4D7F"),  # Gateways per Region
            ("bedrock-agentcore", "L-3601D726"),  # Targets per gateway
            ("bedrock-agentcore", "L-A0D48779"),  # Rate of tool-call/tool-list requests
        ),
    ),
    CatalogGroup(
        id="background_jobs",
        label="Background jobs",
        description=(
            "Every batch job (KB sync, conversation index, memory maintenance, scheduled "
            "runs, RAG ingestion, large documents, KB migration, platform cost sync) is a "
            "Lambda function and shares the account's concurrency."
        ),
        quotas=(("lambda", "L-B99A9384"),),  # Concurrent executions
    ),
    CatalogGroup(
        id="app_api",
        label="App API",
        description="The App API runs as an ECS service on Fargate.",
        quotas=(
            ("fargate", "L-3032A538"),  # Fargate On-Demand vCPU resource count
            ("ecs", "L-92E49DE3"),  # Tasks per service
        ),
    ),
    CatalogGroup(
        id="data",
        label="Data",
        description="DynamoDB tables and throughput.",
        quotas=(
            ("dynamodb", "L-F98FE922"),  # Maximum number of tables
            ("dynamodb", "L-CF0CBE56"),  # Table-level read throughput limit
            ("dynamodb", "L-AB614373"),  # Table-level write throughput limit
        ),
    ),
    CatalogGroup(
        id="knowledge_bases",
        label="Knowledge bases (RAG)",
        description=(
            "S3 Vectors stores the assistant and attachment embeddings. Its limits are not "
            "in Service Quotas, so they are listed in the S3 Vectors documentation instead."
        ),
        doc_links=(DocLink(label="S3 Vectors limits", url=S3_VECTORS_LIMITS_URL),),
    ),
    CatalogGroup(
        id="fine_tuning",
        label="Fine-tuning",
        description=(
            "SageMaker training and Batch Transform instance quotas for the G5, G6 and G6e "
            "instance types fine-tuning offers. A new account starts most of these at 0."
        ),
        name_selectors=(("sagemaker", _matches(SAGEMAKER_INSTANCE_PATTERN)),),
        enabled=fine_tuning_enabled,
    ),
    CatalogGroup(
        id="dictation",
        label="Dictation",
        description="Composer dictation streams audio to Amazon Transcribe.",
        quotas=(
            ("transcribe", "L-0599F82B"),  # Number of concurrent streams (HTTP/2 + Websocket)
            ("transcribe", "L-00A7F8C0"),  # StartStreamTranscriptionWebSocket TPS
        ),
        enabled=dictation_enabled,
    ),
)


def enabled_groups() -> Tuple[CatalogGroup, ...]:
    """The groups whose feature is deployed, in page order."""
    return tuple(g for g in CATALOG if g.enabled())


def get_group(group_id: str) -> Optional[CatalogGroup]:
    return next((g for g in CATALOG if g.id == group_id), None)


def find_group(service_code: str, quota_code: str, quota_name: str) -> Optional[CatalogGroup]:
    """The enabled group that monitors this quota, or None.

    "Show all quotas" counts: a Bedrock quota the page can show may be raised.
    """
    for group in enabled_groups():
        if group.selects(service_code, quota_code, quota_name, all_quotas=True):
            return group
    return None


def is_monitored(service_code: str, quota_code: str, quota_name: str) -> bool:
    return find_group(service_code, quota_code, quota_name) is not None


def console_url(region: str, service_code: str, quota_code: str) -> str:
    return f"https://{region}.console.aws.amazon.com/servicequotas/home/services/" f"{service_code}/quotas/{quota_code}"


def history_console_url(region: str) -> str:
    return f"https://{region}.console.aws.amazon.com/servicequotas/home/requests"


def auto_management_console_url(region: str) -> str:
    return f"https://{region}.console.aws.amazon.com/servicequotas/home/automatic-management"
