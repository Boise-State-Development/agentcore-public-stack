"""Service Quotas and CloudWatch reads, and increase requests, for AWS Limits.

* **Caching.** Service Quotas has low request-rate limits and the Bedrock and
  SageMaker lists run past 1,000 quotas each, so every read is cached per task
  for ``ttl_seconds`` (10 minutes). ``refresh=True`` drops the cache first.
* **Error isolation.** Each group loads on its own. A service that fails
  (throttled, denied, not in this Region) returns its group with ``error`` set
  and the rest of the page still loads.
* **Applied vs default.** ``ListServiceQuotas`` returns applied values but
  omits some quotas that were never changed (ECS "Tasks per service", for one),
  so each service's list is merged with ``ListAWSDefaultServiceQuotas``.
* **Requests.** Validated against the catalog and a fresh read before AWS is
  asked, and audited: AWS records the app-api task role as the requester, so
  the audit record is the only trace of which admin asked.

boto3 is synchronous; the async methods run the AWS work in a worker thread.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from botocore.exceptions import BotoCoreError, ClientError
from cachetools import TTLCache

from apis.shared.audit.models import TARGET_AWS_LIMIT_REQUEST, AuditAction
from apis.shared.audit.service import get_audit_service
from apis.shared.auth.models import User

from . import catalog
from .bedrock_match import AWS_MODEL_PROVIDERS, ModelRef, model_id_variants, model_matches_quota, quota_usage_metric, usage_model_ids
from .models import (
    AutoManagementStatus,
    AwsLimit,
    AwsLimitGroup,
    AwsLimitsDashboard,
    AwsLimitsSummary,
    IncreaseHistory,
    IncreaseRequest,
    IncreaseRequestRecord,
)

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 600
OPEN_STATUSES = frozenset({"PENDING", "CASE_OPENED"})
WARN_THRESHOLD = 80.0
DANGER_THRESHOLD = 95.0

# GetMetricData accepts up to 500 queries per call.
_METRIC_QUERY_BATCH = 500
# Usage is the peak of the last week: one busy hour is not a reference point,
# and 1-minute CloudWatch data is kept for 15 days.
USAGE_WINDOW_DAYS = 7
_USAGE_WINDOW = timedelta(days=USAGE_WINDOW_DAYS)
_SUM_PERIOD_SECONDS = 60
_MAX_PERIOD_SECONDS = 3600
# How far back the history join reads the audit log; records expire at 365 days.
_AUDIT_JOIN_MAX_MONTHS = 13
_AUDIT_JOIN_MAX_PAGES_PER_MONTH = 20


class AwsLimitsError(Exception):
    """A request the route turns into an HTTP error with ``status_code``."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _stamp(value)
    return str(value)


