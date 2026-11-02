"""The chargeback report over a moto user-quotas table (quota-requests spec §9, §16).

The seeded month below is §9.1 worked by hand. Base $30 for everyone unless
noted; the funding fields are ``fund`` and ``org``, grouped by ``org``.

    user  extension                          spend   central  over  consumed                 unattributed
    A     E1 central $10                      75      30       45    E1 10, E2 20, E3 15      0
          E2 chargeback $20  fund 100000
          E3 standing, uncapped, fund 200000
    B     E4 chargeback $25  fund 100000      40      30       10    E4 10                    0
    C     E5 central $10                      28      28        0    E5 0, E6 0               0
          E6 standing, cap $50, fund 200000
    D     E7 central $5 (a ``warn`` tier)     50      30       20    E7 5                     15
    E     E8 central $10 (base 30)            55      40       15    E8 10, E9 5              0
          E9 chargeback $20 fund 300000 (base 40: the tier changed mid-period,
          so §9.2 uses the larger base and marks both lines)

    fund 100000 = 20 + 10 = 30 · fund 200000 = 15 + 0 = 15 · fund 300000 = 5
    central budget = 10 + 0 + 5 + 10 = 25 · unattributed = 15
"""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from agents.main_agent.quota.extensions import period_waterfall
from agents.main_agent.quota.models import FundingSource, QuotaExtension
from agents.main_agent.quota.repository import QuotaRepository
from apis.app_api.admin.quota_requests import routes as admin_routes
from apis.shared.auth.models import User
from apis.shared.quota_requests import chargeback as chargeback_module
from apis.shared.quota_requests.chargeback import (
    ChargebackConflict,
    ChargebackFilters,
    ChargebackService,
    report_csv,
)
from apis.shared.quota_requests.ledger import ChargeLedgerRepository
from apis.shared.quota_requests.models import FundingField, QuotaRequestPolicy

from tests.apis.app_api.test_quota_requests import TABLE, _create_table

ADMIN = User(email="approver@example.com", user_id="user-admin", name="A", roles=["Admin"])
PERIOD = "2026-10"
FUND_1 = "fund=100000|org=4210"
FUND_2 = "fund=200000|org=4300"
FUND_3 = "fund=300000|org=4210"

POLICY = QuotaRequestPolicy(
    funding_fields=[FundingField(key="fund", label="Fund", required=True), FundingField(key="org", label="Org")],
    report_group_field="org",
)


def _source(fund: str, org: str, authorizer: str | None = None) -> FundingSource:
    return FundingSource(fields={"fund": fund, "org": org}, authorizer_email=authorizer)


def _ext(
    ext_id: str,
    user: str,
    amount,
    *,
    mode: str = "central",
    standing: bool = False,
    source: FundingSource | None = None,
    base: str = "30",
    valid_from: str = "2026-10-01T00:00:00Z",
    valid_until: str = "2026-10-31T23:59:59.999999Z",
    created_at: str = "2026-10-01T00:00:00Z",
) -> QuotaExtension:
    key = None
    if source is not None:
        key = "|".join(f"{k}={v}" for k, v in source.fields.items())
    return QuotaExtension(
        extension_id=ext_id,
        user_id=user,
        user_email=f"{user}@example.com",
        standing=standing,
        amount=amount,
        valid_from=valid_from,
        valid_until=valid_until,
        funding_mode=mode,
        funding_source=source,
        funding_key=key,
        reason="seed",
        base_limit_snapshot=Decimal(base),
        created_by=ADMIN.user_id,
        created_by_email=ADMIN.email,
        created_at=created_at,
    )


