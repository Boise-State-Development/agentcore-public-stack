"""Admin routes for quota requests and extensions (quota-requests spec §10.2).

Governed by its own delegable scope, ``admin.quota_requests``, so a
department approver can be given the queue without the tier editor
(``admin.quota``). The package boundary is the permission boundary
(``tests/architecture/test_admin_scope_coverage.py``).

Mounted always; every handler 404s while ``QUOTA_REQUESTS_ENABLED`` is off.
The flag is checked in the handler, after the scope dependency has
authenticated and authorized, so a signed-out caller still gets 401 and the
route-coverage test still sees the scope checker as each route's dependency.

The chargeback report (``/admin/quota-requests/chargeback*``, §9) reads the
ledger for closed periods and computes the open one; its first read of a
past period after the 48-hour grace window closes that period.
"""

from __future__ import annotations

import logging
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response

from agents.main_agent.quota.models import QuotaExtension
from apis.shared.auth import User, require_admin_scope
from apis.shared.feature_flags import quota_requests_enabled
from apis.shared.quota_requests.chargeback import (
    ChargebackError,
    ChargebackFilters,
    get_chargeback_service,
    periods_between,
    report_csv,
    validate_period,
)
from apis.shared.quota_requests.models import (
    ApproveRequestBody,
    ExtensionCreate,
    NoteBody,
    QuotaRequest,
    QuotaRequestPolicy,
    RequestStatus,
    RevokeBody,
)
from apis.shared.quota_requests.service import (
    QuotaRequestError,
    get_quota_request_service,
)
from apis.app_api.quota.routes import error_response

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-quota-requests"])

require_quota_requests_admin = require_admin_scope("admin.quota_requests")


def _ensure_enabled() -> None:
    if not quota_requests_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _chargeback_error(e: ChargebackError) -> JSONResponse:
    return JSONResponse(status_code=e.status_code, content={"detail": e.message, **e.extra})


# ⚠️ Literal paths (`/summary`, `/policy`, `/chargeback`) stay above `/{request_id}`.

@router.get("/quota-requests")
async def list_requests(
    status_filter: RequestStatus = Query("pending", alias="status"),
    limit: int = Query(50, ge=1, le=100),
    cursor: Optional[str] = None,
    admin: User = Depends(require_quota_requests_admin),
):
    _ensure_enabled()
    items, next_cursor = await get_quota_request_service().list_queue(status_filter, limit=limit, cursor=cursor)
    return {"requests": [r.model_dump(by_alias=True, mode="json") for r in items], "nextCursor": next_cursor}


@router.get("/quota-requests/summary")
async def requests_summary(admin: User = Depends(require_quota_requests_admin)):
    _ensure_enabled()
    return await get_quota_request_service().summary()


@router.get("/quota-requests/policy", response_model=QuotaRequestPolicy)
async def get_policy(admin: User = Depends(require_quota_requests_admin)):
    _ensure_enabled()
    return await get_quota_request_service().get_policy()


@router.put("/quota-requests/policy", response_model=QuotaRequestPolicy)
async def put_policy(body: QuotaRequestPolicy, admin: User = Depends(require_quota_requests_admin)):
    _ensure_enabled()
    try:
        return await get_quota_request_service().update_policy(admin, body)
    except QuotaRequestError as e:
        return error_response(e)