def _error_text(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        code = err.get("Code", "Error")
        message = err.get("Message") or str(exc)
        return f"{code}: {message}"
    return str(exc) or exc.__class__.__name__


def map_client_error(exc: ClientError) -> AwsLimitsError:
    """The HTTP answer for an AWS error from ``RequestServiceQuotaIncrease``."""
    err = exc.response.get("Error", {})
    code = err.get("Code", "")
    message = err.get("Message") or "AWS rejected the request."
    if code == "ResourceAlreadyExistsException":
        return AwsLimitsError(409, f"An increase request for this quota is already open. {message}".strip())
    if code in ("TooManyRequestsException", "QuotaExceededException"):
        return AwsLimitsError(429, f"AWS is limiting requests right now. Try again shortly. {message}".strip())
    if code == "DependencyAccessDeniedException":
        return AwsLimitsError(
            502,
            "Service Quotas could not use its service-linked role "
            "(AWSServiceRoleForServiceQuotas). The first increase request in an account "
            "creates that role, which needs iam:CreateServiceLinkedRole for "
            "servicequotas.amazonaws.com. Check the App API role's AwsLimitsServiceLinkedRole "
            f"grant, or create the role once from the Service Quotas console. AWS said: {message}",
        )
    if code in ("NoSuchResourceException", "IllegalArgumentException", "InvalidResourceStateException"):
        return AwsLimitsError(400, message)
    if code == "AccessDeniedException":
        return AwsLimitsError(403, f"The App API role is not allowed to request this increase. {message}")
    return AwsLimitsError(502, f"Service Quotas returned {code or 'an error'}: {message}")


def _merge_quota(applied: Optional[Dict[str, Any]], default: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    base = applied or default or {}
    period = base.get("Period") or {}
    return {
        "service_code": base.get("ServiceCode", ""),
        "service_name": base.get("ServiceName", ""),
        "quota_code": base.get("QuotaCode", ""),
        "quota_name": base.get("QuotaName", ""),
        "description": base.get("Description") or (default or {}).get("Description"),
        "applied_value": (applied or default or {}).get("Value"),
        "default_value": (default or {}).get("Value"),
        "unit": base.get("Unit"),
        "period_value": period.get("PeriodValue"),
        "period_unit": period.get("PeriodUnit"),
        "adjustable": bool(base.get("Adjustable")),
        "global_quota": bool(base.get("GlobalQuota")),
        "usage_metric": base.get("UsageMetric") or (default or {}).get("UsageMetric") or None,
    }


def _usage_adjustment(quota: Dict[str, Any], statistic: str) -> Optional[float]:
    """Multiplier turning a datapoint into the quota's own period.

    A Sum is read per minute; a per-second quota divides it by 60 (the busiest
    minute's average rate). None when the quota's period is longer than a
    minute (hourly and daily quotas), which a per-minute peak cannot answer.
    """
    unit = (quota.get("period_unit") or "").upper()
    value = quota.get("period_value") or 1
    if not unit or statistic != "Sum":
        return 1.0
    if unit == "SECOND":
        return value / _SUM_PERIOD_SECONDS
    if unit == "MINUTE":
        return float(value)
    return None


@dataclass(frozen=True)
class _Series:
    namespace: str
    metric_name: str
    dimensions: Tuple[Tuple[str, str], ...]
    stat: str

    @property
    def period(self) -> int:
        # The Maximum of hourly Maximums is the Maximum of every sample, so a
        # gauge loses nothing at an hour. A Sum must stay per minute: an hour's
        # total is not the busiest minute.
        return _MAX_PERIOD_SECONDS if self.stat == "Maximum" else _SUM_PERIOD_SECONDS


@dataclass(frozen=True)
class _UsagePlan:
    """How one quota's usage is measured: the series summed per timestamp, then peaked."""

    series: Tuple[_Series, ...]
    factor: float
    source: str


def _service_quotas_plan(quota: Dict[str, Any]) -> Optional[_UsagePlan]:
    metric = quota.get("usage_metric") or {}
    if not metric.get("MetricName") or not metric.get("MetricNamespace"):
        return None
    stat = metric.get("MetricStatisticRecommendation") or "Maximum"
    factor = _usage_adjustment(quota, stat)
    if factor is None:
        return None
    dims = tuple(sorted((metric.get("MetricDimensions") or {}).items()))
    return _UsagePlan(
        series=(_Series(metric["MetricNamespace"], metric["MetricName"], dims, stat),),
        factor=factor,
        source=f"{metric['MetricNamespace']} {metric['MetricName']} ({stat})",
    )


def _with_variants(models: List[ModelRef], region: str) -> List[ModelRef]:
    """Each catalog model's display name, lent to every id it can be invoked as."""
    return [ModelRef(model_id=v, model_name=m.model_name, provider=m.provider) for m in models for v in model_id_variants(m.model_id, region)]


def _bedrock_plan(quota: Dict[str, Any], model_ids: List[str], models: List[ModelRef]) -> Optional[_UsagePlan]:
    name = quota.get("quota_name") or ""
    metric_name = quota_usage_metric(name)
    if metric_name is None:
        return None
    ids = usage_model_ids(name, model_ids, models)
    if not ids:
        return None
    return _UsagePlan(
        series=tuple(_Series("AWS/Bedrock", metric_name, (("ModelId", i),), "Sum") for i in ids),
        factor=1.0,
        source=f"AWS/Bedrock {metric_name} per minute for {', '.join(ids)}",
    )


async def _load_managed_models() -> List[ModelRef]:
    from apis.shared.models.managed_models import list_all_managed_models

    models = await list_all_managed_models()
    return [ModelRef(model_id=m.model_id, model_name=m.model_name, provider=m.provider) for m in models if m.provider.lower() in AWS_MODEL_PROVIDERS]


class AwsLimitsService:
    def __init__(
        self,
        *,
        region: Optional[str] = None,
        quotas_client: Any = None,
        cloudwatch_client: Any = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        model_loader: Optional[Callable[[], Awaitable[List[ModelRef]]]] = None,
    ):
        self.region = region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"
        self._quotas = quotas_client
        self._cloudwatch = cloudwatch_client
        self._model_loader = model_loader or _load_managed_models
        self._cache: TTLCache = TTLCache(maxsize=256, ttl=max(1, ttl_seconds))
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Clients and cache
    # ------------------------------------------------------------------

    @property
    def quotas(self) -> Any:
        if self._quotas is None:
            import boto3

            self._quotas = boto3.client("service-quotas", region_name=self.region)
        return self._quotas

    @property
    def cloudwatch(self) -> Any:
        if self._cloudwatch is None:
            import boto3

            self._cloudwatch = boto3.client("cloudwatch", region_name=self.region)
        return self._cloudwatch

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def _cached(self, key: Tuple[Any, ...], load: Callable[[], Any]) -> Tuple[Any, str]:
        """``(value, fetched_at)``; loads (outside the lock) on a miss."""
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None:
            return hit
        value = load()
        entry = (value, _stamp(_now()))
        with self._lock:
            self._cache[key] = entry
        return entry

    def _invalidate(self, *keys: Tuple[Any, ...]) -> None:
        with self._lock:
            for key in keys:
                self._cache.pop(key, None)
            # Group results embed the service snapshots, so drop those too.
            for key in [k for k in self._cache.keys() if k and k[0] == "group"]:
                self._cache.pop(key, None)

    # ------------------------------------------------------------------
    # Service Quotas reads (sync)
    # ------------------------------------------------------------------

    def _paginate(self, operation: str, result_key: str, **params: Any) -> List[Dict[str, Any]]:
        paginator = self.quotas.get_paginator(operation)
        items: List[Dict[str, Any]] = []
        for page in paginator.paginate(**params, PaginationConfig={"PageSize": 100}):
            items.extend(page.get(result_key, []))
        return items

    def _service_snapshot(self, service_code: str) -> Tuple[Dict[str, Dict[str, Any]], str]:
        def load() -> Dict[str, Dict[str, Any]]:
            applied = {q["QuotaCode"]: q for q in self._paginate("list_service_quotas", "Quotas", ServiceCode=service_code)}
            defaults = {q["QuotaCode"]: q for q in self._paginate("list_aws_default_service_quotas", "Quotas", ServiceCode=service_code)}
            return {code: _merge_quota(applied.get(code), defaults.get(code)) for code in dict.fromkeys([*applied, *defaults])}

        return self._cached(("service", service_code), load)

    def _read_quota_fresh(self, service_code: str, quota_code: str) -> Dict[str, Any]:
        """Applied value straight from AWS, with the default for comparison."""
        try:
            default = self.quotas.get_aws_default_service_quota(ServiceCode=service_code, QuotaCode=quota_code).get("Quota")
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "NoSuchResourceException":
                raise
            default = None
        try:
            applied = self.quotas.get_service_quota(ServiceCode=service_code, QuotaCode=quota_code).get("Quota")
        except ClientError as e:
            # Never changed from the default: Service Quotas has no applied row.
            if e.response.get("Error", {}).get("Code") != "NoSuchResourceException" or default is None:
                raise
            applied = None
        return _merge_quota(applied, default)

    # ------------------------------------------------------------------
    # Usage (sync)
    # ------------------------------------------------------------------

    def _bedrock_model_ids(self) -> List[str]:
        """ModelIds Bedrock has published metrics for in the last two weeks."""

        def load() -> List[str]:
            paginator = self.cloudwatch.get_paginator("list_metrics")
            ids: List[str] = []
            for page in paginator.paginate(Namespace="AWS/Bedrock", MetricName="Invocations"):
                for metric in page.get("Metrics", []):
                    dims = metric.get("Dimensions") or []
                    if len(dims) == 1 and dims[0].get("Name") == "ModelId":
                        ids.append(dims[0]["Value"])
            return sorted(set(ids))

        value, _ = self._cached(("bedrock_model_ids",), load)
        return list(value)

    def _plans(self, quotas: List[Dict[str, Any]], models: Optional[List[ModelRef]]) -> Dict[str, _UsagePlan]:
        """``service/quota`` -> how to measure it, for every quota that can be measured."""
        plans: Dict[str, _UsagePlan] = {}
        bedrock_ids: Optional[List[str]] = None
        for quota in quotas:
            key = f"{quota['service_code']}/{quota['quota_code']}"
            plan = _service_quotas_plan(quota)
            if plan is None and quota["service_code"] == "bedrock" and quota_usage_metric(quota.get("quota_name") or ""):
                if bedrock_ids is None:
                    try:
                        listed = self._bedrock_model_ids()
                    except (ClientError, BotoCoreError) as e:
                        logger.warning("AWS Limits could not list Bedrock metrics: %s", _error_text(e))
                        listed = []
                    catalog_ids = {v for m in models or [] for v in model_id_variants(m.model_id, self.region)}
                    bedrock_ids = sorted(set(listed) | catalog_ids)
                plan = _bedrock_plan(quota, bedrock_ids, _with_variants(models or [], self.region))
            if plan is not None:
                plans[key] = plan
        return plans

    def _usage(self, plans: Dict[str, _UsagePlan]) -> Dict[str, float]:
        """Peak usage over the last ``USAGE_WINDOW_DAYS`` days, keyed like ``plans``.

        Each distinct series is read once; a quota measured by several series
        (one Bedrock model invoked by two ModelIds) sums them per timestamp
        before taking the peak. A series with no datapoints is zero use.
        """
        series_ids: Dict[_Series, str] = {}
        for plan in plans.values():
            for series in plan.series:
                series_ids.setdefault(series, f"q{len(series_ids)}")
        if not series_ids:
            return {}
        queries = [
            {
                "Id": query_id,
                "MetricStat": {
                    "Metric": {
                        "Namespace": series.namespace,
                        "MetricName": series.metric_name,
                        "Dimensions": [{"Name": k, "Value": v} for k, v in series.dimensions],
                    },
                    "Period": series.period,
                    "Stat": series.stat,
                },
                "ReturnData": True,
            }
            for series, query_id in series_ids.items()
        ]
        points: Dict[str, Dict[Any, float]] = {query_id: {} for query_id in series_ids.values()}
        end = _now()
        start = end - _USAGE_WINDOW
        for offset in range(0, len(queries), _METRIC_QUERY_BATCH):
            batch = queries[offset : offset + _METRIC_QUERY_BATCH]
            token: Optional[str] = None
            while True:
                params: Dict[str, Any] = {"MetricDataQueries": batch, "StartTime": start, "EndTime": end}
                if token:
                    params["NextToken"] = token
                response = self.cloudwatch.get_metric_data(**params)
                for result in response.get("MetricDataResults", []):
                    values = result.get("Values") or []
                    stamps = result.get("Timestamps") or list(range(len(values)))
                    bucket = points[result["Id"]]
                    for stamp, value in zip(stamps, values):
                        bucket[stamp] = bucket.get(stamp, 0.0) + value
                token = response.get("NextToken")
                if not token:
                    break

        usage: Dict[str, float] = {}
        for key, plan in plans.items():
            combined: Dict[Any, float] = {}
            for series in plan.series:
                for stamp, value in points[series_ids[series]].items():
                    combined[stamp] = combined.get(stamp, 0.0) + value
            usage[key] = (max(combined.values()) if combined else 0.0) * plan.factor
        return usage

    # ------------------------------------------------------------------
    # Groups (sync)
    # ------------------------------------------------------------------

    def _to_limit(self, quota: Dict[str, Any], usage: Dict[str, float], plans: Dict[str, _UsagePlan]) -> AwsLimit:
        key = f"{quota['service_code']}/{quota['quota_code']}"
        plan = plans.get(key)
        value = usage.get(key) if plan is not None else None
        applied = quota.get("applied_value")
        utilization = None
        if value is not None and applied:
            # Three places, so 0.02% stays 0.02% and the UI can say "<0.1%" rather than "0%".
            utilization = round(value / applied * 100, 3)
        return AwsLimit(
            service_code=quota["service_code"],
            service_name=quota.get("service_name") or quota["service_code"],
            quota_code=quota["quota_code"],
            quota_name=quota.get("quota_name") or quota["quota_code"],
            description=quota.get("description"),
            applied_value=applied,
            default_value=quota.get("default_value"),
            unit=quota.get("unit"),
            period_value=quota.get("period_value"),
            period_unit=quota.get("period_unit"),
            adjustable=quota.get("adjustable", False),
            global_quota=quota.get("global_quota", False),
            has_usage_metric=plan is not None,
            usage=round(value, 4) if value is not None else None,
            usage_source=plan.source if plan is not None else None,
            utilization=utilization,
            console_url=catalog.console_url(self.region, quota["service_code"], quota["quota_code"]),
            requestable=bool(quota.get("adjustable")),
        )

    def _build_group(self, group: catalog.CatalogGroup, *, all_bedrock: bool, models: Optional[List[ModelRef]]) -> AwsLimitGroup:
        selected: List[Dict[str, Any]] = []
        fetched: List[str] = []
        for service_code in group.service_codes:
            snapshot, fetched_at = self._service_snapshot(service_code)
            fetched.append(fetched_at)
            for quota in snapshot.values():
                if not group.selects(service_code, quota["quota_code"], quota["quota_name"], all_quotas=all_bedrock):
                    continue
                if group.filter_to_managed_models and not all_bedrock:
                    if not any(model_matches_quota(m, quota["quota_name"]) for m in models or []):
                        continue
                selected.append(quota)
        # Keep the catalog's order for code-listed groups.
        order = {code: i for i, (_, code) in enumerate(group.quotas)}
        selected.sort(key=lambda q: (order.get(q["quota_code"], len(order)), q["quota_name"]))

        result = AwsLimitGroup(
            id=group.id,
            label=group.label,
            description=group.description,
            doc_links=list(group.doc_links),
            fetched_at=min(fetched) if fetched else _stamp(_now()),
        )
        plans = self._plans(selected, models)
        try:
            usage = self._usage(plans)
        except (ClientError, BotoCoreError) as e:
            logger.warning("AWS Limits usage read failed for %s: %s", group.id, _error_text(e))
            usage = {}
            plans = {}
            result.usage_error = f"Usage could not be read from CloudWatch ({_error_text(e)})."
        result.limits = [self._to_limit(q, usage, plans) for q in selected]
        return result

    def _group(self, group: catalog.CatalogGroup, *, all_bedrock: bool, models: Optional[List[ModelRef]]) -> AwsLimitGroup:
        flag = all_bedrock and bool(group.show_all_services)
        try:
            value, _ = self._cached(
                ("group", group.id, flag),
                lambda: self._build_group(group, all_bedrock=flag, models=models),
            )
            return value
        except Exception as e:  # noqa: BLE001 — isolate one group
            logger.warning("AWS Limits group %s failed: %s", group.id, _error_text(e))
            return AwsLimitGroup(
                id=group.id,
                label=group.label,
                description=group.description,
                doc_links=list(group.doc_links),
                error=f"Could not read {', '.join(group.service_codes)} quotas: {_error_text(e)}",
                fetched_at=_stamp(_now()),
            )

    def _auto_management(self) -> AutoManagementStatus:
        url = catalog.auto_management_console_url(self.region)
        try:
            value, _ = self._cached(("auto",), lambda: self.quotas.get_auto_management_configuration())
        except (ClientError, BotoCoreError) as e:
            return AutoManagementStatus(console_url=url, error=_error_text(e))
        return AutoManagementStatus(
            opt_in_status=value.get("OptInStatus"),
            opt_in_level=value.get("OptInLevel"),
            opt_in_type=value.get("OptInType"),
            console_url=url,
        )

    # ------------------------------------------------------------------
    # History (sync)
    # ------------------------------------------------------------------

    def _history_raw(self) -> Tuple[List[Dict[str, Any]], str]:
        return self._cached(
            ("history",),
            lambda: self._paginate("list_requested_service_quota_change_history", "RequestedQuotas"),
        )

    def _requesters(self, oldest: Optional[datetime]) -> Dict[str, str]:
        """AWS request id -> the email of the admin who asked through this app."""
        audit = get_audit_service()
        if not audit.configured:
            return {}
        today = _now().date()
        start = (oldest.date() if oldest else today).replace(day=1)
        earliest = (today.replace(day=1) - timedelta(days=31 * (_AUDIT_JOIN_MAX_MONTHS - 1))).replace(day=1)
        month = max(start, earliest)
        months: List[str] = []
        while month <= today:
            months.append(month.strftime("%Y-%m"))
            month = (month + timedelta(days=32)).replace(day=1)
        found: Dict[str, str] = {}
        for m in months:
            cursor = None
            for _ in range(_AUDIT_JOIN_MAX_PAGES_PER_MONTH):
                records, cursor = audit.repository.list_recent(m, limit=100, cursor=cursor)
                for record in records:
                    if record.action == AuditAction.AWS_LIMIT_INCREASE_REQUESTED and record.target_id:
                        found.setdefault(record.target_id, record.actor_email or record.actor_user_id)
                if not cursor:
                    break
        return found

    def _to_record(self, raw: Dict[str, Any], requesters: Dict[str, str]) -> IncreaseRequestRecord:
        request_id = raw.get("Id", "")
        return IncreaseRequestRecord(
            request_id=request_id,
            service_code=raw.get("ServiceCode", ""),
            service_name=raw.get("ServiceName"),
            quota_code=raw.get("QuotaCode", ""),
            quota_name=raw.get("QuotaName"),
            desired_value=raw.get("DesiredValue"),
            status=raw.get("Status", "PENDING"),
            request_type=raw.get("RequestType"),
            case_id=raw.get("CaseId") or None,
            created=_iso(raw.get("Created")),
            last_updated=_iso(raw.get("LastUpdated")),
            unit=raw.get("Unit"),
            requested_by=requesters.get(request_id),
            console_url=catalog.history_console_url(self.region),
        )

    def _history(self) -> IncreaseHistory:
        raw, fetched_at = self._history_raw()
        created = [r["Created"] for r in raw if isinstance(r.get("Created"), datetime)]
        try:
            requesters = self._requesters(min(created) if created else None)
        except Exception as e:  # noqa: BLE001 — the history still shows without names
            logger.warning("AWS Limits audit join failed: %s", _error_text(e))
            requesters = {}
        records = [self._to_record(r, requesters) for r in raw]
        records.sort(key=lambda r: r.created or "", reverse=True)
        return IncreaseHistory(region=self.region, requests=records, as_of=fetched_at)

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def _models(self) -> Optional[List[ModelRef]]:
        try:
            return await self._model_loader()
        except Exception as e:  # noqa: BLE001 — show the unfiltered-empty state, not a 500
            logger.warning("AWS Limits could not load managed models: %s", e)
            return None

    async def dashboard(self, *, all_bedrock: bool = False, refresh: bool = False, can_request_increase: bool = False) -> AwsLimitsDashboard:
        if refresh:
            self.clear_cache()
        groups_def = catalog.enabled_groups()
        # The catalog filters the default Bedrock view and also supplies ModelIds
        # for usage, so it is read with "Show all" on too.
        has_bedrock = any(g.filter_to_managed_models for g in groups_def)
        needs_models = has_bedrock and not all_bedrock
        models = await self._models() if has_bedrock else None

        def build() -> AwsLimitsDashboard:
            groups = [self._group(g, all_bedrock=all_bedrock, models=models) for g in groups_def]
            if needs_models and models is None:
                for g in groups:
                    if g.id == "model_inference" and not g.error:
                        g.error = (
                            "The managed-model catalog could not be read, so no models were "
                            'matched. Turn on "Show all quotas" to see every Bedrock quota.'
                        )
            limits = [limit for g in groups for limit in g.limits]
            summary = AwsLimitsSummary(
                monitored=len(limits),
                at_or_above_80=sum(1 for x in limits if (x.utilization or 0) >= WARN_THRESHOLD),
                at_or_above_95=sum(1 for x in limits if (x.utilization or 0) >= DANGER_THRESHOLD),
            )
            try:
                raw, _ = self._history_raw()
                summary.open_requests = sum(1 for r in raw if r.get("Status") in OPEN_STATUSES)
            except (ClientError, BotoCoreError) as e:
                logger.warning("AWS Limits history read failed: %s", _error_text(e))
            stamps = [g.fetched_at for g in groups if g.fetched_at]
            now = _stamp(_now())
            return AwsLimitsDashboard(
                region=self.region,
                generated_at=now,
                as_of=min(stamps) if stamps else now,
                all_bedrock=all_bedrock,
                can_request_increase=can_request_increase,
                groups=groups,
                summary=summary,
                auto_management=self._auto_management(),
                guidance_url=catalog.GUIDANCE_URL,
                usage_window_days=USAGE_WINDOW_DAYS,
            )

        return await asyncio.to_thread(build)

    async def quota_detail(self, service_code: str, quota_code: str) -> AwsLimit:
        if service_code not in catalog.MONITORED_SERVICE_CODES:
            raise AwsLimitsError(404, "That service is not monitored.")

        models = await self._models() if service_code == "bedrock" else None

        def load() -> AwsLimit:
            try:
                quota = self._read_quota_fresh(service_code, quota_code)
            except ClientError as e:
                raise map_client_error(e) from e
            if not catalog.is_monitored(service_code, quota_code, quota["quota_name"]):
                raise AwsLimitsError(404, "That quota is not monitored.")
            plans = self._plans([quota], models)
            try:
                usage = self._usage(plans)
            except (ClientError, BotoCoreError) as e:
                logger.warning("AWS Limits usage read failed for %s/%s: %s", service_code, quota_code, e)
                usage, plans = {}, {}
            return self._to_limit(quota, usage, plans)

        return await asyncio.to_thread(load)

    async def history(self, *, refresh: bool = False) -> IncreaseHistory:
        if refresh:
            self._invalidate(("history",))
        try:
            return await asyncio.to_thread(self._history)
        except (ClientError, BotoCoreError) as e:
            raise AwsLimitsError(502, f"Could not read the increase history: {_error_text(e)}") from e

    async def request_increase(self, admin: User, body: IncreaseRequest) -> IncreaseRequestRecord:
        service_code, quota_code = body.service_code, body.quota_code
        if service_code not in catalog.MONITORED_SERVICE_CODES:
            raise AwsLimitsError(400, "That service is not monitored, so it cannot be raised from here.")

        def submit() -> IncreaseRequestRecord:
            try:
                quota = self._read_quota_fresh(service_code, quota_code)
            except ClientError as e:
                raise map_client_error(e) from e
            if not catalog.is_monitored(service_code, quota_code, quota["quota_name"]):
                raise AwsLimitsError(400, "That quota is not monitored, so it cannot be raised from here.")
            if not quota["adjustable"]:
                raise AwsLimitsError(
                    400,
                    "AWS does not allow this quota to be raised through Service Quotas. " "Open an AWS Support case instead.",
                )
            current = quota.get("applied_value")
            if current is not None and body.desired_value <= current:
                raise AwsLimitsError(400, f"The new value must be greater than the applied value ({current:g}).")
            try:
                response = self.quotas.request_service_quota_increase(
                    ServiceCode=service_code,
                    QuotaCode=quota_code,
                    DesiredValue=body.desired_value,
                    # When the increase needs manual review, Service Quotas opens
                    # the support case itself through its service-linked role.
                    # False would turn that case into NOT_APPROVED.
                    SupportCaseAllowed=True,
                )
            except ClientError as e:
                raise map_client_error(e) from e
            raw = response.get("RequestedQuota") or {}
            request_id = raw.get("Id", "")
            get_audit_service().record(
                action=AuditAction.AWS_LIMIT_INCREASE_REQUESTED,
                actor=admin,
                target_type=TARGET_AWS_LIMIT_REQUEST,
                target_id=request_id or f"{service_code}/{quota_code}",
                changes=["desiredValue"],
                before={"value": current},
                after={
                    "serviceCode": service_code,
                    "quotaCode": quota_code,
                    "quotaName": quota.get("quota_name"),
                    "region": self.region,
                    "currentValue": current,
                    "desiredValue": body.desired_value,
                    "requestId": request_id,
                    "status": raw.get("Status"),
                    "caseId": raw.get("CaseId") or None,
                },
            )
            logger.info(
                "AWS limit increase requested",
                extra={
                    "event": "aws_limit_increase_requested",
                    "service_code": service_code,
                    "quota_code": quota_code,
                    "desired_value": body.desired_value,
                    "request_id": request_id,
                    "actor_user_id": admin.user_id,
                },
            )
            self._invalidate(("history",), ("service", service_code))
            return self._to_record(
                {
                    "ServiceCode": service_code,
                    "QuotaCode": quota_code,
                    "QuotaName": quota.get("quota_name"),
                    "ServiceName": quota.get("service_name"),
                    **raw,
                },
                {request_id: admin.email} if request_id else {},
            )

        return await asyncio.to_thread(submit)


_service: Optional[AwsLimitsService] = None
_service_lock = threading.Lock()


def get_aws_limits_service() -> AwsLimitsService:
    """Process-wide service, so the cache is per task."""
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = AwsLimitsService()
    return _service


def reset_aws_limits_service() -> None:
    """For tests."""
    global _service
    _service = None