def _seed() -> List[QuotaExtension]:
    return [
        _ext("E1", "user-a", 10),
        _ext("E2", "user-a", 20, mode="chargeback", source=_source("100000", "4210", "owner1@example.com"),
             valid_until="2026-11-30T23:59:59.999999Z"),
        _ext("E3", "user-a", None, mode="chargeback", standing=True, source=_source("200000", "4300"),
             valid_until="2027-06-30T23:59:59.999999Z"),
        # Starts before the month, so a back-dated revoke can take it out of October.
        _ext("E4", "user-b", 25, mode="chargeback", source=_source("100000", "4210", "owner1@example.com"),
             valid_from="2026-09-15T00:00:00Z"),
        _ext("E5", "user-c", 10),
        _ext("E6", "user-c", 50, mode="chargeback", standing=True, source=_source("200000", "4300"),
             valid_until="2027-06-30T23:59:59.999999Z"),
        _ext("E7", "user-d", 5),
        _ext("E8", "user-e", 10),
        _ext("E9", "user-e", 20, mode="chargeback", source=_source("300000", "4210"), base="40"),
        # Ended in September: has no pointer for October and must not appear.
        _ext("E0", "user-a", 99, valid_from="2026-09-01T00:00:00Z", valid_until="2026-09-30T23:59:59.999999Z"),
    ]


SPEND = {"user-a": "75", "user-b": "40", "user-c": "28", "user-d": "50", "user-e": "55"}


class _Audit:
    def __init__(self):
        self.records: List[dict] = []

    def record(self, **kw):
        self.records.append(kw)


class _Clock:
    def __init__(self, iso: str):
        self.now = datetime.fromisoformat(iso.replace("Z", "+00:00"))

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("QUOTA_REQUESTS_ENABLED", "true")
    with mock_aws():
        _create_table()
        quotas = QuotaRepository(table_name=TABLE, events_table_name="unused")
        ledger = ChargeLedgerRepository(table_name=TABLE)
        for ext in _seed():
            asyncio.run(quotas.create_extension(ext))
        spend: Dict[str, str] = dict(SPEND)
        reads: List[str] = []

        async def _spend(user_id: str, period: str) -> Decimal:
            reads.append(user_id)
            return Decimal(spend.get(user_id, "0"))

        async def _policy():
            return POLICY

        clock = _Clock("2026-11-03T12:00:00Z")
        service = ChargebackService(
            ledger=ledger, policy_loader=_policy, spend_reader=_spend, clock=clock,
            extension_lister=quotas.list_extensions,
        )
        audit = _Audit()
        monkeypatch.setattr(chargeback_module, "get_audit_service", lambda: audit)
        monkeypatch.setattr(chargeback_module, "_service", service)
        yield {
            "service": service, "ledger": ledger, "quotas": quotas, "spend": spend,
            "clock": clock, "audit": audit, "reads": reads,
        }


def _report(service: ChargebackService, filters: ChargebackFilters | None = None, periods=(PERIOD,)) -> dict:
    return asyncio.run(service.report(list(periods), filters or ChargebackFilters()))


def _by_ext(report: dict) -> Dict[str, dict]:
    return {l["extensionId"]: l for l in report["lines"]}


def _fund_totals(report: dict) -> Dict[str, Decimal]:
    totals: Dict[str, Decimal] = {}
    for l in report["lines"]:
        key = l.get("fundingKey") or "central"
        totals[key] = totals.get(key, Decimal("0")) + Decimal(l["consumed"])
    return totals


# ── §9.1 by hand ────────────────────────────────────────────────────────────


def test_seeded_month_reproduces_the_waterfall_by_hand(env):
    report = _report(env["service"])
    lines = _by_ext(report)
    consumed = {k: Decimal(v["consumed"]) for k, v in lines.items()}
    assert consumed == {
        "E1": 10, "E2": 20, "E3": 15, "E4": 10, "E5": 0, "E6": 0, "E7": 5, "E8": 10, "E9": 5,
    }
    assert _fund_totals(report) == {"central": 25, FUND_1: 30, FUND_2: 15, FUND_3: 5}
    assert report["totals"] == {"central": "25.00", "chargeback": "50.00", "consumed": "75.00", "unattributed": "15.00"}

    assert lines["E3"]["granted"] is None and lines["E3"]["standing"] is True
    assert Decimal(lines["E6"]["granted"]) == 50
    # §9.2: user E's snapshots differ, so both lines carry the larger base and the marker.
    assert {lines["E8"]["baseLimitAmbiguous"], lines["E9"]["baseLimitAmbiguous"]} == {True}
    assert Decimal(lines["E9"]["baseLimit"]) == 40
    assert lines["E1"]["baseLimitAmbiguous"] is False
    # Funding attributes ride every chargeback line; a central line has none.
    assert lines["E2"]["fields"] == {"fund": "100000", "org": "4210"} and lines["E2"]["groupValue"] == "4210"
    assert lines["E1"]["fundingKey"] is None and lines["E1"]["fundingLabel"] == "" and lines["E1"]["fields"] == {}
    assert "E0" not in lines
    # Fund order: the partition sort key starts with the funding key.
    funds = [l.get("fundingKey") or "central" for l in report["lines"]]
    assert funds == sorted(funds)
    assert "centrally funded, then capped chargeback, then standing" in report["consumptionOrder"]


