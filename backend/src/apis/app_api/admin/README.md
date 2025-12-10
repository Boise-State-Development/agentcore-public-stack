# Admin API Module

This module provides role-based access control (RBAC) for administrative endpoints.

## Overview

The admin module demonstrates how to use the shared authentication RBAC utilities to create protected endpoints that require specific roles from JWT tokens.

## Architecture

### JWT Role Extraction

Roles are automatically extracted from the JWT token by `EntraIDJWTValidator` (`apis/shared/auth/jwt_validator.py:149`) and populated in the `User` model (`apis/shared/auth/models.py:13`).

### RBAC Dependencies

The shared auth module provides FastAPI dependencies for role checking:

- `require_roles(*roles)` - User must have at least ONE of the specified roles (OR logic)
- `require_all_roles(*roles)` - User must have ALL of the specified roles (AND logic)
- `has_any_role(user, *roles)` - Helper function for conditional logic
- `has_all_roles(user, *roles)` - Helper function for conditional logic

### Predefined Role Checkers

For convenience, common role checkers are available:

- `require_admin` - Requires "Admin" or "SuperAdmin" role
- `require_faculty` - Requires "Faculty" role
- `require_staff` - Requires "Staff" role
- `require_developer` - Requires "DotNetDevelopers" role
- `require_aws_ai_access` - Requires "AWS-BoiseStateAI" role

## Usage Examples

### Basic Admin Endpoint

```python
from fastapi import APIRouter, Depends
from apis.shared.auth import User, require_admin

router = APIRouter(prefix="/admin", tags=["admin"])

@router.get("/stats")
async def get_stats(admin_user: User = Depends(require_admin)):
    """Admin-only endpoint. Requires Admin or SuperAdmin role."""
    return {"stats": "..."}
```

### Custom Role Requirements

```python
from apis.shared.auth import require_roles

@router.post("/faculty-portal")
async def faculty_endpoint(user: User = Depends(require_roles("Faculty", "Staff"))):
    """Requires Faculty OR Staff role."""
    return {"message": "Access granted"}
```

### Multiple Required Roles

```python
from apis.shared.auth import require_all_roles

@router.post("/critical-operation")
async def critical_endpoint(user: User = Depends(require_all_roles("Admin", "Security"))):
    """Requires BOTH Admin AND Security roles."""
    return {"message": "Access granted"}
```

### Conditional Admin Features

```python
from apis.shared.auth import get_current_user, has_any_role

@router.get("/dashboard")
async def dashboard(user: User = Depends(get_current_user)):
    """Available to all authenticated users, with extra data for admins."""
    response = {"user": user.email}

    # Add admin-specific data conditionally
    if has_any_role(user, "Admin", "SuperAdmin"):
        response["admin_data"] = {"debug_info": "..."}

    return response
```

## Available Endpoints

### `GET /admin/me`
Get information about the current admin user.

**Required Role:** Admin or SuperAdmin

**Response:**
```json
{
  "email": "admin@example.com",
  "user_id": "123456789",
  "name": "Admin User",
  "roles": ["Admin", "Faculty"],
  "picture": "https://..."
}
```

### `GET /admin/sessions/all`
List all sessions across all users (for monitoring/support).

**Required Role:** Admin or SuperAdmin

**Query Parameters:**
- `limit` (optional): Maximum sessions to return (1-1000, default 100)
- `next_token` (optional): Pagination token

**Response:**
```json
{
  "sessions": [...],
  "total_count": 42,
  "next_token": "..."
}
```

### `DELETE /admin/sessions/{session_id}`
Delete any user's session (for abuse handling, privacy requests).

**Required Role:** Admin or SuperAdmin

**Response:**
```json
{
  "success": true,
  "session_id": "abc123",
  "message": "Session deleted by admin user@example.com"
}
```

### `GET /admin/stats`
Get system-wide statistics.

**Required Role:** Admin or SuperAdmin

**Response:**
```json
{
  "total_users": 150,
  "total_sessions": 450,
  "active_sessions": 23,
  "total_messages": 12000,
  "stats_as_of": "2025-12-10T12:00:00Z"
}
```

