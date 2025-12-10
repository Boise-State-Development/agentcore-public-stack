# Role-Based Access Control (RBAC) Implementation Guide

## Overview

This document describes the RBAC implementation for the AgentCore Public Stack backend API, which enables role-based access control for admin and privileged endpoints using JWT tokens from Entra ID.

## Architecture

### Flow Diagram

```
JWT Token (from Entra ID)
    ↓
EntraIDJWTValidator (validates & extracts roles)
    ↓
User Model (email, user_id, name, roles[])
    ↓
FastAPI Dependency (require_admin, require_roles, etc.)
    ↓
Protected Route Handler
```

## Components

### 1. JWT Validator (`apis/shared/auth/jwt_validator.py`)

- Validates JWT tokens from Entra ID (Azure AD)
- Extracts user information including roles array
- Location: `jwt_validator.py:149` - Role extraction

### 2. User Model (`apis/shared/auth/models.py`)

```python
@dataclass
class User:
    email: str
    user_id: str
    name: str
    roles: List[str]  # ← Roles from JWT
    picture: Optional[str] = None
```

### 3. RBAC Module (`apis/shared/auth/rbac.py`)

**NEW - Created for this implementation**

Provides FastAPI dependencies for role-based access control:

#### Dependencies

- `require_roles(*roles)` - User must have at least ONE of the roles (OR logic)
- `require_all_roles(*roles)` - User must have ALL of the roles (AND logic)

#### Helper Functions

- `has_any_role(user, *roles)` - Check if user has any role (for conditional logic)
- `has_all_roles(user, *roles)` - Check if user has all roles (for conditional logic)

#### Predefined Checkers

- `require_admin` - Requires "Admin" or "SuperAdmin"
- `require_faculty` - Requires "Faculty"
- `require_staff` - Requires "Staff"
- `require_developer` - Requires "DotNetDevelopers"
- `require_aws_ai_access` - Requires "AWS-BoiseStateAI"

### 4. Admin Routes Module (`apis/app_api/admin/`)

**NEW - Created for this implementation**

Example implementation showing how to use RBAC in practice.

**Files:**
- `routes.py` - Admin endpoint implementations
- `models.py` - Pydantic models for admin responses
- `README.md` - Documentation and usage examples

## Usage Examples

### Basic Admin Endpoint

```python
from fastapi import APIRouter, Depends
from apis.shared.auth import User, require_admin

router = APIRouter(prefix="/admin", tags=["admin"])

@router.get("/stats")
async def get_stats(admin_user: User = Depends(require_admin)):
    """Only users with Admin or SuperAdmin role can access."""
    return {"stats": "..."}
```

### Custom Role Requirements

```python
from apis.shared.auth import require_roles

@router.post("/faculty-only")
async def faculty_endpoint(user: User = Depends(require_roles("Faculty", "Staff"))):
    """Requires Faculty OR Staff role."""
    return {"message": f"Access granted to {user.email}"}
```

### Multiple Required Roles (AND logic)

```python
from apis.shared.auth import require_all_roles

@router.post("/critical")
async def critical_endpoint(user: User = Depends(require_all_roles("Admin", "Security"))):
    """Requires BOTH Admin AND Security roles."""
    return {"message": "Access granted"}
```

### Conditional Features

```python
from apis.shared.auth import get_current_user, has_any_role

@router.get("/dashboard")
async def dashboard(user: User = Depends(get_current_user)):
    """All authenticated users can access, but admins see extra data."""
    response = {"user": user.email}

    if has_any_role(user, "Admin", "SuperAdmin"):
        response["admin_features"] = {...}

    return response
```

## Testing

### Local Testing with Docker

1. **Start the backend:**
   ```bash
   docker-compose up backend
   ```

2. **Test with JWT token:**
   ```bash
   curl -H "Authorization: Bearer <your-jwt-token>" \
     http://localhost:8000/admin/me
   ```

### Development Mode (Auth Disabled)

For local development without Entra ID setup:

1. **Set environment variable:**
   ```bash
   # backend/src/.env
   ENABLE_AUTHENTICATION=false
   ```

2. **Test without token:**
   ```bash
   curl http://localhost:8000/admin/me
   ```

**Note:** With auth disabled, user will have empty roles array, so role-protected endpoints will still return 403. This is by design.

### Testing Role-Protected Endpoints

When authentication is enabled, the JWT token must contain the required roles in the `roles` claim.

**Example JWT payload:**
```json
{
  "email": "admin@example.com",
  "name": "Admin User",
  "http://schemas.boisestate.edu/claims/employeenumber": "123456789",
  "roles": ["Admin", "Faculty", "AWS-BoiseStateAI"],
  "aud": "your-client-id",
  "iss": "https://login.microsoftonline.com/{tenant-id}/v2.0"
}
```

## Available Admin Endpoints

All endpoints require authentication. Admin endpoints require Admin or SuperAdmin role.

| Endpoint | Method | Required Role | Description |
|----------|--------|---------------|-------------|
| `/admin/me` | GET | Admin or SuperAdmin | Get admin user info |
| `/admin/sessions/all` | GET | Admin or SuperAdmin | List all sessions (all users) |
| `/admin/sessions/{id}` | DELETE | Admin or SuperAdmin | Delete any user's session |
| `/admin/stats` | GET | Admin or SuperAdmin | Get system statistics |
| `/admin/users/{id}/sessions` | GET | Admin or SuperAdmin | Get specific user's sessions |
| `/admin/conditional-example` | GET | Any authenticated | Example with conditional features |
| `/admin/require-multiple-roles-example` | POST | Admin, SuperAdmin, or DotNetDevelopers | Multi-role example |

