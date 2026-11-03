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
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from botocore.exceptions import BotoCoreError, ClientError
from cachetools import TTLCache

from apis.shared.audit.models import TARGET_AWS_LIMIT_REQUEST, AuditAction
from apis.shared.audit.service import get_audit_service
from apis.shared.auth.models import User

from . import catalog
from .bedrock_match import AWS_MODEL_PROVIDERS, ModelRef, model_matches_quota
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
_USAGE_WINDOW = timedelta(hours=1)
_USAGE_PERIOD_SECONDS = 60
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
    """Multiplier turning a 60-second datapoint into the quota's own period.

    None when the quota's period is too long for a one-hour peak to mean
    anything (hourly and daily quotas), so the page shows N/A rather than a
    misleading number.
    """
    unit = (quota.get("period_unit") or "").upper()
    value = quota.get("period_value") or 1
    if not unit or statistic != "Sum":
        return 1.0
    if unit == "SECOND":
        return value / _USAGE_PERIOD_SECONDS
    if unit == "MINUTE":
        return float(value)
    return None


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

    def _usage(self, quotas: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
        """Peak usage over the last hour, keyed ``service/quota``."""
        queries: List[Dict[str, Any]] = []
        meta: Dict[str, Tuple[str, Optional[float]]] = {}
        for quota in quotas:
            metric = quota.get("usage_metric") or {}
            if not metric.get("MetricName") or not metric.get("MetricNamespace"):
                continue
            stat = metric.get("MetricStatisticRecommendation") or "Maximum"
            factor = _usage_adjustment(quota, stat)
            if factor is None:
                continue
            query_id = f"q{len(queries)}"
            meta[query_id] = (f"{quota['service_code']}/{quota['quota_code']}", factor)
            queries.append(
                {
                    "Id": query_id,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": metric["MetricNamespace"],
                            "MetricName": metric["MetricName"],
                            "Dimensions": [{"Name": k, "Value": v} for k, v in (metric.get("MetricDimensions") or {}).items()],
                        },
                        "Period": _USAGE_PERIOD_SECONDS,
                        "Stat": stat,
                    },
                    "ReturnData": True,
                }
            )
        usage: Dict[str, Optional[float]] = {}
        if not queries:
            return usage
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
                    key, factor = meta[result["Id"]]
                    values = result.get("Values") or []
                    peak = max(values) * factor if values else 0.0
                    usage[key] = max(usage.get(key) or 0.0, peak)
                token = response.get("NextToken")
                if not token:
                    break
        return usage

    # ------------------------------------------------------------------
    # Groups (sync)
    # ------------------------------------------------------------------

    def _to_limit(self, quota: Dict[str, Any], usage: Dict[str, Optional[float]]) -> AwsLimit:
        key = f"{quota['service_code']}/{quota['quota_code']}"
        has_metric = bool((quota.get("usage_metric") or {}).get("MetricName"))
        value = usage.get(key) if has_metric else None
        applied = quota.get("applied_value")
        utilization = None
        if value is not None and applied:
            utilization = round(value / applied * 100, 1)
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
            has_usage_metric=has_metric,
            usage=round(value, 2) if value is not None else None,
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
        try:
            usage = self._usage(selected)
        except (ClientError, BotoCoreError) as e:
            logger.warning("AWS Limits usage read failed for %s: %s", group.id, _error_text(e))
            usage = {}
            result.usage_error = f"Usage could not be read from CloudWatch ({_error_text(e)})."
        result.limits = [self._to_limit(q, usage) for q in selected]
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
        needs_models = not all_bedrock and any(g.filter_to_managed_models for g in groups_def)
        models = await self._models() if needs_models else None

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
            )

        return await asyncio.to_thread(build)

    async def quota_detail(self, service_code: str, quota_code: str) -> AwsLimit:
        if service_code not in catalog.MONITORED_SERVICE_CODES:
            raise AwsLimitsError(404, "That service is not monitored.")

        def load() -> AwsLimit:
            try:
                quota = self._read_quota_fresh(service_code, quota_code)
            except ClientError as e:
                raise map_client_error(e) from e
            if not catalog.is_monitored(service_code, quota_code, quota["quota_name"]):
                raise AwsLimitsError(404, "That quota is not monitored.")
            try:
                usage = self._usage([quota])
            except (ClientError, BotoCoreError) as e:
                logger.warning("AWS Limits usage read failed for %s/%s: %s", service_code, quota_code, e)
                usage = {}
            return self._to_limit(quota, usage)

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