def test_waterfall_cases():
    def run(spend, *exts):
        return period_waterfall(Decimal(spend), exts)

    central = _ext("c", "u", 10)
    capped = _ext("k", "u", 20, mode="chargeback", source=_source("1", "1"))
    uncapped = _ext("s", "u", None, mode="chargeback", standing=True, source=_source("2", "2"))

    under = run("25", central)
    assert under.central == 25 and under.consumed[0][1] == 0
    within = run("35", capped)
    assert within.consumed[0][1] == 5
    spanning = run("55", central, capped)
    assert [c for _, c in spanning.consumed] == [10, 15]
    over = run("70", central, capped)
    assert [c for _, c in over.consumed] == [10, 20] and over.unattributed == 10
    # The central extension is consumed first even beside a standing one that ends sooner.
    early_standing = _ext("s2", "u", None, mode="chargeback", standing=True, source=_source("2", "2"),
                          valid_until="2026-10-05T00:00:00Z")
    absorbed = run("100", early_standing, central)
    assert [(e.extension_id, c) for e, c in absorbed.consumed] == [("c", 10), ("s2", 60)]
    assert run("100", uncapped).unattributed == 0
    # No snapshot at all: nothing is attributed rather than guessing a base.
    blind = central.model_copy(update={"base_limit_snapshot": None})
    assert run("90", blind).consumed[0][1] == 0


# ── closing (§9.4) ──────────────────────────────────────────────────────────


def test_before_the_grace_window_lines_are_provisional_and_nothing_is_written(env):
    env["clock"].now = datetime(2026, 11, 2, 23, 0, tzinfo=timezone.utc)
    report = _report(env["service"])
    assert report["periods"][0]["status"] == "provisional"
    assert report["periods"][0]["closable"] is False
    assert asyncio.run(env["ledger"].get_marker(PERIOD)) is None
    assert asyncio.run(env["ledger"].period_lines(PERIOD)) == []


def test_closing_twice_writes_once(env, monkeypatch):
    writes = []
    original = env["ledger"].replace_lines

    async def _spy(period, lines, stale):
        writes.append(len(lines))
        await original(period, lines, stale)

    monkeypatch.setattr(env["ledger"], "replace_lines", _spy)

    async def _two_admins():
        return await asyncio.gather(
            env["service"].report([PERIOD], ChargebackFilters()),
            env["service"].report([PERIOD], ChargebackFilters()),
        )

    first, second = asyncio.run(_two_admins())
    assert writes == [9]
    statuses = sorted(r["periods"][0]["status"] for r in (first, second))
    # The loser of the claim answers from its own computation, marked provisional
    # (or from the ledger, if the winner finished before it looked).
    assert statuses in (["closed", "provisional"], ["closed", "closed"])
    assert _fund_totals(first) == _fund_totals(second)

    reads_before = len(env["reads"])
    third = _report(env["service"])
    assert writes == [9]
    assert len(env["reads"]) == reads_before, "a closed period is read from the ledger, not recomputed"
    assert third["periods"][0]["status"] == "closed" and third["periods"][0]["closeVersion"] == 1
    assert _fund_totals(third) == _fund_totals(first)
    assert {l["status"] for l in third["lines"]} == {"closed"}
    assert Decimal(third["periods"][0]["unattributed"]) == 15


def test_a_closed_period_does_not_move_when_spend_changes(env):
    closed = _report(env["service"])
    env["spend"]["user-a"] = "500"
    assert _fund_totals(_report(env["service"])) == _fund_totals(closed)


