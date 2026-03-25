"""System status and first-boot API routes."""

import logging
from datetime import datetime, timezone

from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException

from apis.shared.users.models import UserProfile, UserStatus
from apis.shared.users.repository import UserRepository

from .cognito_service import get_cognito_service
from .models import FirstBootRequest, FirstBootResponse, SystemStatusResponse
from .repository import get_system_settings_repository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/status")
async def get_system_status() -> SystemStatusResponse:
    """Check if first-boot has been completed. Public endpoint — no auth required."""
    try:
        repo = get_system_settings_repository()
        settings = await repo.get_first_boot_status()
        return SystemStatusResponse(
            first_boot_completed=settings is not None and settings.get("completed") is True,
        )
    except Exception:
        logger.exception("Failed to read first-boot status from DynamoDB")
        return SystemStatusResponse(first_boot_completed=False)


@router.post("/first-boot", status_code=200)
async def first_boot(request: FirstBootRequest) -> FirstBootResponse:
    """
    Create the initial admin user. One-time only — rejects if already completed.

    Public endpoint (no auth required). The flow:
    1. Atomic check via conditional DynamoDB write (rejects duplicates with 409)
    2. Create user in Cognito via AdminCreateUser + AdminSetUserPassword
    3. Create user record in Users DynamoDB table with system_admin role
    4. Mark first-boot completed in DynamoDB
    5. Disable self-signup on the Cognito User Pool
    """
    settings_repo = get_system_settings_repository()
    cognito = get_cognito_service()
    user_repo = UserRepository()

    # 1. Atomic check: if first-boot already completed, return 409
    try:
        existing = await settings_repo.get_first_boot_status()
        if existing is not None and existing.get("completed") is True:
            raise HTTPException(
                status_code=409,
                detail="First-boot has already been completed.",
            )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to check first-boot status")
        raise HTTPException(
            status_code=500,
            detail="Failed to check first-boot status.",
        )

    # 2. Create user in Cognito
    user_sub = ""
    try:
        user_sub = cognito.create_admin_user(
            username=request.username,
            email=request.email,
            password=request.password,
        )
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "InvalidPasswordException":
            raise HTTPException(
                status_code=400,
                detail=f"Password does not meet Cognito policy: {e.response['Error']['Message']}",
            )
        if error_code == "UsernameExistsException":
            raise HTTPException(
                status_code=409,
                detail="A user with that username already exists.",
            )
        logger.exception("Cognito AdminCreateUser failed")
        raise HTTPException(
            status_code=500,
            detail="Failed to create user in Cognito.",
        )

    # 3. Create user record in Users DynamoDB table with system_admin role
    now_iso = datetime.now(timezone.utc).isoformat()
    email_domain = request.email.split("@")[1] if "@" in request.email else ""

    # Add user to system_admin Cognito group so JWT includes the role
    try:
        cognito.add_user_to_group(request.username, "system_admin")
    except Exception:
        logger.exception("Failed to add user to system_admin Cognito group — rolling back")
        cognito.delete_user(request.username)
        raise HTTPException(
            status_code=500,
            detail="Failed to assign admin group. Cognito user has been rolled back.",
        )

    try:
        if user_repo.enabled:
            profile = UserProfile(
                userId=user_sub,
                email=request.email,
                name=request.username,
                roles=["system_admin"],
                emailDomain=email_domain,
                createdAt=now_iso,
                lastLoginAt=now_iso,
                status=UserStatus.ACTIVE,
            )
            await user_repo.create_user(profile)
    except Exception:
        logger.exception("Failed to create user record in DynamoDB — rolling back Cognito user")
        cognito.delete_user(request.username)
        raise HTTPException(
            status_code=500,
            detail="Failed to create user record. Cognito user has been rolled back.",
        )

    # 4. Mark first-boot completed in DynamoDB (conditional write)
    try:
        await settings_repo.mark_first_boot_completed(
            user_id=user_sub,
            username=request.username,
            email=request.email,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Race condition: another request completed first-boot between our
            # check and this write. Roll back the Cognito user.
            logger.warning("First-boot race condition detected — rolling back")
            cognito.delete_user(request.username)
            raise HTTPException(
                status_code=409,
                detail="First-boot was completed by another request.",
            )
        logger.exception("Failed to mark first-boot completed — rolling back")
        cognito.delete_user(request.username)
        raise HTTPException(
            status_code=500,
            detail="Failed to mark first-boot completed.",
        )

    # 5. Disable self-signup on the Cognito User Pool
    try:
        cognito.disable_self_signup()
    except Exception:
        # Non-fatal: first-boot succeeded, admin can disable manually
        logger.exception(
            "Failed to disable self-signup after first-boot. "
            "Admin should disable it manually via AWS console."
        )

    return FirstBootResponse(
        success=True,
        user_id=user_sub,
        message="First-boot completed. Admin user created successfully.",
    )
