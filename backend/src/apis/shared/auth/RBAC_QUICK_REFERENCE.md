# RBAC Quick Reference

## Import Statement

```python
from apis.shared.auth import User, require_admin, require_roles, require_all_roles, has_any_role, has_all_roles, get_current_user
```

## Common Patterns

### 1. Admin-Only Endpoint

```python
@router.get("/admin/feature")
async def admin_feature(admin: User = Depends(require_admin)):
    return {"message": f"Hello {admin.email}"}
```

### 2. Multiple Allowed Roles (OR)

```python
@router.get("/staff-area")
async def staff_area(user: User = Depends(require_roles("Staff", "Faculty", "Admin"))):
    return {"message": "Access granted"}
```

### 3. All Roles Required (AND)

```python
@router.post("/critical")
async def critical(user: User = Depends(require_all_roles("Admin", "Security"))):
    return {"message": "Critical access granted"}
```

### 4. Conditional Admin Features

```python
@router.get("/dashboard")
async def dashboard(user: User = Depends(get_current_user)):
    response = {"user": user.email}

    if has_any_role(user, "Admin", "SuperAdmin"):
        response["admin_panel"] = {...}

    return response
```

### 5. Role-Based Logic

```python
@router.get("/data")
async def get_data(user: User = Depends(get_current_user)):
    if has_any_role(user, "Admin"):
        # Return all data
        return get_all_data()
    elif has_any_role(user, "Faculty"):
        # Return faculty data
        return get_faculty_data(user.user_id)
    else:
        # Return user's own data
        return get_user_data(user.user_id)
```

## User Object Properties

```python
user.email       # "user@example.com"
user.user_id     # "123456789" (9-digit employee number)
user.name        # "John Doe"
user.roles       # ["Admin", "Faculty"]
user.picture     # "https://..." (optional)
```

## Predefined Role Checkers

```python
require_admin             # Admin or SuperAdmin
require_faculty           # Faculty
require_staff             # Staff
require_developer         # DotNetDevelopers
require_aws_ai_access     # AWS-BoiseStateAI
```

## Helper Functions

```python
# Check if user has any of the roles
if has_any_role(user, "Admin", "Faculty"):
    # Do something

# Check if user has all of the roles
if has_all_roles(user, "Admin", "Security"):
    # Do something critical
```

## HTTP Status Codes

- **200**: Success
- **401**: No token or invalid token
- **403**: Valid token but insufficient roles

## Error Responses

```json
// 401 Unauthorized
{
  "detail": "Authentication required. Please provide a valid Bearer token in the Authorization header."
}

// 403 Forbidden
{
  "detail": "Access denied. Required roles: Admin, SuperAdmin"
}
```

## Testing with curl

```bash
# With JWT token
curl -H "Authorization: Bearer YOUR_TOKEN_HERE" \
  http://localhost:8000/admin/me

# Check if endpoint is protected (should return 401)
curl http://localhost:8000/admin/me
```

## Common Roles in System

Based on `jwt_validator.py:69-77`:

- `Admin` / `SuperAdmin` - Administrative access
- `Faculty` - Faculty member
- `Staff` - Staff member
- `PSSTUCURTERM` - Current student
- `DotNetDevelopers` - Developer access
- `All-Students Entra Sync` - All students
- `All-Employees Entra Sync` - All employees
- `AWS-BoiseStateAI` - AWS AI platform access

## Creating Custom Role Checker

```python
# In your routes file
require_student = require_roles("PSSTUCURTERM", "All-Students Entra Sync")

@router.get("/student-portal")
async def student_portal(student: User = Depends(require_student)):
    return {"message": "Student access granted"}
```

## Logging Best Practice

```python
import logging
logger = logging.getLogger(__name__)

@router.post("/admin/action")
async def admin_action(admin: User = Depends(require_admin)):
    logger.info(f"Admin action performed by {admin.email} ({admin.user_id})")
    # ... perform action
    return {"success": True}
```

## Development Mode

Disable authentication for local testing:

```bash
# .env
ENABLE_AUTHENTICATION=false
```

**Warning:** This returns an anonymous user with **no roles**, so role-protected endpoints will still return 403.
