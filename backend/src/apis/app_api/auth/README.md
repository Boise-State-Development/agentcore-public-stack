# OIDC Authentication with Distributed State Management

This module provides OIDC authentication endpoints for Entra ID (Azure AD) with support for distributed state management.

## Problem: State Verification in Distributed Systems

In a distributed system with multiple API instances behind a load balancer:

1. **Request 1**: User calls `/auth/login` → hits **Instance A**
   - Instance A generates state token
   - Stores state in Instance A's memory
   
2. **Request 2**: User authenticates → callback to `/auth/token` → hits **Instance B**
   - Instance B doesn't have the state token in its memory
   - **Validation fails** ❌

## Solution: Distributed State Storage

The solution uses an abstraction layer (`StateStore`) that supports multiple backends:

### 1. DynamoDB State Store (Production)

Uses DynamoDB for distributed state storage across all instances.

**Features:**
- ✅ Works across multiple instances
- ✅ Automatic expiration via DynamoDB TTL
- ✅ Atomic get-and-delete operations (prevents race conditions)
- ✅ Consistent reads for immediate consistency

**Setup:**

1. Create DynamoDB table:
```bash
aws dynamodb create-table \
  --table-name oidc-state-store \
  --attribute-definitions AttributeName=state,AttributeType=S \
  --key-schema AttributeName=state,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --time-to-live-specification Enabled=true,AttributeName=expires_at
```

2. Set environment variable:
```bash
export DYNAMODB_OIDC_STATE_TABLE_NAME=oidc-state-store
```

**Table Schema:**
- **Partition Key**: `state` (String) - The state token
- **TTL Attribute**: `expires_at` (Number) - Unix timestamp for automatic cleanup
- **Attributes**:
  - `redirect_uri` (String, optional) - Stored redirect URI
  - `created_at` (Number) - Creation timestamp

### 2. In-Memory State Store (Development)

For local development or single-instance deployments.

**Features:**
- ✅ Simple, no dependencies
- ❌ **Does not work in distributed systems**
- ⚠️ State is lost on server restart

**Usage:**
- Automatically used when `DYNAMODB_OIDC_STATE_TABLE_NAME` is not set
- Logs a warning about distributed deployment limitations

## Configuration

The state store is automatically selected based on environment:

```python
# DynamoDB (production)
export DYNAMODB_OIDC_STATE_TABLE_NAME=oidc-state-store
export AWS_REGION=us-west-2
export AWS_PROFILE=dev-ai  # Optional

# In-memory (development)
# Just don't set DYNAMODB_OIDC_STATE_TABLE_NAME
```

## Security Features

1. **CSRF Protection**: Cryptographically secure state tokens (32 bytes)
2. **One-Time Use**: State tokens are deleted after validation (atomic operation)
3. **Expiration**: States expire after 10 minutes (configurable)
4. **Race Condition Prevention**: Atomic get-and-delete operations
5. **Consistent Reads**: Uses DynamoDB consistent reads for immediate consistency

## Alternative: Redis/ElastiCache

For even better performance, you could implement a Redis-based state store:

```python
class RedisStateStore(StateStore):
    def __init__(self, redis_url: str):
        import redis
        self.redis = redis.from_url(redis_url)
    
    def store_state(self, state: str, redirect_uri: Optional[str] = None, ttl_seconds: int = 600):
        key = f"oidc:state:{state}"
        value = json.dumps({"redirect_uri": redirect_uri})
        self.redis.setex(key, ttl_seconds, value)
    
    def get_and_delete_state(self, state: str) -> Tuple[bool, Optional[str]]:
        key = f"oidc:state:{state}"
        pipe = self.redis.pipeline()
        pipe.get(key)
        pipe.delete(key)
        result, _ = pipe.execute()
        
        if not result:
            return False, None
        
        data = json.loads(result)
        return True, data.get("redirect_uri")
```

## Architecture Decision

**Why DynamoDB?**
- ✅ Already used in the project
- ✅ Serverless, no infrastructure to manage
- ✅ Built-in TTL for automatic cleanup
- ✅ Atomic operations prevent race conditions
- ✅ Scales automatically
- ✅ No additional infrastructure costs (if already using DynamoDB)

**Why not Redis?**
- Requires additional infrastructure (ElastiCache)
- Additional cost
- More complex setup
- DynamoDB is sufficient for this use case (low volume, short TTL)

