# Shared Authentication Module

This module provides shared authentication utilities that can be used by both `app_api` and `agent_api` projects.

## Usage

### From app_api or agent_api

Since both APIs add `src` to the Python path in their `main.py` files, you can import from `apis.shared.auth`:

```python
# Import authentication dependencies
from apis.shared.auth import get_current_user, User, get_validator

# Import state store utilities
from apis.shared.auth import create_state_store, StateStore

# Use in FastAPI routes
from fastapi import APIRouter, Depends
from apis.shared.auth import get_current_user

router = APIRouter()

@router.get("/protected")
async def protected_route(user: User = Depends(get_current_user)):
    return {"message": f"Hello, {user.email}!"}
```

### Available Exports

- `get_current_user`: FastAPI dependency for extracting and validating JWT tokens
- `User`: User dataclass with email, empl_id, name, roles, and picture
- `get_validator`: Get the global JWT validator instance
- `EntraIDJWTValidator`: JWT validator class for Entra ID tokens
- `create_state_store`: Factory function to create appropriate state store
- `StateStore`: Abstract base class for state storage
- `InMemoryStateStore`: In-memory state store (for local development)
- `DynamoDBStateStore`: DynamoDB-based state store (for production)

## Dependencies

The shared auth module requires:
- `fastapi`
- `PyJWT` (for JWT validation)
- `python-dotenv` (optional, for .env file loading)
- `boto3` (optional, only if using DynamoDBStateStore)

These should be included in each API's `requirements.txt` file.

