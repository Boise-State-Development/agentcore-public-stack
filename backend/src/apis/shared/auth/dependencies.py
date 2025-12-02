"""FastAPI dependencies for authentication."""

import logging
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from .jwt_validator import get_validator
from .models import User

logger = logging.getLogger(__name__)

# HTTP Bearer token security scheme with auto_error=False to handle missing tokens manually
security = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> User:
    """
    FastAPI dependency to get the current authenticated user.
    
    Extracts Bearer token from Authorization header and validates it.
    Returns 401 Unauthorized if token is missing or invalid.
    Returns 403 Forbidden if token is valid but user lacks required roles.
    
    Args:
        credentials: HTTP Bearer token credentials (None if missing)
        
    Returns:
        User object with authenticated user information
        
    Raises:
        HTTPException: 
            - 401 if token is missing or invalid
            - 403 if user doesn't have required roles
    """
    # Check if credentials are missing
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please provide a valid Bearer token in the Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    token = credentials.credentials
    validator = get_validator()
    
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

