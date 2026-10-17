"""Whether the sign-in page offers the Cognito username/password button.

A system admin can hide the button once the organization signs in through a
federated provider (Entra, Okta, ...). Hiding is cosmetic: COGNITO stays on the
app client's supported identity providers, so the first-boot admin, who is a
Cognito user, can still sign in through the ``/auth/login?local=1`` break-glass
URL.

The setting is a *request*, not the answer. The button stays visible while no
federated provider is enabled, so disabling or deleting the last provider can
never leave a deployment with an empty sign-in page.
"""

import logging

from .repository import get_system_settings_repository

logger = logging.getLogger(__name__)


def cognito_login_visible(*, hide_requested: bool, enabled_provider_count: int) -> bool:
    """Resolve the button's visibility from the admin's request and the providers."""
    return not (hide_requested and enabled_provider_count > 0)


async def read_hide_cognito_login() -> bool:
    """Read the admin's request to hide the button. Any failure reads as "show"."""
    repo = get_system_settings_repository()
    if not repo.enabled:
        return False

    try:
        item = await repo.get_login_page_settings()
    except Exception as e:
        logger.warning(f"Could not read login-page settings, showing Cognito login: {e}")
        return False

    return bool(item and item.get("hideCognitoLogin"))
