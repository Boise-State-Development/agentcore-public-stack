"""Quota requests and extensions over a moto user-quotas table.

docs/specs/quota-requests-and-extensions.md §16: repository (GSI prefixes,
revoke re-deriving the sort key, period pointers) and routes (one open
request, policy bounds, funding validation, self-approval, the flag, the
standing rules), driven through the real FastAPI routers.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import List

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from agents.main_agent.quota.models import FundingSource, QuotaExtension
from agents.main_agent.quota.repository import QuotaRepository
from apis.app_api.admin.quota_requests import routes as admin_routes
from apis.app_api.quota import routes as user_routes
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.caching.config_cache import get_config_cache
from apis.shared.quota_requests import service as service_module
from apis.shared.quota_requests.models import QuotaRequestContext, QuotaRequestPolicy
from apis.shared.quota_requests.repository import QuotaRequestRepository
from apis.shared.quota_requests.service import QuotaRequestService

TABLE = "test-user-quotas"
REQUESTER = User(email="requester@example.com", user_id="user-r", name="R", roles=["Faculty"])
ADMIN = User(email="approver@example.com", user_id="user-a", name="A", roles=["Admin"])


def _create_table() -> None:
    gsis = [
        ("AssignmentTypeIndex", "GSI1PK", "GSI1SK"),
        ("UserAssignmentIndex", "GSI2PK", "GSI2SK"),
        ("RoleAssignmentIndex", "GSI3PK", "GSI3SK"),
        ("UserOverrideIndex", "GSI4PK", "GSI4SK"),
        ("AppRoleAssignmentIndex", "GSI6PK", "GSI6SK"),
    ]
    attrs = {"PK", "SK"} | {a for _, pk, sk in gsis for a in (pk, sk)}
    boto3.client("dynamodb").create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in sorted(attrs)],
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[
            {
                "IndexName": name,
                "KeySchema": [{"AttributeName": pk, "KeyType": "HASH"}, {"AttributeName": sk, "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            }
            for name, pk, sk in gsis
        ],
    )


class _Notifications:
    def __init__(self):
        self.sent: List[dict] = []

    def notify(self, **kw):
        self.sent.append(kw)

    def notify_many(self, recipients, **kw):
        self.sent.append({"recipients": list(recipients), **kw})
        return len(recipients)


class _Audit:
    def __init__(self):
        self.records: List[dict] = []

    def record(self, **kw):
        self.records.append(kw)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("QUOTA_REQUESTS_ENABLED", "true")
    get_config_cache().clear()
    with mock_aws():
        _create_table()
        quotas = QuotaRepository(table_name=TABLE, events_table_name="unused")
        requests = QuotaRequestRepository(table_name=TABLE)
        notifications = _Notifications()
        audit = _Audit()
        service = QuotaRequestService(requests=requests, quotas=quotas, notifications=notifications)

        async def _snapshot(user, *, fresh=False):
            return QuotaRequestContext(period="2026-10", base_limit=30, effective_limit=30, current_usage=31, blocked=True)

        async def _resolve(user, *, fresh=False):
            return None

        async def _user_for(user_id, fallback_email=""):
            directory = {REQUESTER.user_id: REQUESTER, ADMIN.user_id: ADMIN}
            return directory.get(user_id) or User(email=fallback_email, user_id=user_id, name="", roles=[])

        monkeypatch.setattr(service, "snapshot", _snapshot)
        monkeypatch.setattr(service, "_resolve", _resolve)
        monkeypatch.setattr(service, "_user_for", _user_for)
        monkeypatch.setattr(service, "_invalidate", lambda user_id: None)
        monkeypatch.setattr(service_module, "get_audit_service", lambda: audit)
        monkeypatch.setattr(service_module, "_service", service)

        app = FastAPI()
        app.include_router(user_routes.router)
        app.include_router(admin_routes.router, prefix="/admin")
        state = {"user": REQUESTER}
        app.dependency_overrides[get_current_user_from_session] = lambda: state["user"]
        app.dependency_overrides[admin_routes.require_quota_requests_admin] = lambda: ADMIN

        yield {
            "client": TestClient(app), "service": service, "quotas": quotas, "requests": requests,
            "notifications": notifications, "audit": audit, "state": state,
        }
    get_config_cache().clear()


def _through(days: int = 20) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _submit(client, **overrides):
    body = {
        "requestedAmount": 25,
        "requestedUntil": _through(),
        "justification": "Grant proposal due Nov 15; the agent drafts sections.",
        **overrides,
    }
    return client.post("/quota/requests", json=body)


def _set_policy(service: QuotaRequestService, **fields):
    import asyncio

    asyncio.run(service.requests.put_policy(QuotaRequestPolicy(**fields)))


# ── the whole loop ──────────────────────────────────────────────────────────


def test_submit_approve_creates_extension_and_notifies(env):
    client = env["client"]
    created = _submit(client)
    assert created.status_code == 201, created.text
    request_id = created.json()["requestId"]
    assert created.json()["context"]["blocked"] is True

    queue = client.get("/admin/quota-requests").json()["requests"]
    assert [r["requestId"] for r in queue] == [request_id]
    assert client.get("/admin/quota-requests/summary").json() == {"pending": 1, "infoRequested": 0}

    approved = client.post(
        f"/admin/quota-requests/{request_id}/approve",
        json={"amount": 20, "validUntil": _through(), "fundingMode": "central", "note": "Good luck"},
    )
    assert approved.status_code == 200, approved.text
    ext = approved.json()["extension"]
    assert approved.json()["request"]["status"] == "approved"
    assert ext["amount"] == "20" or float(ext["amount"]) == 20.0

    import asyncio

    active = asyncio.run(env["quotas"].get_active_extensions(REQUESTER.user_id))
    assert [e.extension_id for e in active] == [ext["extensionId"]]

    kinds = [n["kind"] for n in env["notifications"].sent]
    assert kinds == ["quota_request_decided"]
    decided = env["notifications"].sent[0]["payload"]
    assert decided["requestedAmount"] == "25.00" and decided["amount"] == "20.00"
    actions = [r["action"] for r in env["audit"].records]
    assert actions == ["quota_extension.created", "quota_request.approved"]

    # The decision cannot be made twice.
    again = client.post(f"/admin/quota-requests/{request_id}/deny", json={"note": "no"})
    assert again.status_code == 409


def test_my_requests_mark_an_approval_whose_extension_was_revoked(env):
    client = env["client"]
    request_id = _submit(client).json()["requestId"]
    approved = client.post(
        f"/admin/quota-requests/{request_id}/approve",
        json={"amount": 20, "validUntil": _through(), "fundingMode": "central"},
    ).json()
    mine = client.get("/quota/requests").json()
    assert mine[0]["decision"].get("extensionRevokedAt") is None

    extension_id = approved["extension"]["extensionId"]
    assert client.post(f"/admin/quota-extensions/{extension_id}/revoke", json={"reason": "ended"}).status_code == 200

    mine = client.get("/quota/requests").json()
    assert mine[0]["status"] == "approved"
    assert mine[0]["decision"]["extensionRevokedAt"]
    # Joined at read time, never written onto the request row.
    stored = env["requests"].table.get_item(Key={"PK": f"QUOTA_REQUEST#{request_id}", "SK": "METADATA"})["Item"]
    assert "extensionRevokedAt" not in stored["decision"]


def test_one_open_request_per_user(env):
    client = env["client"]
    first = _submit(client).json()["requestId"]
    second = _submit(client)
    assert second.status_code == 409
    assert second.json()["openRequestId"] == first

    assert client.patch(f"/quota/requests/{first}", json={"withdraw": True}).json()["status"] == "withdrawn"
    assert _submit(client).status_code == 201


def test_policy_bounds_on_requests(env):
    client = env["client"]
    assert _submit(client, requestedAmount=500).status_code == 400  # over maxRequestAmount 100
    assert _submit(client, requestedUntil=_through(200)).status_code == 400  # over 92 days
    assert _submit(client, requestedUntil=_through(-2)).status_code == 400
    assert _submit(client, justification="more pls").status_code == 400

    _set_policy(env["service"], allowCustomAmount=False)
    assert _submit(client, requestedAmount=13).status_code == 400
    assert _submit(client, requestedAmount=10).status_code == 201


def test_funding_schema_validation(env):
    client = env["client"]
    _set_policy(
        env["service"],
        fundingSourceMode="required",
        fundingFields=[
            {"key": "fund", "label": "Fund", "required": True, "pattern": r"\d{6}"},
            {"key": "org", "label": "Org", "required": False},
        ],
    )
    assert _submit(client).status_code == 400  # required and missing
    assert _submit(client, fundingSource={"fields": {"fund": "12"}}).status_code == 400  # pattern
    assert _submit(client, fundingSource={"fields": {"fund": "100000", "grant": "x"}}).status_code == 400
    ok = _submit(client, fundingSource={"fields": {"org": " 4210 ", "fund": "100000"}})
    assert ok.status_code == 201, ok.text
    assert ok.json()["fundingKey"] == "fund=100000|org=4210"

    policy = client.get("/quota/policy").json()
    assert policy["fundingSourceMode"] == "required" and len(policy["fundingFields"]) == 2


def test_info_requested_round_trip(env):
    client = env["client"]
    _set_policy(env["service"], fundingFields=[{"key": "fund", "label": "Fund"}])
    request_id = _submit(client).json()["requestId"]

    asked = client.post(f"/admin/quota-requests/{request_id}/request-info", json={"note": "Which fund?"})
    assert asked.json()["status"] == "info_requested"
    assert env["notifications"].sent[-1]["kind"] == "quota_request_info_requested"

    back = client.patch(f"/quota/requests/{request_id}", json={"fundingSource": {"fields": {"fund": "100000"}}})
    assert back.json()["status"] == "pending"
    assert back.json()["fundingKey"] == "fund=100000"


def test_self_approval_is_refused_and_audited(env):
    env["state"]["user"] = ADMIN
    request_id = _submit(env["client"]).json()["requestId"]

    refused = env["client"].post(
        f"/admin/quota-requests/{request_id}/approve",
        json={"amount": 20, "validUntil": _through(), "fundingMode": "central"},
    )

    assert refused.status_code == 403
    record = env["audit"].records[-1]
    assert record["action"] == "quota_request.mutation_denied" and record["outcome"] == "denied"
    direct = env["client"].post(
        "/admin/quota-extensions",
        json={"userId": ADMIN.user_id, "amount": 10, "validUntil": _through(), "reason": "me"},
    )
    assert direct.status_code == 403


def test_flag_off_is_404_after_auth(env, monkeypatch):
    monkeypatch.setenv("QUOTA_REQUESTS_ENABLED", "")
    client = env["client"]
    assert client.get("/quota/policy").status_code == 404
    assert client.get("/admin/quota-requests").status_code == 404

    del client.app.dependency_overrides[get_current_user_from_session]
    del client.app.dependency_overrides[admin_routes.require_quota_requests_admin]
    assert client.get("/quota/policy").status_code == 401


def test_standing_rules_on_direct_extensions(env):
    client = env["client"]
    base = {"userId": REQUESTER.user_id, "validUntil": _through(200), "reason": "Research grant"}
    fund = {"fields": {"fund": "100000"}, "authorizerEmail": "Owner@Example.edu"}

    assert client.post("/admin/quota-extensions", json={**base}).status_code == 400  # amount null, not standing
    assert client.post("/admin/quota-extensions", json={**base, "amount": 25000}).status_code == 400

    _set_policy(env["service"], fundingFields=[{"key": "fund", "label": "Fund", "required": True}])
    no_fund = client.post("/admin/quota-extensions", json={**base, "standing": True, "fundingMode": "chargeback"})
    assert no_fund.status_code == 400
    central = client.post(
        "/admin/quota-extensions", json={**base, "standing": True, "fundingMode": "central", "fundingSource": fund}
    )
    assert central.status_code == 400

    uncapped = client.post(
        "/admin/quota-extensions",
        json={**base, "standing": True, "fundingMode": "chargeback", "fundingSource": fund},
    )
    assert uncapped.status_code == 201, uncapped.text
    assert uncapped.json()["amount"] is None
    assert uncapped.json()["fundingSource"]["authorizerEmail"] == "owner@example.edu"
    assert env["notifications"].sent[-1]["kind"] == "quota_extension_granted"

    # Turning standing off refuses new ones but existing ones keep resolving.
    _set_policy(env["service"], standingEnabled=False, fundingFields=[{"key": "fund", "label": "Fund"}])
    refused = client.post(
        "/admin/quota-extensions",
        json={**base, "standing": True, "amount": 50, "fundingMode": "chargeback", "fundingSource": fund},
    )
    assert refused.status_code == 400
    import asyncio

    assert len(asyncio.run(env["quotas"].get_active_extensions(REQUESTER.user_id))) == 1


# ── repository ──────────────────────────────────────────────────────────────


def _extension(ext_id: str, valid_until: str, **kw) -> QuotaExtension:
    return QuotaExtension(
        extension_id=ext_id, user_id=REQUESTER.user_id, amount=10, valid_from="2026-10-30T00:00:00Z",
        valid_until=valid_until, created_by=ADMIN.user_id, created_at="2026-10-30T00:00:00Z", **kw,
    )


@pytest.mark.asyncio
async def test_revoke_rederives_the_index_sort_key_and_keeps_the_row(env):
    quotas: QuotaRepository = env["quotas"]
    await quotas.create_extension(
        _extension(
            "e1", "2099-01-31T23:59:59.999999Z", funding_mode="chargeback",
            funding_source=FundingSource(fields={"fund": "1"}), funding_key="fund=1",
        )
    )
    assert len(await quotas.get_active_extensions(REQUESTER.user_id)) == 1

    revoked = await quotas.revoke_extension("e1", revoked_by=ADMIN.user_id, reason="ended")

    assert revoked.revoked_at and revoked.valid_until < "2099"
    item = quotas.table.get_item(Key={"PK": "QUOTA_EXT#e1", "SK": "METADATA"})["Item"]
    assert item["GSI2SK"] == f"VALID_UNTIL#{revoked.valid_until}"
    assert item["GSI1SK"] == f"VALID_UNTIL#{revoked.valid_until}"
    assert await quotas.get_active_extensions(REQUESTER.user_id) == []
    assert len(await quotas.list_user_extensions(REQUESTER.user_id)) == 1
    assert await quotas.revoke_extension("e1", revoked_by=ADMIN.user_id, reason="again") is None


@pytest.mark.asyncio
async def test_one_pointer_per_overlapped_period(env):
    quotas: QuotaRepository = env["quotas"]
    await quotas.create_extension(_extension("e2", "2027-01-31T23:59:59.999999Z"))

    for period, expected in (("2026-10", 1), ("2026-11", 1), ("2027-01", 1), ("2027-02", 0)):
        items = quotas.table.query(
            KeyConditionExpression="PK = :pk", ExpressionAttributeValues={":pk": f"QUOTA_EXT_PERIOD#{period}"}
        )["Items"]
        assert len(items) == expected, period


@pytest.mark.asyncio
async def test_assignment_and_extension_prefixes_never_cross(env):
    quotas: QuotaRepository = env["quotas"]
    # A direct-assignment row on the same index, for the same user.
    quotas.table.put_item(Item={
        "PK": "ASSIGNMENT#a1", "SK": "METADATA", "GSI2PK": f"USER#{REQUESTER.user_id}",
        "GSI2SK": "ASSIGNMENT#a1", "GSI1PK": "ASSIGNMENT_TYPE#direct_user", "GSI1SK": "PRIORITY#300#a1",
        "assignmentId": "a1", "tierId": "t", "assignmentType": "direct_user", "userId": REQUESTER.user_id,
        "priority": 300, "enabled": True, "createdAt": "x", "updatedAt": "x", "createdBy": "x",
    })
    await quotas.create_extension(_extension("e3", "2099-01-31T23:59:59.999999Z"))

    assert [e.extension_id for e in await quotas.get_active_extensions(REQUESTER.user_id)] == ["e3"]
    assignment = await quotas.query_user_assignment(REQUESTER.user_id)
    assert assignment is not None and assignment.assignment_id == "a1"