See `backend/src/apis/app_api/admin/README.md` for detailed endpoint documentation.

## HTTP Status Codes

| Code | Meaning | When It Occurs |
|------|---------|----------------|
| 200 | Success | Request succeeded |
| 401 | Unauthorized | No token provided or invalid token |
| 403 | Forbidden | Valid token but user lacks required role |
| 404 | Not Found | Resource doesn't exist |
| 500 | Server Error | Internal error |

## Error Response Format

### 401 Unauthorized
```json
{
  "detail": "Authentication required. Please provide a valid Bearer token in the Authorization header."
}
```

### 403 Forbidden
```json
{
  "detail": "Access denied. Required roles: Admin, SuperAdmin"
}
```

## Entra ID Configuration

### Required Environment Variables

```bash
# .env
ENTRA_TENANT_ID=your-tenant-id
ENTRA_CLIENT_ID=your-client-id
ENTRA_CLIENT_SECRET=your-client-secret
ENTRA_REDIRECT_URI=your-redirect-uri

# Optional - disable auth for development
ENABLE_AUTHENTICATION=true
```

### App Registration Setup

1. **Register application in Entra ID**
2. **Define app roles in app manifest:**
   ```json
   "appRoles": [
     {
       "id": "...",
       "allowedMemberTypes": ["User"],
       "displayName": "Admin",
       "value": "Admin",
       "description": "Administrator access"
     },
     {
       "id": "...",
       "allowedMemberTypes": ["User"],
       "displayName": "Faculty",
       "value": "Faculty",
       "description": "Faculty access"
     }
   ]
   ```
3. **Assign roles to users/groups**
4. **Configure token claims to include roles**

### Role Claim Location

The validator checks the `roles` claim in the JWT payload:
```python
roles = payload.get('roles', [])  # Line 149 in jwt_validator.py
```

## Adding New Role-Protected Endpoints

### Step 1: Import Dependencies

```python
from fastapi import APIRouter, Depends
from apis.shared.auth import User, require_admin, require_roles
```

### Step 2: Create Route with Dependency

```python
@router.post("/my-admin-feature")
async def my_feature(admin_user: User = Depends(require_admin)):
    logger.info(f"Admin {admin_user.email} accessed feature")

    # Access user properties
    user_email = admin_user.email
    user_id = admin_user.user_id
    user_roles = admin_user.roles

    return {"message": "Success"}
```

### Step 3: Handle Authorization

The dependency automatically:
- ✓ Validates JWT token
- ✓ Extracts user information
- ✓ Checks required roles
- ✓ Returns 403 if role check fails
- ✓ Injects User object into handler

## Security Best Practices

1. **Always use dependencies** - Never manually check roles
2. **Log admin actions** - Audit trail for compliance
3. **Use specific roles** - Prefer `require_admin` over `get_current_user` for sensitive operations
4. **Never disable auth in production** - `ENABLE_AUTHENTICATION=false` is for development only
5. **Validate on every request** - Stateless authentication, no sessions
6. **Use HTTPS in production** - Protect tokens in transit

## Future Enhancements

Potential improvements to the RBAC system:

- **Permission-based access control** - Map roles to specific permissions
- **Dynamic role configuration** - Store role mappings in database
- **Role hierarchies** - Admin inherits Staff permissions, etc.
- **Audit logging** - Track all admin actions with timestamps
- **Rate limiting by role** - Different limits for different user types
- **Temporary role elevation** - Time-limited admin access
- **Multi-tenancy** - Scope roles to organizations

## Troubleshooting

### Issue: "User does not have required role"

**Cause:** JWT token doesn't contain the required role in the `roles` claim.

**Solution:**
1. Check Entra ID app role assignments
2. Verify role is defined in app manifest
3. Ensure token includes `roles` claim
4. Check role spelling (case-sensitive)

### Issue: "Invalid token audience"

**Cause:** Token audience doesn't match expected client ID.

**Solution:**
1. Verify `ENTRA_CLIENT_ID` matches app registration
2. Check token was issued for correct application
3. See `jwt_validator.py:55-59` for acceptable audiences

### Issue: "Authentication service misconfigured"

**Cause:** Environment variables not set correctly.

**Solution:**
1. Verify `.env` file exists in `backend/src/`
2. Check `ENTRA_TENANT_ID` and `ENTRA_CLIENT_ID` are set
3. Restart backend after changing environment variables

## File References

- RBAC utilities: `backend/src/apis/shared/auth/rbac.py`
- JWT validation: `backend/src/apis/shared/auth/jwt_validator.py`
- User model: `backend/src/apis/shared/auth/models.py`
- Auth dependencies: `backend/src/apis/shared/auth/dependencies.py`
- Admin routes: `backend/src/apis/app_api/admin/routes.py`
- Admin README: `backend/src/apis/app_api/admin/README.md`

## Additional Resources

- [FastAPI Dependencies](https://fastapi.tiangolo.com/tutorial/dependencies/)
- [Microsoft Entra ID](https://learn.microsoft.com/en-us/entra/identity/)
- [JWT.io](https://jwt.io/) - Decode and inspect tokens
- [PyJWT Documentation](https://pyjwt.readthedocs.io/)
