"""User routes for quota requests (docs/specs/quota-requests-and-extensions.md §10.1).

    GET   /quota/policy          what the request dialog offers
    GET   /quota/requests        the caller's requests, newest first
    POST  /quota/requests        submit; 409 {openRequestId} while one is open
    PATCH /quota/requests/{id}   add a funding source (info_requested) or withdraw

All 404 to a signed-in user while ``QUOTA_REQUESTS_ENABLED`` is off (401
unauthenticated regardless). A request is owner-scoped: another user's id is
a 404.
"""

from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from apis.shared.auth import User, get_current_user_from_session
from apis.shared.feature_flags import quota_requests_enabled
from apis.shared.quota_requests.models import (
    QuotaRequest,
    QuotaRequestCreate,
    QuotaRequestPatch,
    UserFacingPolicy,
)
from apis.shared.quota_requests.service import (
    QuotaRequestError,
    get_quota_request_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/quota", tags=["quota-requests"])


async def require_quota_requests_user(
    user: User = Depends(get_current_user_from_session),
) -> User:
    """The signed-in user, or 404 while the flag is off. Authentication runs first."""
    if not quota_requests_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return user


def error_response(e: QuotaRequestError) -> JSONResponse:
    return JSONResponse(status_code=e.status_code, content={"detail": e.message, **e.extra})


@router.get("/policy", response_model=UserFacingPolicy)
async def get_policy(user: User = Depends(require_quota_requests_user)):
    policy = await get_quota_request_service().get_policy()
    return UserFacingPolicy.from_policy(policy)


@router.get("/requests", response_model=List[QuotaRequest])
async def list_my_requests(user: User = Depends(require_quota_requests_user)):
    return await get_quota_request_service().list_my_requests(user)


@router.post("/requests", response_model=QuotaRequest, status_code=status.HTTP_201_CREATED)
async def submit_request(body: QuotaRequestCreate, user: User = Depends(require_quota_requests_user)):
    try:
        return await get_quota_request_service().submit(user, body)
    except QuotaRequestError as e:
        return error_response(e)


@router.patch("/requests/{request_id}", response_model=QuotaRequest)
async def patch_request(
    request_id: str, body: QuotaRequestPatch, user: User = Depends(require_quota_requests_user)
):
    try:
        return await get_quota_request_service().patch(user, request_id, body)
    except QuotaRequestError as e:
        return error_response(e)
