"""AWS Limits admin page: service and routes (GitHub issue #12).

The service is exercised against botocore ``Stubber`` so the Service Quotas
and CloudWatch request and response shapes are checked against the real API
models: pagination, the applied/default merge, usage, per-group error
isolation, POST validation and error mapping, and the audit record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

import boto3
import pytest
from botocore.stub import ANY, Stubber
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.aws_limits import routes as aws_routes
from apis.shared.audit.models import AuditRecord
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.aws_limits import catalog
from apis.shared.aws_limits import service as service_module
from apis.shared.aws_limits.bedrock_match import ModelRef
from apis.shared.aws_limits.models import IncreaseRequest
from apis.shared.aws_limits.service import AwsLimitsError, AwsLimitsService

REGION = "us-west-2"
ADMIN = User(email="admin@example.edu", user_id="admin-1", name="Admin", roles=["Admin"])


# ── fixtures ────────────────────────────────────────────────────────────────


def _quota(service: str, code: str, name: str, value: float, **extra: Any) -> Dict[str, Any]:
    return {
        "ServiceCode": service,
        "ServiceName": service.upper(),
        "QuotaCode": code,
        "QuotaName": name,
        "Value": value,
        "Adjustable": extra.pop("adjustable", True),
        "GlobalQuota": False,
        **extra,
    }


LAMBDA_CONCURRENCY = "L-B99A9384"
LAMBDA_USAGE = {
    "MetricNamespace": "AWS/Lambda",
    "MetricName": "ConcurrentExecutions",
    "MetricDimensions": {},
    "MetricStatisticRecommendation": "Maximum",
}


@dataclass
class _AuditRepo:
    records: List[AuditRecord] = field(default_factory=list)
    months: List[str] = field(default_factory=list)

    def list_recent(self, month, limit=50, cursor=None):
        self.months.append(month)
        return [r for r in self.records if r.timestamp.startswith(month)], None


class _Audit:
    def __init__(self, configured: bool = True):
        self.configured = configured
        self.records: List[dict] = []
        self.repository = _AuditRepo()

    def record(self, **kw):
        self.records.append(kw)
        return None


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_LIMITS_ENABLED", "true")
    monkeypatch.setenv("FINE_TUNING_ENABLED", "true")
    monkeypatch.setenv("DICTATION_ENABLED", "true")
    quotas = boto3.client("service-quotas", region_name=REGION)
    cloudwatch = boto3.client("cloudwatch", region_name=REGION)
    audit = _Audit()
    monkeypatch.setattr(service_module, "get_audit_service", lambda: audit)
    models: List[ModelRef] = []

    async def load_models():
        return models

    service = AwsLimitsService(region=REGION, quotas_client=quotas, cloudwatch_client=cloudwatch, model_loader=load_models)
    with Stubber(quotas) as sq, Stubber(cloudwatch) as scw:
        yield SimpleNamespace(service=service, sq=sq, scw=scw, audit=audit, models=models)
        sq.assert_no_pending_responses()
        scw.assert_no_pending_responses()


def _only_groups(monkeypatch, *ids: str) -> None:
    monkeypatch.setattr(catalog, "CATALOG", tuple(g for g in catalog.CATALOG if g.id in ids))


_METRIC_PARAMS = {"MetricDataQueries": ANY, "StartTime": ANY, "EndTime": ANY}


def _stub_service(sq: Stubber, service: str, applied_pages: List[List[dict]], defaults: List[dict]) -> None:
    for i, page in enumerate(applied_pages):
        response: Dict[str, Any] = {"Quotas": page}
        if i < len(applied_pages) - 1:
            response["NextToken"] = f"t{i + 1}"
        params: Dict[str, Any] = {"ServiceCode": service, "MaxResults": 100}
        if i:
            params["NextToken"] = f"t{i}"
        sq.add_response("list_service_quotas", response, params)
    sq.add_response("list_aws_default_service_quotas", {"Quotas": defaults}, {"ServiceCode": service, "MaxResults": 100})


def _stub_history(sq: Stubber, requests: List[dict]) -> None:
    sq.add_response("list_requested_service_quota_change_history", {"RequestedQuotas": requests}, {"MaxResults": 100})


def _stub_auto(sq: Stubber, status: str = "DISABLED") -> None:
    sq.add_response("get_auto_management_configuration", {"OptInStatus": status}, {})


def _client_error(sq: Stubber, operation: str, code: str, message: str = "nope") -> None:
    sq.add_client_error(operation, service_error_code=code, service_message=message, http_status_code=400)


# ── reads ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_paginates_merges_defaults_and_reads_usage(aws, monkeypatch):
    _only_groups(monkeypatch, "background_jobs", "app_api")
    concurrency = _quota("lambda", LAMBDA_CONCURRENCY, "Concurrent executions", 1000, UsageMetric=LAMBDA_USAGE)
    _stub_service(
        aws.sq,
        "lambda",
        [[_quota("lambda", "L-9FEEFFC0", "Function timeout", 900, adjustable=False)], [concurrency]],
        [{**concurrency, "Value": 1000}],
    )
    aws.scw.add_response(
        "get_metric_data",
        {"MetricDataResults": [{"Id": "q0", "Values": [120.0, 850.0, 400.0], "StatusCode": "Complete"}]},
        {"MetricDataQueries": ANY, "StartTime": ANY, "EndTime": ANY},
    )
    # ECS "Tasks per service" is absent from the applied list until it is changed.
    fargate = _quota("fargate", "L-3032A538", "Fargate On-Demand vCPU resource count", 512)
    _stub_service(aws.sq, "fargate", [[fargate]], [{**fargate, "Value": 6}])
    tasks = _quota("ecs", "L-92E49DE3", "Tasks per service", 5000)
    _stub_service(aws.sq, "ecs", [[]], [tasks])
    _stub_history(
        aws.sq,
        [
            {"Id": "r1", "ServiceCode": "lambda", "QuotaCode": LAMBDA_CONCURRENCY, "Status": "PENDING"},
            {"Id": "r2", "ServiceCode": "lambda", "QuotaCode": LAMBDA_CONCURRENCY, "Status": "APPROVED"},
        ],
    )
    _stub_auto(aws.sq)

    dashboard = await aws.service.dashboard(can_request_increase=True)

    jobs, app_api = dashboard.groups
    assert [q.quota_code for q in jobs.limits] == [LAMBDA_CONCURRENCY]
    limit = jobs.limits[0]
    assert limit.usage == 850.0 and limit.utilization == 85.0 and limit.has_usage_metric
    assert limit.usage_source == "AWS/Lambda ConcurrentExecutions (Maximum)"
    assert limit.console_url.endswith("/services/lambda/quotas/L-B99A9384")
    assert {q.quota_code: (q.applied_value, q.default_value) for q in app_api.limits} == {
        "L-3032A538": (512, 6),
        "L-92E49DE3": (5000, 5000),
    }
    assert all(q.usage is None and q.utilization is None for q in app_api.limits)
    assert dashboard.summary.model_dump() == {
        "monitored": 3,
        "at_or_above_80": 1,
        "at_or_above_95": 0,
        "open_requests": 1,
    }
    assert dashboard.auto_management.opt_in_status == "DISABLED"
    assert dashboard.can_request_increase is True
    body = dashboard.model_dump(by_alias=True, mode="json")
    assert body["summary"]["atOrAbove80"] == 1 and body["groups"][0]["limits"][0]["appliedValue"] == 1000

    # Cached: a second read makes no AWS calls (the Stubbers would fail on one).
    again = await aws.service.dashboard()
    assert again.as_of == dashboard.as_of


@pytest.mark.asyncio
async def test_one_failing_service_does_not_break_the_page(aws, monkeypatch):
    _only_groups(monkeypatch, "background_jobs", "data")
    _client_error(aws.sq, "list_service_quotas", "AccessDeniedException", "not allowed")
    tables = _quota("dynamodb", "L-F98FE922", "Maximum number of tables", 2500)
    _stub_service(aws.sq, "dynamodb", [[tables]], [tables])
    _client_error(aws.sq, "list_requested_service_quota_change_history", "TooManyRequestsException")
    _client_error(aws.sq, "get_auto_management_configuration", "AccessDeniedException")

    dashboard = await aws.service.dashboard()

    jobs, data = dashboard.groups
    assert jobs.limits == [] and "AccessDeniedException" in jobs.error
    assert data.error is None and [q.quota_code for q in data.limits] == ["L-F98FE922"]
    assert dashboard.summary.open_requests == 0
    assert dashboard.auto_management.error


@pytest.mark.asyncio
async def test_usage_failure_keeps_the_limits(aws, monkeypatch):
    _only_groups(monkeypatch, "background_jobs")
    concurrency = _quota("lambda", LAMBDA_CONCURRENCY, "Concurrent executions", 1000, UsageMetric=LAMBDA_USAGE)
    _stub_service(aws.sq, "lambda", [[concurrency]], [concurrency])
    _client_error(aws.scw, "get_metric_data", "Throttling")
    _stub_history(aws.sq, [])
    _stub_auto(aws.sq)

    (jobs,) = (await aws.service.dashboard()).groups
    assert jobs.error is None and jobs.usage_error
    assert jobs.limits[0].usage is None


@pytest.mark.asyncio
async def test_rate_quota_usage_is_converted_to_its_period(aws, monkeypatch):
    _only_groups(monkeypatch, "agent_runtime")
    rate = _quota(
        "bedrock-agentcore",
        "L-8EE2AEA2",
        "Rate of new Runtime session creation",
        25,
        UsageMetric={
            "MetricNamespace": "AWS/Usage",
            "MetricName": "CallCount",
            "MetricDimensions": {"Service": "Bedrock AgentCore", "Resource": "CombinedNewSessionCreation"},
            "MetricStatisticRecommendation": "Sum",
        },
        Period={"PeriodValue": 1, "PeriodUnit": "SECOND"},
    )
    _stub_service(aws.sq, "bedrock-agentcore", [[rate]], [rate])
    aws.scw.add_response(
        "get_metric_data",
        {"MetricDataResults": [{"Id": "q0", "Values": [600.0], "StatusCode": "Complete"}]},
        {"MetricDataQueries": ANY, "StartTime": ANY, "EndTime": ANY},
    )
    _stub_history(aws.sq, [])
    _stub_auto(aws.sq)

    (runtime,) = (await aws.service.dashboard()).groups
    # 600 calls in a 60-second datapoint is 10 per second, 40% of 25/s.
    assert runtime.limits[0].usage == 10.0 and runtime.limits[0].utilization == 40.0


@pytest.mark.asyncio
async def test_bedrock_is_filtered_to_managed_models_unless_all_are_shown(aws, monkeypatch):
    _only_groups(monkeypatch, "model_inference")
    haiku = _quota("bedrock", "L-1", "Cross-region model inference tokens per minute for Anthropic Claude Haiku 4.5", 1e6)
    opus = _quota("bedrock", "L-2", "Cross-region model inference tokens per minute for Anthropic Claude Opus 4.1", 1e6)
    batch = _quota("bedrock", "L-3", "Batch inference job size (in GB) for Claude Haiku 4.5", 5, adjustable=False)
    aws.models.append(ModelRef(model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0", model_name="Claude Haiku 4.5"))

    _stub_service(aws.sq, "bedrock", [[haiku, opus, batch]], [haiku, opus, batch])
    aws.scw.add_response("list_metrics", {"Metrics": []}, {"Namespace": "AWS/Bedrock", "MetricName": "Invocations"})
    aws.scw.add_response("get_metric_data", {"MetricDataResults": []}, _METRIC_PARAMS)
    _stub_history(aws.sq, [])
    _stub_auto(aws.sq)
    (filtered,) = (await aws.service.dashboard()).groups
    assert [q.quota_code for q in filtered.limits] == ["L-1"]
    assert filtered.usage_error is None
    # No traffic this week is zero use, not N/A.
    assert filtered.limits[0].usage == 0.0 and filtered.limits[0].utilization == 0.0

    # The service snapshot and the ModelId list are cached, so "Show all" lists nothing again.
    aws.scw.add_response("get_metric_data", {"MetricDataResults": []}, _METRIC_PARAMS)
    (everything,) = (await aws.service.dashboard(all_bedrock=True)).groups
    assert sorted(q.quota_code for q in everything.limits) == ["L-1", "L-2", "L-3"]
    by_code = {q.quota_code: q for q in everything.limits}
    # Opus is in no catalog and had no traffic, and batch quotas are not throughput.
    assert by_code["L-2"].usage is None and by_code["L-3"].usage is None


def test_feature_groups_hide_with_their_feature(monkeypatch):
    monkeypatch.setenv("FINE_TUNING_ENABLED", "false")
    monkeypatch.setenv("DICTATION_ENABLED", "false")
    ids = [g.id for g in catalog.enabled_groups()]
    assert "fine_tuning" not in ids and "dictation" not in ids
    assert {"model_inference", "agent_runtime", "knowledge_bases"} <= set(ids)
    assert not catalog.is_monitored("sagemaker", "L-X", "ml.g5.xlarge for training job usage")

    monkeypatch.setenv("FINE_TUNING_ENABLED", "true")
    assert catalog.is_monitored("sagemaker", "L-X", "ml.g5.xlarge for training job usage")
    assert not catalog.is_monitored("sagemaker", "L-Y", "ml.p5.48xlarge for training job usage")


def test_agentcore_catalog_is_curated_and_s3_vectors_is_a_doc_link():
    runtime = catalog.get_group("agent_runtime")
    assert len(runtime.quotas) == 10
    # Payments, Policy Engine and Evaluations quotas are not in the catalog.
    assert not catalog.is_monitored("bedrock-agentcore", "L-NOT-LISTED", "Payment managers per account")
    kb = catalog.get_group("knowledge_bases")
    assert kb.service_codes == () and kb.doc_links[0].url.endswith("s3-vectors-limitations.html")
    # Every service the catalog reads is in the IAM write condition's list.
    for group in catalog.CATALOG:
        assert set(group.service_codes) <= set(catalog.MONITORED_SERVICE_CODES)


# ── history ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_history_names_the_admin_who_asked_through_this_app(aws):
    created = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    _stub_history(
        aws.sq,
        [
            {
                "Id": "ours",
                "ServiceCode": "lambda",
                "QuotaCode": LAMBDA_CONCURRENCY,
                "Status": "CASE_OPENED",
                "CaseId": "case-1",
                "DesiredValue": 3000.0,
                "Created": created,
            },
            {
                "Id": "console",
                "ServiceCode": "sagemaker",
                "QuotaCode": "L-1",
                "Status": "APPROVED",
                "Created": datetime(2026, 3, 14, tzinfo=timezone.utc),
            },
            {
                "Id": "auto",
                "ServiceCode": "lambda",
                "QuotaCode": LAMBDA_CONCURRENCY,
                "Status": "DENIED",
                "RequestType": "AutomaticManagement",
                "Created": datetime(2026, 9, 1, tzinfo=timezone.utc),
            },
        ],
    )
    aws.audit.repository.records.append(
        AuditRecord(
            action="aws_limit.increase_requested",
            actor_user_id="admin-1",
            actor_email="admin@example.edu",
            target_type="aws_limit_request",
            target_id="ours",
            timestamp="2026-10-20T12:00:01Z",
        )
    )

    history = await aws.service.history()

    assert [r.request_id for r in history.requests] == ["ours", "auto", "console"]
    by_id = {r.request_id: r for r in history.requests}
    assert by_id["ours"].requested_by == "admin@example.edu" and by_id["ours"].case_id == "case-1"
    assert by_id["console"].requested_by is None
    assert by_id["auto"].request_type == "AutomaticManagement"
    assert aws.audit.repository.months[0] == "2026-03"


# ── POST validation ─────────────────────────────────────────────────────────


def _stub_fresh_read(sq: Stubber, quota: dict, *, applied: bool = True) -> None:
    key = {"ServiceCode": quota["ServiceCode"], "QuotaCode": quota["QuotaCode"]}
    sq.add_response("get_aws_default_service_quota", {"Quota": quota}, key)
    if applied:
        sq.add_response("get_service_quota", {"Quota": quota}, key)
    else:
        _client_error(sq, "get_service_quota", "NoSuchResourceException")


def _body(service="lambda", code=LAMBDA_CONCURRENCY, value=3000.0) -> IncreaseRequest:
    return IncreaseRequest(service_code=service, quota_code=code, desired_value=value)


@pytest.mark.asyncio
async def test_submits_an_increase_and_audits_it(aws):
    _stub_fresh_read(aws.sq, _quota("lambda", LAMBDA_CONCURRENCY, "Concurrent executions", 1000))
    aws.sq.add_response(
        "request_service_quota_increase",
        {
            "RequestedQuota": {
                "Id": "req-123",
                "Status": "PENDING",
                "CaseId": "",
                "ServiceCode": "lambda",
                "QuotaCode": LAMBDA_CONCURRENCY,
                "DesiredValue": 3000.0,
            }
        },
        {"ServiceCode": "lambda", "QuotaCode": LAMBDA_CONCURRENCY, "DesiredValue": 3000.0, "SupportCaseAllowed": True},
    )

    record = await aws.service.request_increase(ADMIN, _body())

    assert record.request_id == "req-123" and record.status == "PENDING"
    assert record.requested_by == "admin@example.edu" and record.quota_name == "Concurrent executions"
    (audit,) = aws.audit.records
    assert audit["action"] == "aws_limit.increase_requested"
    assert audit["target_type"] == "aws_limit_request" and audit["target_id"] == "req-123"
    assert audit["actor"] is ADMIN
    assert audit["after"] == {
        "serviceCode": "lambda",
        "quotaCode": LAMBDA_CONCURRENCY,
        "quotaName": "Concurrent executions",
        "region": REGION,
        "currentValue": 1000,
        "desiredValue": 3000.0,
        "requestId": "req-123",
        "status": "PENDING",
        "caseId": None,
    }


@pytest.mark.asyncio
async def test_a_quota_never_changed_from_its_default_can_be_raised(aws):
    tasks = _quota("ecs", "L-92E49DE3", "Tasks per service", 5000)
    _stub_fresh_read(aws.sq, tasks, applied=False)
    aws.sq.add_response("request_service_quota_increase", {"RequestedQuota": {"Id": "r", "Status": "PENDING"}}, None)
    record = await aws.service.request_increase(ADMIN, _body("ecs", "L-92E49DE3", 6000))
    assert record.request_id == "r"


@pytest.mark.asyncio
async def test_refuses_an_unmonitored_service_without_calling_aws(aws):
    with pytest.raises(AwsLimitsError) as e:
        await aws.service.request_increase(ADMIN, _body("ec2", "L-1216C47A"))
    assert e.value.status_code == 400
    assert aws.audit.records == []


@pytest.mark.parametrize(
    "quota,value,fragment",
    [
        (_quota("lambda", "L-9FEEFFC0", "Function timeout", 900), 1000, "not monitored"),
        (_quota("lambda", LAMBDA_CONCURRENCY, "Concurrent executions", 1000, adjustable=False), 3000, "Support case"),
        (_quota("lambda", LAMBDA_CONCURRENCY, "Concurrent executions", 1000), 1000, "greater than the applied value"),
    ],
)
@pytest.mark.asyncio
async def test_validates_before_asking_aws(aws, quota, value, fragment):
    _stub_fresh_read(aws.sq, quota)
    with pytest.raises(AwsLimitsError) as e:
        await aws.service.request_increase(ADMIN, _body(quota["ServiceCode"], quota["QuotaCode"], value))
    assert e.value.status_code == 400 and fragment in e.value.message
    assert aws.audit.records == []


@pytest.mark.parametrize(
    "code,status,fragment",
    [
        ("ResourceAlreadyExistsException", 409, "already open"),
        ("TooManyRequestsException", 429, "Try again"),
        ("QuotaExceededException", 429, "Try again"),
        ("DependencyAccessDeniedException", 502, "AWSServiceRoleForServiceQuotas"),
        ("IllegalArgumentException", 400, "nope"),
        ("InvalidResourceStateException", 400, "nope"),
    ],
)
@pytest.mark.asyncio
async def test_maps_aws_errors(aws, code, status, fragment):
    _stub_fresh_read(aws.sq, _quota("lambda", LAMBDA_CONCURRENCY, "Concurrent executions", 1000))
    _client_error(aws.sq, "request_service_quota_increase", code)
    with pytest.raises(AwsLimitsError) as e:
        await aws.service.request_increase(ADMIN, _body())
    assert e.value.status_code == status and fragment in e.value.message
    assert aws.audit.records == []


@pytest.mark.asyncio
async def test_unknown_quota_is_a_400(aws):
    key = {"ServiceCode": "lambda", "QuotaCode": "L-NOPE"}
    _client_error(aws.sq, "get_aws_default_service_quota", "NoSuchResourceException", "no such quota")
    aws.sq.add_client_error(
        "get_service_quota",
        service_error_code="NoSuchResourceException",
        service_message="no such quota",
        http_status_code=400,
        expected_params=key,
    )
    with pytest.raises(AwsLimitsError) as e:
        await aws.service.request_increase(ADMIN, _body("lambda", "L-NOPE"))
    assert e.value.status_code == 400 and "no such quota" in e.value.message


# ── routes ──────────────────────────────────────────────────────────────────


class _FakeService:
    def __init__(self):
        self.calls: List[tuple] = []

    async def dashboard(self, **kw):
        self.calls.append(("dashboard", kw))
        from apis.shared.aws_limits.models import AwsLimitsDashboard

        return AwsLimitsDashboard(
            region=REGION,
            generated_at="t",
            as_of="t",
            guidance_url="g",
            **{"can_request_increase": kw["can_request_increase"], "all_bedrock": kw["all_bedrock"]},
        )

    async def request_increase(self, admin, body):
        self.calls.append(("request", admin.user_id, body.desired_value))
        raise AwsLimitsError(409, "An increase request for this quota is already open.")


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("AWS_LIMITS_ENABLED", "true")
    fake = _FakeService()
    monkeypatch.setattr(aws_routes, "get_aws_limits_service", lambda: fake)
    permissions = {"app_roles": [], "admin_scopes": ["admin.aws_limits"]}

    class _Roles:
        async def resolve_user_permissions(self, user):
            return SimpleNamespace(**permissions)

    import apis.shared.rbac.service as rbac_service

    monkeypatch.setattr(rbac_service, "get_app_role_service", lambda: _Roles())
    app = FastAPI()
    app.include_router(aws_routes.router, prefix="/admin")
    app.dependency_overrides[get_current_user_from_session] = lambda: ADMIN
    return SimpleNamespace(client=TestClient(app), fake=fake, permissions=permissions)


def test_routes_serve_a_scope_holder_and_pass_the_query(api):
    response = api.client.get("/admin/aws-limits", params={"all_bedrock": "true", "refresh": "true"})
    assert response.status_code == 200, response.text
    assert response.json()["canRequestIncrease"] is True
    assert api.fake.calls == [("dashboard", {"all_bedrock": True, "refresh": True, "can_request_increase": True})]


def test_dev_post_accepts_the_aws_limits_scope_and_maps_errors(api):
    response = api.client.post(
        "/admin/aws-limits/requests",
        json={"serviceCode": "lambda", "quotaCode": LAMBDA_CONCURRENCY, "desiredValue": 3000},
    )
    assert response.status_code == 409
    assert "already open" in response.json()["detail"]
    assert api.fake.calls == [("request", "admin-1", 3000.0)]


def test_post_rejects_a_non_positive_value(api):
    response = api.client.post("/admin/aws-limits/requests", json={"serviceCode": "lambda", "quotaCode": "L-1", "desiredValue": 0})
    assert response.status_code == 422


def test_admins_without_the_scope_get_403(api):
    api.permissions["admin_scopes"] = ["admin.quota"]
    assert api.client.get("/admin/aws-limits").status_code == 403
    assert api.client.post("/admin/aws-limits/requests", json={"serviceCode": "lambda", "quotaCode": "L-1", "desiredValue": 5}).status_code == 403
    assert api.fake.calls == []


def test_system_admin_passes_without_the_scope(api):
    api.permissions.update(app_roles=["system_admin"], admin_scopes=[])
    assert api.client.get("/admin/aws-limits").status_code == 200


def test_flag_off_is_404_after_auth(api, monkeypatch):
    monkeypatch.setenv("AWS_LIMITS_ENABLED", "")
    assert api.client.get("/admin/aws-limits").status_code == 404
    assert api.client.get("/admin/aws-limits/requests").status_code == 404
    assert api.client.get("/admin/aws-limits/lambda/L-B99A9384").status_code == 404
    assert api.client.post("/admin/aws-limits/requests", json={"serviceCode": "lambda", "quotaCode": "L-1", "desiredValue": 5}).status_code == 404
    assert api.fake.calls == []

    del api.client.app.dependency_overrides[get_current_user_from_session]
    assert api.client.get("/admin/aws-limits").status_code == 401


# ── usage over the week ─────────────────────────────────────────────────────


class _RecordingCloudWatch:
    """A CloudWatch stand-in that answers from a table and keeps the requests."""

    def __init__(self, series: Dict[tuple, Dict[datetime, float]], model_ids: List[str]):
        self.series = series
        self.model_ids = model_ids
        self.requests: List[dict] = []

    def get_paginator(self, name):
        assert name == "list_metrics"
        ids = self.model_ids

        class _P:
            def paginate(self, **kw):
                assert kw == {"Namespace": "AWS/Bedrock", "MetricName": "Invocations"}
                yield {"Metrics": [{"Dimensions": [{"Name": "ModelId", "Value": i}]} for i in ids]}

        return _P()

    def get_metric_data(self, **params):
        self.requests.append(params)
        results = []
        for q in params["MetricDataQueries"]:
            stat = q["MetricStat"]
            dims = tuple((d["Name"], d["Value"]) for d in stat["Metric"]["Dimensions"])
            points = self.series.get((stat["Metric"]["MetricName"], dims), {})
            results.append({"Id": q["Id"], "Timestamps": list(points), "Values": list(points.values())})
        return {"MetricDataResults": results}


T1 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 11, 1, 12, 1, tzinfo=timezone.utc)
HAIKU = "anthropic.claude-haiku-4-5-20251001-v1:0"


@pytest.mark.asyncio
async def test_bedrock_quotas_are_measured_from_bedrock_metrics_over_seven_days(aws, monkeypatch):
    _only_groups(monkeypatch, "model_inference", "background_jobs")
    quotas = [
        _quota("bedrock", "L-XR", "Cross-region model inference tokens per minute for Anthropic Claude Haiku 4.5", 5_000_000),
        _quota("bedrock", "L-OD", "On-demand model inference requests per minute for Anthropic Claude Haiku 4.5", 1000),
        _quota("bedrock", "L-GL", "Global cross-region model inference tokens per minute for Anthropic Claude Haiku 4.5", 5_000_000),
        _quota("bedrock", "L-1M", "Cross-region model inference tokens per minute for Anthropic Claude Haiku 4.5 V1 1M Context Length", 1e6),
    ]
    _stub_service(aws.sq, "bedrock", [quotas], quotas)
    concurrency = _quota("lambda", LAMBDA_CONCURRENCY, "Concurrent executions", 1000, UsageMetric=LAMBDA_USAGE)
    _stub_service(aws.sq, "lambda", [[concurrency]], [concurrency])
    _stub_history(aws.sq, [])
    _stub_auto(aws.sq)
    aws.models.append(ModelRef(model_id=f"us.{HAIKU}", model_name="Claude Haiku 4.5"))
    tpm = ("EstimatedTPMQuotaUsage", (("ModelId", f"us.{HAIKU}"),))
    cloudwatch = _RecordingCloudWatch(
        {
            tpm: {T1: 100_000.0, T2: 250_000.0},
            # The on-demand model was invoked by id and by ARN; both count, per minute.
            ("Invocations", (("ModelId", HAIKU),)): {T1: 30.0, T2: 10.0},
            ("Invocations", (("ModelId", f"arn:aws:bedrock:us-west-2::foundation-model/{HAIKU}"),)): {T1: 15.0},
            ("ConcurrentExecutions", ()): {T1: 7.0},
        },
        model_ids=[HAIKU, f"arn:aws:bedrock:us-west-2::foundation-model/{HAIKU}", f"us.{HAIKU}"],
    )
    aws.service._cloudwatch = cloudwatch

    dashboard = await aws.service.dashboard()

    by_code = {q.quota_code: q for g in dashboard.groups for q in g.limits}
    assert by_code["L-XR"].usage == 250_000.0 and by_code["L-XR"].utilization == 5.0
    assert "EstimatedTPMQuotaUsage" in by_code["L-XR"].usage_source
    assert by_code["L-OD"].usage == 45.0 and by_code["L-OD"].utilization == 4.5
    # The catalog model could be invoked globally; nobody did this week.
    assert by_code["L-GL"].usage == 0.0
    # A long-context variant cannot be told apart from the base model's traffic.
    assert by_code["L-1M"].usage is None and by_code["L-1M"].utilization is None
    assert by_code[LAMBDA_CONCURRENCY].usage == 7.0
    assert dashboard.usage_window_days == 7

    queries = {
        (q["MetricStat"]["Metric"]["MetricName"], q["MetricStat"]["Stat"], q["MetricStat"]["Period"])
        for r in cloudwatch.requests
        for q in r["MetricDataQueries"]
    }
    # Sums stay per minute (the busiest minute); a Maximum loses nothing at an hour.
    assert ("EstimatedTPMQuotaUsage", "Sum", 60) in queries
    assert ("Invocations", "Sum", 60) in queries
    assert ("ConcurrentExecutions", "Maximum", 3600) in queries
    for r in cloudwatch.requests:
        assert r["EndTime"] - r["StartTime"] == timedelta(days=7)


@pytest.mark.asyncio
async def test_small_utilization_keeps_its_precision(aws, monkeypatch):
    _only_groups(monkeypatch, "agent_runtime")
    sessions = _quota(
        "bedrock-agentcore",
        "L-3E5722B2",
        "Active Session Workloads per Account",
        5000,
        UsageMetric={
            "MetricNamespace": "AWS/Bedrock-AgentCore",
            "MetricName": "ActiveSessionCount",
            "MetricDimensions": {"Service": "AgentCore.Runtime"},
            "MetricStatisticRecommendation": "Maximum",
        },
    )
    _stub_service(aws.sq, "bedrock-agentcore", [[sessions]], [sessions])
    _stub_history(aws.sq, [])
    _stub_auto(aws.sq)
    aws.service._cloudwatch = _RecordingCloudWatch({("ActiveSessionCount", (("Service", "AgentCore.Runtime"),)): {T1: 1.0}}, model_ids=[])
    (group,) = (await aws.service.dashboard()).groups
    assert group.limits[0].utilization == 0.02