def test_reclose_after_a_revoke_changes_exactly_that_fund_and_audits(env):
    before = _report(env["service"])
    assert _fund_totals(before)[FUND_1] == 30

    # A revoke that should have been back-dated: E4 ended before October began.
    asyncio.run(env["quotas"].revoke_extension("E4", revoked_by=ADMIN.user_id, reason="ended in September",
                                               now_iso="2026-09-30T12:00:00Z"))
    # A closed period stays as it was until someone recalculates it.
    assert _fund_totals(_report(env["service"])) == _fund_totals(before)

    result = asyncio.run(env["service"].reclose(ADMIN, PERIOD))
    assert result["changedFunds"] == [FUND_1]
    assert result["before"][FUND_1] == "30.00" and result["after"][FUND_1] == "20.00"
    assert {k: v for k, v in result["before"].items() if k != FUND_1} == {
        k: v for k, v in result["after"].items() if k != FUND_1
    }
    after = _report(env["service"])
    assert after["periods"][0]["closeVersion"] == 2
    assert "E4" not in _by_ext(after), "the revoked extension's stale line is deleted"
    # User B now has no extension in October, so they leave the extension lane entirely.
    assert Decimal(after["periods"][0]["unattributed"]) == 15
    totals = _fund_totals(after)
    assert totals[FUND_1] == 20 and totals[FUND_2] == 15 and totals[FUND_3] == 5 and totals["central"] == 25

    (record,) = env["audit"].records
    assert record["action"] == "quota_period.reclosed"
    assert record["target_type"] == "quota_period" and record["target_id"] == PERIOD
    assert record["changes"] == [FUND_1]
    assert record["before"] == {"closeVersion": 1, "funds": result["before"]}
    assert record["after"] == {"closeVersion": 2, "funds": result["after"]}


def test_reclose_refuses_an_open_period(env):
    env["clock"].now = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)
    with pytest.raises(ChargebackConflict):
        asyncio.run(env["service"].reclose(ADMIN, PERIOD))
    assert env["audit"].records == []


def test_an_abandoned_close_is_taken_over_once_stale(env):
    ledger = env["ledger"]
    assert asyncio.run(ledger.claim_first_close(PERIOD, now="2026-11-03T11:00:00Z", stale_before="2026-11-03T10:00:00Z")) == 1
    # Fresh claim held by someone else: this read stays provisional.
    env["clock"].now = datetime(2026, 11, 3, 11, 1, tzinfo=timezone.utc)
    assert _report(env["service"])["periods"][0]["status"] == "provisional"
    # Ten minutes on, the claim is stale and the next read finishes the close.
    env["clock"].now = datetime(2026, 11, 3, 11, 10, tzinfo=timezone.utc)
    assert _report(env["service"])["periods"][0]["status"] == "closed"


# ── filters and indexes (§7.3, §9.3) ────────────────────────────────────────


def test_filters_on_open_and_closed_periods_agree(env):
    env["clock"].now = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)
    cases = [
        (ChargebackFilters(funding_key=FUND_1), {"E2", "E4"}),
        (ChargebackFilters(funding_key="central"), {"E1", "E5", "E7", "E8"}),
        (ChargebackFilters(group="4210"), {"E2", "E4", "E9"}),
        (ChargebackFilters(user_id="user-a"), {"E1", "E2", "E3"}),
        (ChargebackFilters(authorizer="OWNER1@example.com"), {"E2", "E4"}),
        (ChargebackFilters(mode="standing"), {"E3", "E6"}),
        (ChargebackFilters(mode="central"), {"E1", "E5", "E7", "E8"}),
        (ChargebackFilters(fields={"fund": "200000"}), {"E3", "E6"}),
    ]
    provisional = [set(_by_ext(_report(env["service"], f))) for f, _ in cases]
    env["clock"].now = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    _report(env["service"])  # close
    closed = [set(_by_ext(_report(env["service"], f))) for f, _ in cases]
    assert provisional == [want for _, want in cases]
    assert closed == [want for _, want in cases]


