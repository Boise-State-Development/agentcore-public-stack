"""Pydantic models for the AWS Limits admin page.

Every model serializes camelCase (``model_dump(by_alias=True)``), like the
rest of the admin API.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class DocLink(_CamelModel):
    label: str
    url: str


class AwsLimit(_CamelModel):
    """One quota as the page shows it: applied value, AWS default, usage."""

    service_code: str
    service_name: str
    quota_code: str
    quota_name: str
    description: Optional[str] = None
    applied_value: Optional[float] = None
    default_value: Optional[float] = None
    unit: Optional[str] = None
    period_value: Optional[int] = None
    period_unit: Optional[str] = None
    adjustable: bool = False
    global_quota: bool = False
    has_usage_metric: bool = False
    # Peak usage over the last 7 days, in the quota's own unit and period. None
    # when nothing measures this quota.
    usage: Optional[float] = None
    # Which CloudWatch series the usage came from, for the row's tooltip.
    usage_source: Optional[str] = None
    # usage / applied value, as a percentage. None without usage.
    utilization: Optional[float] = None
    console_url: str
    # Adjustable and in the monitored catalog. Whether the *caller* may submit
    # is separate: ``AwsLimitsDashboard.can_request_increase``.
    requestable: bool = False


class AwsLimitGroup(_CamelModel):
    """Limits grouped by the stack component that depends on them."""

    id: str
    label: str
    description: str
    limits: List[AwsLimit] = Field(default_factory=list)
    doc_links: List[DocLink] = Field(default_factory=list)
    # Set when this group's services could not be read. The other groups load.
    error: Optional[str] = None
    # Set when usage metrics could not be read; the limits still show.
    usage_error: Optional[str] = None
    fetched_at: Optional[str] = None


class AutoManagementStatus(_CamelModel):
    opt_in_status: Optional[str] = None
    opt_in_level: Optional[str] = None
    opt_in_type: Optional[str] = None
    console_url: str
    error: Optional[str] = None


class AwsLimitsSummary(_CamelModel):
    monitored: int = 0
    at_or_above_80: int = Field(0, alias="atOrAbove80")
    at_or_above_95: int = Field(0, alias="atOrAbove95")
    open_requests: int = 0


class AwsLimitsDashboard(_CamelModel):
    region: str
    generated_at: str
    # The oldest cached read on the page, so the UI can say "as of 4 min ago".
    as_of: str
    all_bedrock: bool = False
    can_request_increase: bool = False
    groups: List[AwsLimitGroup] = Field(default_factory=list)
    summary: AwsLimitsSummary = Field(default_factory=AwsLimitsSummary)
    auto_management: Optional[AutoManagementStatus] = None
    guidance_url: str
    # How far back "usage" looks.
    usage_window_days: int = 7


class IncreaseRequest(_CamelModel):
    """Body of ``POST /admin/aws-limits/requests``."""

    service_code: str = Field(..., min_length=1, max_length=64)
    quota_code: str = Field(..., min_length=1, max_length=64)
    desired_value: float = Field(..., gt=0)


class IncreaseRequestRecord(_CamelModel):
    """One Service Quotas increase request, joined to our audit log."""

    request_id: str
    service_code: str
    service_name: Optional[str] = None
    quota_code: str
    quota_name: Optional[str] = None
    desired_value: Optional[float] = None
    status: str
    request_type: Optional[str] = None
    case_id: Optional[str] = None
    created: Optional[str] = None
    last_updated: Optional[str] = None
    unit: Optional[str] = None
    # Email of the admin who asked through this app; None when the request
    # came from the console, the CLI or Automatic Management.
    requested_by: Optional[str] = None
    console_url: str


class IncreaseHistory(_CamelModel):
    region: str
    requests: List[IncreaseRequestRecord] = Field(default_factory=list)
    as_of: str
