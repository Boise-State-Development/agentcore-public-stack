"""FastAPI dependencies for authentication."""

import logging
import os
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from .jwt_validator import get_validator
from .models import User

logger = logging.getLogger(__name__)

# HTTP Bearer token security scheme with auto_error=False to handle missing tokens manually
security = HTTPBearer(auto_error=False)

# Check if authentication is enabled (defaults to true for security)
ENABLE_AUTHENTICATION = os.environ.get('ENABLE_AUTHENTICATION', 'true').lower() == 'true'


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> User:
    """
    FastAPI dependency to get the current authenticated user.
    
    Extracts Bearer token from Authorization header and validates it.
    Returns 401 Unauthorized if token is missing or invalid.
    Returns 403 Forbidden if token is valid but user lacks required roles.
    
    When ENABLE_AUTHENTICATION=false, bypasses authentication and returns
    an anonymous user object. This should only be used in development/testing.
    
    Args:
        credentials: HTTP Bearer token credentials (None if missing)
        
    Returns:
        User object with authenticated user information (or anonymous user if auth disabled)
        
    Raises:
        HTTPException: 
            - 401 if token is missing or invalid (when auth enabled)
            - 403 if user doesn't have required roles (when auth enabled)
    """
    # Check if authentication is disabled
    if not ENABLE_AUTHENTICATION:
        logger.warning("⚠️ Authentication is DISABLED via ENABLE_AUTHENTICATION=false - returning anonymous user")
        return User(
            email="anonymous@local.dev",
            user_id="anonymous",
            name="Anonymous User",
            roles=[],
            picture=None
        )
    
    # Check if credentials are missing
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please provide a valid Bearer token in the Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    token = credentials.credentials
    validator = get_validator()
    
    # Validator should always be available when auth is enabled
    if validator is None:
        logger.error("Validator is None but authentication is enabled - this should not happen")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication service misconfigured."
        )
    
    try:
        user = validator.validate_token(token)
        return user
    except HTTPException:
        # Re-raise HTTPExceptions (401 for invalid tokens, 403 for missing roles)
        raise
    except Exception as e:
        logger.error(f"Unexpected error in authentication: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed."
        )