def test_each_index_family_answers_across_periods(env):
    env["clock"].now = datetime(2026, 12, 3, 12, 0, tzinfo=timezone.utc)
    _report(env["service"], periods=("2026-10", "2026-11"))
    service = env["service"]
    for filters, want in [
        (ChargebackFilters(funding_key=FUND_1), {("2026-10", "E2"), ("2026-10", "E4"), ("2026-11", "E2")}),
        (ChargebackFilters(user_id="user-c"), {("2026-10", "E5"), ("2026-10", "E6"), ("2026-11", "E6")}),
        (ChargebackFilters(authorizer="owner1@example.com"), {("2026-10", "E2"), ("2026-10", "E4"), ("2026-11", "E2")}),
        (ChargebackFilters(group="4300"), {("2026-10", "E3"), ("2026-10", "E6"), ("2026-11", "E3"), ("2026-11", "E6")}),
    ]:
        report = _report(service, filters, periods=("2026-10", "2026-11"))
        assert {(l["period"], l["extensionId"]) for l in report["lines"]} == want, filters


def test_charge_and_assignment_families_never_meet(env):
    _report(env["service"])  # close October
    table = boto3.resource("dynamodb").Table(TABLE)
    table.put_item(Item={
        "PK": "ASSIGNMENT#a1", "SK": "METADATA", "GSI2PK": "USER#user-a", "GSI2SK": "ASSIGNMENT#a1",
        "GSI1PK": "ASSIGNMENT_TYPE#direct_user", "GSI1SK": "PRIORITY#300#a1",
    })
    user_rows = table.query(IndexName="UserAssignmentIndex", KeyConditionExpression="GSI2PK = :pk",
                            ExpressionAttributeValues={":pk": "USER#user-a"})["Items"]
    assert [r["PK"] for r in user_rows] == ["ASSIGNMENT#a1"]
    charge_rows = asyncio.run(env["ledger"].index_lines("userId", "user-a", "2026-01", "2026-12"))
    assert {l.extension_id for l in charge_rows} == {"E1", "E2", "E3"}
    type_rows = table.query(IndexName="AssignmentTypeIndex", KeyConditionExpression="GSI1PK = :pk",
                            ExpressionAttributeValues={":pk": "ASSIGNMENT_TYPE#direct_user"})["Items"]
    assert [r["PK"] for r in type_rows] == ["ASSIGNMENT#a1"]


def test_funds_listing(env):
    _report(env["service"])  # close October
    funds = {f["fundingKey"]: f for f in asyncio.run(env["service"].funds())}
    assert set(funds) == {FUND_1, FUND_2, FUND_3}
    assert funds[FUND_1]["consumedClosed"] == "30.00"
    assert funds[FUND_1]["authorizerEmail"] == "owner1@example.com"
    assert funds[FUND_1]["firstPeriod"] == "2026-09" and funds[FUND_1]["lastPeriod"] == "2026-11"
    assert funds[FUND_2]["lastPeriod"] == "2027-06" and funds[FUND_2]["extensions"] == 2
    assert funds[FUND_1]["fields"] == {"fund": "100000", "org": "4210"} and funds[FUND_1]["label"] == ""


def test_a_label_only_chargeback_is_its_own_fund_not_central(env):
    """A deployment with no funding fields can charge back to a fund named only by its label."""
    labelled = _ext("L1", "user-l", 15, mode="chargeback", source=FundingSource(label="Physics grant"))
    asyncio.run(env["quotas"].create_extension(labelled))
    env["spend"]["user-l"] = "40"
    report = _report(env["service"])
    line = _by_ext(report)["L1"]
    assert line["fundingKey"] == "_label=Physics grant" and Decimal(line["consumed"]) == 10
    assert _fund_totals(report)["central"] == 25
    only = _report(env["service"], ChargebackFilters(funding_key="_label=Physics grant"))
    assert set(_by_ext(only)) == {"L1"}
    assert "_label=Physics grant" in {f["fundingKey"] for f in asyncio.run(env["service"].funds())}
    rows = list(csv.reader(io.StringIO(report_csv(report, ["fund", "org"]))))
    assert dict(zip(rows[0], next(r for r in rows if r[2] == "L1")))["fundingLabel"] == "Physics grant"