### `GET /admin/users/{user_id}/sessions`
Get all sessions for a specific user (for support).

**Required Role:** Admin or SuperAdmin

**Query Parameters:**
- `limit` (optional): Maximum sessions to return (1-1000, default 100)
- `next_token` (optional): Pagination token

**Response:**
```json
{
  "sessions": [...],
  "next_token": "..."
}
```

### `GET /admin/conditional-example`
Example showing conditional admin features.

**Required Role:** Any authenticated user

**Response for regular users:**
```json
{
  "message": "Welcome!",
  "user_email": "user@example.com",
  "user_roles": ["Faculty"]
}
```

**Response for admins:**
```json
{
  "message": "Welcome!",
  "user_email": "admin@example.com",
  "user_roles": ["Admin", "Faculty"],
  "admin_data": {
    "debug_info": "Additional admin information",
    "system_health": "All systems operational"
  }
}
```

### `POST /admin/require-multiple-roles-example`
Example requiring one of multiple specific roles.

**Required Role:** Admin, SuperAdmin, or DotNetDevelopers

**Response:**
```json
{
  "message": "Access granted",
  "user": "dev@example.com",
  "matched_roles": ["DotNetDevelopers"]
}
```

## Error Responses

### 401 Unauthorized
Returned when no JWT token is provided or token is invalid.

```json
{
  "detail": "Authentication required. Please provide a valid Bearer token in the Authorization header."
}
```

### 403 Forbidden
Returned when user is authenticated but lacks required role(s).

```json
{
  "detail": "Access denied. Required roles: Admin, SuperAdmin"
}
```

## Testing

### With Authentication Enabled (Production)

Test with a valid JWT token in the Authorization header:

```bash
curl -H "Authorization: Bearer <your-jwt-token>" \
  http://localhost:8000/admin/me
```

### With Authentication Disabled (Development)

Set `ENABLE_AUTHENTICATION=false` in your `.env` file:

```bash
# .env
ENABLE_AUTHENTICATION=false
```

This bypasses authentication and returns an anonymous user:
- Email: `anonymous@local.dev`
- User ID: `anonymous`
- Name: `Anonymous User`
- Roles: `[]` (empty - will fail role checks)

**Note:** With authentication disabled and no roles, you'll still get 403 errors from role-protected endpoints. To test admin endpoints without authentication, you would need to modify the role checker or use mock data.

## Adding New Admin Endpoints

1. **Import dependencies:**
```python
from apis.shared.auth import User, require_admin, require_roles
```

2. **Add route with role dependency:**
```python
@router.post("/my-admin-feature")
async def my_admin_feature(admin_user: User = Depends(require_admin)):
    logger.info(f"Admin {admin_user.email} accessed feature")
    return {"message": "Success"}
```

3. **Access user information:**
```python
admin_user.email      # Email address
admin_user.user_id    # 9-digit employee number
admin_user.name       # Full name
admin_user.roles      # List of roles
admin_user.picture    # Profile picture URL (optional)
```

## Security Considerations

1. **Always validate roles server-side** - Never trust client-side role checks
2. **Log admin actions** - All admin operations should be logged for audit trails
3. **Use specific roles** - Prefer `require_admin` over `get_current_user` for sensitive operations
4. **Disable auth only in development** - Never set `ENABLE_AUTHENTICATION=false` in production
5. **Review JWT claims** - Ensure your Entra ID app registration includes role claims

## Role Configuration

Roles are configured in Entra ID (Azure AD) app registration:
1. Define app roles in the app manifest
2. Assign roles to users/groups
3. Roles appear in the JWT token's `roles` claim
4. Backend validates and extracts roles automatically

See `apis/shared/auth/jwt_validator.py` for role extraction logic.

## Future Enhancements

Potential additions to the admin module:

- User management (list, create, update, disable users)
- Audit log viewing
- System configuration management
- Usage analytics and reporting
- Session management (force logout, view active sessions)
- Cost tracking and budgeting
- Tool usage monitoring
- Rate limiting configuration
