"""Admin API routes for direct user grants.

**Non-delegable — these routes keep bare ``require_admin`` on purpose.**
A grant is written per user, and nothing stops the writer naming
themselves: anyone who could reach this surface could hand themselves any
tool, model or skill, which is role editing by another route. The matching
registry entry is ``admin.user_grants``, marked ``delegable=False`` in
``apis/shared/rbac/admin_scopes.py``, and
``tests/architecture/test_admin_scope_coverage.py`` lists this module among
the non-delegable ones.

Mounted only while ``USER_GRANTS_ENABLED`` (``apis.shared.feature_flags``):
the feature is in development, so a deployment turns the surface on by
choice. Resolution reads existing grants regardless — see the flag's
docstring for why that is the off state.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from apis.shared.auth import User, require_admin
from apis.shared.rbac.models import (
    UserGrantHoldersResponse,
    UserGrantListResponse,
    UserGrantResponse,
    UserGrantUpdate,
)
from apis.shared.rbac.user_grant_admin_service import (
    GRANT_KINDS,
    UnknownUserError,
    UserGrantValidationError,
    get_user_grant_admin_service,
)
from apis.shared.timestamps import utc_now_iso

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/user-grants", tags=["admin-user-grants"])


@router.get("", response_model=UserGrantListResponse)
async def list_user_grants(admin: User = Depends(require_admin)):
    """Every user holding a direct grant, expired ones included (flagged ``active``)."""
    service = get_user_grant_admin_service()
    now = utc_now_iso()
    grants = [
        UserGrantResponse.from_user_grant(g, now) for g in await service.list_grants()
    ]
    return UserGrantListResponse(grants=grants, total=len(grants))


# ⚠️ Must stay ABOVE `/{user_id}`: FastAPI matches in declaration order and a
# literal first segment would otherwise be read as a user id. Three segments
# here against one there keeps them apart whichever way they are declared,
# but the ordering is kept explicit so a future two-segment route is safe.
@router.get("/for/{kind}/{resource_id:path}", response_model=UserGrantHoldersResponse)
async def list_grant_holders(
    kind: str,
    resource_id: str,
    admin: User = Depends(require_admin),
):
    """The users directly granted one tool, model or skill.

    ``resource_id`` is a path segment because model ids carry ``.`` and ``:``.
    """
    if kind not in GRANT_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown grant kind '{kind}'. Expected one of: {', '.join(GRANT_KINDS)}.",
        )
    service = get_user_grant_admin_service()
    user_ids = await service.holders(kind, resource_id)
    return UserGrantHoldersResponse(kind=kind, resource_id=resource_id, user_ids=user_ids)


@router.get("/{user_id}", response_model=UserGrantResponse)
async def get_user_grant(user_id: str, admin: User = Depends(require_admin)):
    """One user's direct grant. A user with none gets the empty shape, not a 404."""
    service = get_user_grant_admin_service()
    grant = await service.get_grant(user_id)
    if grant is None:
        return UserGrantResponse.empty(user_id)
    return UserGrantResponse.from_user_grant(grant, utc_now_iso())


@router.put("/{user_id}", response_model=UserGrantResponse)
async def set_user_grant(
    user_id: str,
    body: UserGrantUpdate,
    admin: User = Depends(require_admin),
):
    """Replace one user's direct grant.

    Full replace: the stored grant becomes exactly the three lists posted.
    Posting three empty lists removes the grant.

    Returns 404 for an unknown user, 400 for an id no catalog knows, a scoped
    tool ref, ``"*"``, or an ``expiresAt`` in the past.
    """
    service = get_user_grant_admin_service()
    try:
        grant = await service.set_grant(user_id, body, admin)
    except UnknownUserError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"User {user_id} not found"
        )
    except UserGrantValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    if grant is None:
        return UserGrantResponse.empty(user_id)
    return UserGrantResponse.from_user_grant(grant, utc_now_iso())


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user_grant(user_id: str, admin: User = Depends(require_admin)):
    """Remove one user's direct grant. 404 when they have none."""
    service = get_user_grant_admin_service()
    deleted = await service.delete_grant(user_id, admin)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} has no direct grant",
        )
    return None
