"""Admin routes for AWS Limits: Service Quotas monitoring and increase requests.

GitHub issue #12. Governed by the delegable ``admin.aws_limits`` scope; the
package boundary is the permission boundary
(``tests/architecture/test_admin_scope_coverage.py``).

Mounted always; every handler 404s while ``AWS_LIMITS_ENABLED`` is off. The
flag is checked in the handler, after the scope dependency has authenticated
and authorized, so a signed-out caller still gets 401 and the route-coverage
test still sees the scope checker as each route's dependency.

Submitting an increase changes the AWS account. While the feature is tested in
development the POST uses the same scope as the reads.

TODO(aws-limits): before production, restrict POST /requests to system_admin
only. The architecture test forbids the bare full-admin guard in a scoped
package, so the POST moves to its own non-delegable module, and
``_can_request_increase`` below must change in the same commit so the SPA hides
the button for everyone else.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status

from apis.shared.auth import User, require_admin_scope
from apis.shared.aws_limits.models import (
    AwsLimit,
    AwsLimitsDashboard,
    IncreaseHistory,
    IncreaseRequest,
    IncreaseRequestRecord,
)
from apis.shared.aws_limits.service import AwsLimitsError, get_aws_limits_service
from apis.shared.feature_flags import aws_limits_enabled

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/aws-limits", tags=["admin-aws-limits"])

require_aws_limits_admin = require_admin_scope("admin.aws_limits")


def _ensure_enabled() -> None:
    if not aws_limits_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _can_request_increase(admin: User) -> bool:
    """Whether ``admin`` may submit an increase; tells the SPA to show the button.

    Development: anyone holding ``admin.aws_limits`` (the POST's own guard).
    TODO(aws-limits): system_admin only before production, with the POST.
    """
    return True


def _raise(e: AwsLimitsError) -> None:
    raise HTTPException(status_code=e.status_code, detail=e.message)


def _dump(model) -> dict:
    return model.model_dump(by_alias=True, mode="json")


# ⚠️ The literal `/requests` paths stay above `/{service_code}/{quota_code}`.


@router.get("", response_model=None)
async def get_dashboard(
    refresh: bool = Query(False),
    all_bedrock: bool = Query(False),
    admin: User = Depends(require_aws_limits_admin),
):
    """Grouped limits with usage, summary counts and the Automatic Management state."""
    _ensure_enabled()
    dashboard: AwsLimitsDashboard = await get_aws_limits_service().dashboard(
        all_bedrock=all_bedrock,
        refresh=refresh,
        can_request_increase=_can_request_increase(admin),
    )
    return _dump(dashboard)


@router.get("/requests", response_model=None)
async def get_request_history(
    refresh: bool = Query(False),
    admin: User = Depends(require_aws_limits_admin),
):
    """Increase requests AWS still lists, joined to who asked through this app."""
    _ensure_enabled()
    try:
        history: IncreaseHistory = await get_aws_limits_service().history(refresh=refresh)
    except AwsLimitsError as e:
        _raise(e)
    return _dump(history)


@router.post("/requests", response_model=None, status_code=status.HTTP_201_CREATED)
async def request_increase(
    body: IncreaseRequest,
    # TODO(aws-limits): system_admin only before production (see module docstring).
    admin: User = Depends(require_aws_limits_admin),
):
    """Ask Service Quotas for an increase. Audited as ``aws_limit.increase_requested``."""
    _ensure_enabled()
    try:
        record: IncreaseRequestRecord = await get_aws_limits_service().request_increase(admin, body)
    except AwsLimitsError as e:
        _raise(e)
    return _dump(record)


@router.get("/{service_code}/{quota_code}", response_model=None)
async def get_quota(
    service_code: str,
    quota_code: str,
    admin: User = Depends(require_aws_limits_admin),
):
    """One quota, read fresh: applied value, AWS default, usage and a console link."""
    _ensure_enabled()
    try:
        limit: AwsLimit = await get_aws_limits_service().quota_detail(service_code, quota_code)
    except AwsLimitsError as e:
        _raise(e)
    return _dump(limit)
