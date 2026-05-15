"""User settings API routes."""

from fastapi import APIRouter, Depends, HTTPException
import logging

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.user_settings.models import UserSettings, UserSettingsUpdate
from apis.shared.user_settings.repository import UserSettingsRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users/me/settings", tags=["user-settings"])


def get_user_settings_repository() -> UserSettingsRepository:
    """Get user settings repository instance."""
    return UserSettingsRepository()


@router.get("", response_model=UserSettings)
async def get_settings(
    current_user: User = Depends(get_current_user_from_session),
    repo: UserSettingsRepository = Depends(get_user_settings_repository),
):
    """Get the current user's settings."""
    logger.info(f"GET /users/me/settings - User: {current_user.user_id}")
    settings = await repo.get_settings(current_user.user_id)
    return UserSettings(**settings)


@router.put("", response_model=UserSettings)
async def update_settings(
    body: UserSettingsUpdate,
    current_user: User = Depends(get_current_user_from_session),
    repo: UserSettingsRepository = Depends(get_user_settings_repository),
):
    """Update the current user's settings (partial merge)."""
    logger.info(f"PUT /users/me/settings - User: {current_user.user_id}")

    update_data = body.model_dump(by_alias=True, exclude_unset=True)

    # Validate defaultModelId if provided and not null
    if "defaultModelId" in update_data and update_data["defaultModelId"] is not None:
        try:
            from apis.shared.models.managed_models import get_managed_model
            model = await get_managed_model(update_data["defaultModelId"])
            if model is None:
                logger.warning(
                    f"Model '{update_data['defaultModelId']}' not found in managed models table - saving anyway"
                )
        except RuntimeError:
            logger.warning("Managed models table not configured - skipping model validation")
        except Exception as e:
            logger.warning(f"Could not validate model ID: {e}")

    # Surface the missing-table case as a real 503 instead of silently
    # echoing the requested values back to the client. Previously the route
    # returned 200 with the new payload while persisting nothing, so the
    # SPA's "Saving..." indicator cleared and the user assumed success —
    # then the next page load showed defaultModelId=null because the GET
    # path falls through to the same disabled repo and returns defaults
    # (#161). Failing loud here lets the frontend show the user that the
    # backend is misconfigured rather than silently dropping their choice.
    if not repo.enabled:
        logger.error(
            "User settings update rejected: DYNAMODB_USER_SETTINGS_TABLE_NAME is not configured"
        )
        raise HTTPException(
            status_code=503,
            detail="User settings storage is not configured on this server.",
        )

    try:
        updated = await repo.update_settings(current_user.user_id, update_data)
        return UserSettings(**updated)
    except RuntimeError:
        logger.warning("User settings table not available - returning requested settings without persisting")
        return UserSettings(**update_data)
    except Exception as e:
        logger.error(f"Failed to update settings for user {current_user.user_id}: {e}")
        raise HTTPException(status_code=503, detail="Settings service temporarily unavailable.")
