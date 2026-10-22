"""Keeps admin-added identity providers on the Cognito BFF App Client.

Admins add federated providers at runtime (Admin -> Auth Providers), and
app-api writes them onto the App Client's ``SupportedIdentityProviders``.
CloudFormation doesn't know about them. When a PlatformStack deploy changes
any property of the App Client (callback or logout URLs, scopes...),
CloudFormation calls UpdateUserPoolClient with the template's full property
set, and Cognito resets every field to what the request says. The
template lists only COGNITO plus ``CDK_COGNITO_SUPPORTED_IDPS``, so the
runtime-added providers drop off. Their sign-in button still shows (the SPA
reads DynamoDB), but Cognito rejects the authorize request.

app-api heals this itself, so a fork needs no configuration and no
post-deploy step: it reconciles once at startup, then every
``RECONCILE_INTERVAL_SECONDS``. Each pass reads the client and writes only
when an enabled provider is missing, so the steady-state cost is one
DescribeUserPoolClient and one small DynamoDB query per task per interval.
"""

import asyncio
import logging
import os
import random
from typing import Awaitable, Callable, List, Optional

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

RECONCILE_INTERVAL_SECONDS = 60.0
# Spreads the passes of several app-api tasks, so they rarely race to write
# the same client (a race is harmless: both write the same list, and the
# loser's ConcurrentModificationException is retried next pass).
RECONCILE_JITTER_SECONDS = 10.0


async def _default_reconcile() -> List[str]:
    # Checked before touching the service singleton, so a process with no
    # Cognito (local dev, tests) never builds one as a side effect.
    if not (os.getenv("COGNITO_USER_POOL_ID") and os.getenv("COGNITO_APP_CLIENT_ID")):
        return []

    from .service import get_auth_provider_service

    return await get_auth_provider_service().reconcile_app_client()


async def reconcile_once(
    reconcile: Optional[Callable[[], Awaitable[List[str]]]] = None,
) -> List[str]:
    """Run one pass, logging instead of raising. Returns the names added."""
    try:
        return await (reconcile or _default_reconcile)()
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("ConcurrentModificationException", "TooManyRequestsException"):
            logger.info(f"App Client reconcile deferred to the next pass ({code})")
        else:
            logger.error(f"App Client reconcile failed: {code or e}")
    except Exception:
        logger.exception("App Client reconcile failed")
    return []


async def run_reconcile_loop(
    *,
    interval_seconds: float = RECONCILE_INTERVAL_SECONDS,
    jitter_seconds: float = RECONCILE_JITTER_SECONDS,
    reconcile: Optional[Callable[[], Awaitable[List[str]]]] = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Reconcile now, then forever on an interval. Cancel the task to stop."""
    while True:
        await reconcile_once(reconcile)
        await sleep(interval_seconds + random.uniform(0, jitter_seconds))
