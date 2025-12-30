"""JWT token validation for Entra ID OIDC."""

import logging
import os
from pathlib import Path
from typing import Optional
import jwt
from jwt import PyJWKClient
from fastapi import HTTPException, status

try:
    from dotenv import load_dotenv
except ImportError:
    # python-dotenv not installed, skip loading .env file
    load_dotenv = None

from .models import User

# Load .env file if python-dotenv is available (before validator initialization)
if load_dotenv:
    # Look for .env file in the python directory (shared location)
    python_env_path = Path(__file__).parent.parent.parent / '.env'
    if python_env_path.exists():
        load_dotenv(python_env_path)
    else:
        # Also check project root
        root_env_path = Path(__file__).parent.parent.parent.parent / '.env'
        if root_env_path.exists():
            load_dotenv(root_env_path)

logger = logging.getLogger(__name__)


class EntraIDJWTValidator:
    """Validates JWT tokens from Entra ID (Azure AD)."""
    
    def __init__(self):
        """Initialize validator with configuration from environment."""
        self.tenant_id = os.getenv('ENTRA_TENANT_ID')
        self.client_id = os.getenv('ENTRA_CLIENT_ID')
        
        if not self.tenant_id or not self.client_id:
            raise ValueError(
                "ENTRA_TENANT_ID and ENTRA_CLIENT_ID environment variables are required"
            )
        
        # Use OIDC v2.0 endpoints (matches NestJS configuration)
        self.issuer = f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"
        self.jwks_uri = f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys"
        
        # Acceptable audiences - only accept proper API access tokens
        self.acceptable_audiences = [
            f"api://{self.client_id}",
        ]

        # Required scope for API access
        self.required_scope = "Read"
        
        # Initialize JWKS client with caching (matches NestJS jwks-rsa config)
        self.jwks_client = PyJWKClient(
            self.jwks_uri,
            cache_keys=True,
            max_cached_keys=5
        )
        
        # Required roles for access
        self.required_roles = [
            'Faculty',
            'Staff',
            'PSSTUCURTERM',
            'DotNetDevelopers',
            'All-Students Entra Sync',
            'All-Employees Entra Sync',
            'AWS-BoiseStateAI',
        ]
    
    def validate_token(self, token: str) -> User:
        """
        Validate JWT token and extract user information.

        Validation rules:
        - Issuer: https://login.microsoftonline.com/{tenant_id}/v2.0
        - Audience: api://{client_id} (access tokens only)
        - Algorithms: RS256
        - Expiration: Enforced with 60 second clock skew tolerance

        Args:
            token: JWT token string

        Returns:
            User object with extracted information

        Raises:
            HTTPException: If token is invalid, expired, or user doesn't have required role
        """
        try:
            # First, decode without verification to inspect the token
            unverified = jwt.decode(token, options={"verify_signature": False})
            token_audience = unverified.get('aud')
            token_issuer = unverified.get('iss')
            
            logger.debug(f"Token audience: {token_audience}, issuer: {token_issuer}")
            
            # Get signing key from JWKS
            signing_key = self.jwks_client.get_signing_key_from_jwt(token)
            
            # Validate audience - accept any of the acceptable audiences
            # This handles both ID tokens (aud=client_id) and access tokens (aud=api://client_id)
            def verify_audience(payload):
                aud = payload.get('aud')
                if isinstance(aud, str):
                    return aud in self.acceptable_audiences
                elif isinstance(aud, list):
                    return any(a in self.acceptable_audiences for a in aud)
                return False
            
            # Decode and validate token with custom audience verification
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=['RS256'],
                issuer=self.issuer,
                options={
                    "verify_signature": True,
                    "verify_aud": False,  # We'll verify audience manually
                    "verify_iss": True,
                    "verify_exp": True,
                },
                leeway=60  # Allow 60 seconds clock skew
            )
            
            # Manual audience verification
            if not verify_audience(payload):
                logger.warning(
                    f"Token audience '{token_audience}' not in acceptable audiences: {self.acceptable_audiences}"
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Invalid token audience. Expected one of: {self.acceptable_audiences}"
                )

            # Validate scope claim (scp) for access tokens
            scp_claim = payload.get('scp', '')
            token_scopes = scp_claim.split() if scp_claim else []

            logger.debug(f"Token scp claim: '{scp_claim}', parsed scopes: {token_scopes}")

            if self.required_scope not in token_scopes:
                logger.warning(
                    f"Token missing required scope '{self.required_scope}'. "
                    f"Token scp claim: '{scp_claim}', parsed scopes: {token_scopes}"
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Token missing required scope: {self.required_scope}"
                )

            # Extract user information
            email = payload.get('email') or payload.get('preferred_username')
            name = payload.get('name') or (
                f"{payload.get('given_name', '')} {payload.get('family_name', '')}"
            ).strip()
            user_id = payload.get('http://schemas.boisestate.edu/claims/employeenumber')
            roles = payload.get('roles', [])
            picture = payload.get('picture')
            
            # Validate emplId is a 9-digit number
            if not user_id or not user_id.isdigit() or len(user_id) != 9:
                logger.warning(f"Invalid emplId for user: {email}")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid user."
                )
            
            # Validate required roles
            has_required_role = any(role in self.required_roles for role in roles)
            if not has_required_role:
                logger.warning(f"User {email} does not have required role")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="User does not have required role."
                )
            
            return User(
                email=email.lower() if email else "",
                name=name,
                user_id=user_id,
                roles=roles,
                picture=picture
            )
            
        except jwt.InvalidSignatureError as e:
            logger.error(f"Invalid token signature: {e}")
            logger.debug(f"Token header: {jwt.get_unverified_header(token) if token else 'N/A'}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token signature. Token may be malformed or signed by different issuer."
            )
        except jwt.InvalidAudienceError as e:
            logger.error(f"Invalid token audience: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid token audience. Expected one of: {self.acceptable_audiences}"
            )
        except jwt.InvalidIssuerError as e:
            logger.error(f"Invalid token issuer: {e}")
            logger.debug(f"Expected issuer: {self.issuer}, JWKS URI: {self.jwks_uri}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid token issuer. Expected: {self.issuer}"
            )
        except jwt.InvalidTokenError as e:
            logger.error(f"Invalid token: {e}")
            # Try to decode without verification to get more info
            try:
                unverified = jwt.decode(token, options={"verify_signature": False})
                logger.debug(f"Unverified token payload keys: {list(unverified.keys())}")
                logger.debug(f"Unverified token audience: {unverified.get('aud')}")
                logger.debug(f"Unverified token issuer: {unverified.get('iss')}")
            except Exception:
                pass
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token."
            )
        except jwt.ExpiredSignatureError:
            logger.warning("Token expired")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token expired. Please refresh your session."
            )
        except Exception as e:
            logger.error(f"Error validating token: {e}", exc_info=True)
            # Log token details for debugging (without exposing full token)
            try:
                unverified = jwt.decode(token, options={"verify_signature": False})
                logger.debug(f"Token type: {unverified.get('typ')}, alg: {unverified.get('alg')}")
                logger.debug(f"Token issuer: {unverified.get('iss')}, audience: {unverified.get('aud')}")
            except Exception:
                pass
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token validation failed."
            )


# Global validator instance
_validator: Optional[EntraIDJWTValidator] = None

# Check if authentication is enabled (defaults to true for security)
ENABLE_AUTHENTICATION = os.environ.get('ENABLE_AUTHENTICATION', 'true').lower() == 'true'


def get_validator() -> Optional[EntraIDJWTValidator]:
    """
    Get or create the global validator instance.
    
    Returns None if authentication is disabled via ENABLE_AUTHENTICATION=false.
    This allows the validator to skip initialization when Entra ID env vars are missing.
    """
    global _validator
    
    # If authentication is disabled, don't initialize validator
    if not ENABLE_AUTHENTICATION:
        return None
    
    if _validator is None:
        _validator = EntraIDJWTValidator()
    return _validator