# ── CSV (§9.5) ──────────────────────────────────────────────────────────────


def test_csv_flattens_fields_by_schema_key(env):
    report = _report(env["service"])
    # A line written under an older schema carries a key the schema no longer lists.
    report["lines"][0] = {**report["lines"][0], "fields": {"fund": "=HYPERLINK(1)", "grant": "G-1"}}
    rows = list(csv.reader(io.StringIO(report_csv(report, ["fund", "org", "program"]))))
    header = rows[0]
    assert header[:5] == ["period", "userEmail", "extensionId", "requestId", "fundingMode"]
    assert header[5:9] == ["fund", "org", "program", "grant"]
    assert header[9:15] == ["authorizerEmail", "granted", "consumed", "approvedBy", "approvedAt", "revokedAt"]
    assert header[-1] == "periodStatus" and "fundingLabel" in header
    by_ext = {r[2]: dict(zip(header, r)) for r in rows[1:]}
    assert by_ext["E2"]["fund"] == "100000" and by_ext["E2"]["program"] == ""
    assert by_ext["E3"]["granted"] == "uncapped" and by_ext["E3"]["consumed"] == "15.00"
    assert by_ext["E9"]["baseLimitChanged"] == "true"
    assert {r["periodStatus"] for r in by_ext.values()} == {"closed"}
    first = dict(zip(header, rows[1]))
    assert first["fund"] == "'=HYPERLINK(1)", "a formula-looking value is neutralised"
    assert first["grant"] == "G-1"


# ── routes ─────────────────────────────────────────────────────────────────


@pytest.fixture
def client(env):
    app = FastAPI()
    app.include_router(admin_routes.router, prefix="/admin")
    app.dependency_overrides[admin_routes.require_quota_requests_admin] = lambda: ADMIN
    return TestClient(app)


def test_report_route_json_csv_filters_and_errors(client, env):
    body = client.get("/admin/quota-requests/chargeback", params={"period": PERIOD, "f_fund": "100000"}).json()
    assert {l["extensionId"] for l in body["lines"]} == {"E2", "E4"}

    resp = client.get("/admin/quota-requests/chargeback", params={"from": "2026-10", "to": "2026-10", "format": "csv"})
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    assert 'filename="chargeback_2026-10.csv"' in resp.headers["content-disposition"]
    assert resp.text.splitlines()[0].startswith("period,userEmail,extensionId,requestId,fundingMode,fund,org,")

    assert client.get("/admin/quota-requests/chargeback", params={"period": "2026-13"}).status_code == 400
    assert client.get("/admin/quota-requests/chargeback", params={"from": "2026-11", "to": "2026-10"}).status_code == 400
    assert client.get("/admin/quota-requests/chargeback", params={"period": "2027-01"}).status_code == 400
    # No period: the last complete month on the service's clock.
    default = client.get("/admin/quota-requests/chargeback").json()
    assert [p["period"] for p in default["periods"]] == [PERIOD]

    funds = client.get("/admin/quota-requests/chargeback/funds").json()["funds"]
    assert {f["fundingKey"] for f in funds} == {FUND_1, FUND_2, FUND_3}


def test_reclose_route(client, env):
    assert client.post(f"/admin/quota-requests/chargeback/{PERIOD}/close").status_code == 200
    resp = client.post(f"/admin/quota-requests/chargeback/{PERIOD}/close")
    assert resp.status_code == 200 and resp.json()["period"]["closeVersion"] == 2
    assert resp.json()["changedFunds"] == []
    assert len(env["audit"].records) == 2
    assert client.post("/admin/quota-requests/chargeback/bad/close").status_code == 400


def test_routes_404_while_the_flag_is_off(client, monkeypatch):
    monkeypatch.setenv("QUOTA_REQUESTS_ENABLED", "false")
    assert client.get("/admin/quota-requests/chargeback").status_code == 404
    assert client.get("/admin/quota-requests/chargeback/funds").status_code == 404
    assert client.post(f"/admin/quota-requests/chargeback/{PERIOD}/close").status_code == 404