@router.get("/quota-requests/chargeback")
async def chargeback_report(
    request: Request,
    period: Optional[str] = None,
    from_period: Optional[str] = Query(None, alias="from"),
    to_period: Optional[str] = Query(None, alias="to"),
    funding_key: Optional[str] = Query(None, alias="fundingKey"),
    group: Optional[str] = None,
    user_id: Optional[str] = Query(None, alias="userId"),
    authorizer: Optional[str] = None,
    mode: Optional[Literal["central", "chargeback", "standing"]] = None,
    format: Literal["json", "csv"] = "json",
    admin: User = Depends(require_quota_requests_admin),
):
    """§9.3. ``period`` or ``from``/``to`` (default: the last complete month); any
    ``f_<field>=value`` filters on a funding field."""
    _ensure_enabled()
    service = get_chargeback_service()
    try:
        if period:
            periods = [validate_period(period)]
        elif from_period or to_period:
            periods = periods_between(from_period or to_period, to_period or from_period)
        else:
            periods = [service.default_period()]
        filters = ChargebackFilters(
            funding_key=funding_key or None,
            group=group or None,
            user_id=user_id or None,
            authorizer=authorizer or None,
            mode=mode,
            fields={k[2:]: v for k, v in request.query_params.items() if k.startswith("f_") and len(k) > 2 and v},
        )
        report = await service.report(periods, filters)
    except ChargebackError as e:
        return _chargeback_error(e)
    if format == "csv":
        schema_keys = [f["key"] for f in report["fundingFields"]]
        span = periods[0] if len(periods) == 1 else f"{periods[0]}_to_{periods[-1]}"
        return Response(
            content=report_csv(report, schema_keys),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="chargeback_{span}.csv"'},
        )
    return report


@router.get("/quota-requests/chargeback/funds")
async def chargeback_funds(admin: User = Depends(require_quota_requests_admin)):
    _ensure_enabled()
    return {"funds": await get_chargeback_service().funds()}


@router.post("/quota-requests/chargeback/{period}/close")
async def reclose_period(period: str, admin: User = Depends(require_quota_requests_admin)):
    """Recalculate a closed period (§9.4); audited with per-fund totals before and after."""
    _ensure_enabled()
    try:
        return await get_chargeback_service().reclose(admin, period)
    except ChargebackError as e:
        return _chargeback_error(e)


@router.get("/quota-requests/{request_id}")
async def get_request(request_id: str, admin: User = Depends(require_quota_requests_admin)):
    _ensure_enabled()
    service = get_quota_request_service()
    try:
        request = await service.get_request(request_id)
        return await service.request_detail(request)
    except QuotaRequestError as e:
        return error_response(e)


@router.post("/quota-requests/{request_id}/approve")
async def approve_request(
    request_id: str, body: ApproveRequestBody, admin: User = Depends(require_quota_requests_admin)
):
    _ensure_enabled()
    try:
        request, extension = await get_quota_request_service().approve(admin, request_id, body)
    except QuotaRequestError as e:
        return error_response(e)
    return {
        "request": request.model_dump(by_alias=True, mode="json"),
        "extension": extension.model_dump(by_alias=True, mode="json"),
    }


@router.post("/quota-requests/{request_id}/request-info", response_model=QuotaRequest)
async def request_info(request_id: str, body: NoteBody, admin: User = Depends(require_quota_requests_admin)):
    _ensure_enabled()
    try:
        return await get_quota_request_service().request_info(admin, request_id, body.note)
    except QuotaRequestError as e:
        return error_response(e)


@router.post("/quota-requests/{request_id}/deny", response_model=QuotaRequest)
async def deny_request(request_id: str, body: NoteBody, admin: User = Depends(require_quota_requests_admin)):
    _ensure_enabled()
    try:
        return await get_quota_request_service().deny(admin, request_id, body.note)
    except QuotaRequestError as e:
        return error_response(e)


@router.get("/quota-extensions", response_model=List[QuotaExtension])
async def list_extensions(
    user_id: Optional[str] = Query(None, alias="userId"),
    active: bool = False,
    admin: User = Depends(require_quota_requests_admin),
):
    _ensure_enabled()
    return await get_quota_request_service().list_extensions(user_id=user_id, active_only=active)


@router.post("/quota-extensions", response_model=QuotaExtension, status_code=status.HTTP_201_CREATED)
async def create_extension(body: ExtensionCreate, admin: User = Depends(require_quota_requests_admin)):
    _ensure_enabled()
    try:
        return await get_quota_request_service().create_direct_extension(admin, body)
    except QuotaRequestError as e:
        return error_response(e)


@router.post("/quota-extensions/{extension_id}/revoke", response_model=QuotaExtension)
async def revoke_extension(
    extension_id: str, body: RevokeBody, admin: User = Depends(require_quota_requests_admin)
):
    _ensure_enabled()
    try:
        return await get_quota_request_service().revoke(admin, extension_id, body.reason)
    except QuotaRequestError as e:
        return error_response(e)
