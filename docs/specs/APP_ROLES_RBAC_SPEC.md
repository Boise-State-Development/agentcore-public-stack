# Application Roles (AppRole) RBAC System Specification

## Document Information

| Field | Value |
|-------|-------|
| Version | 1.0 |
| Status | Draft |
| Created | 2025-01-XX |
| Author | AI Assistant |

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Problem Statement](#2-problem-statement)
3. [Design Goals](#3-design-goals)
4. [System Architecture](#4-system-architecture)
5. [Data Models](#5-data-models)
6. [DynamoDB Schema](#6-dynamodb-schema)
7. [Caching Strategy](#7-caching-strategy)
8. [Authorization Flow](#8-authorization-flow)
9. [Admin Access Control](#9-admin-access-control)
10. [API Design](#10-api-design)
11. [Frontend Admin UI](#11-frontend-admin-ui)
12. [Future Integrations](#12-future-integrations)
13. [Implementation Phases](#13-implementation-phases)
14. [Security Considerations](#14-security-considerations)
15. [Configuration](#15-configuration)

---

## 1. Executive Summary

This specification defines an **Application Role (AppRole) system** that provides a centralized, flexible way to manage permissions across the AgentCore platform. The system allows administrators to:

- Create application-level roles that map to one or more JWT roles from the identity provider (Entra ID)
- Grant access to tools, models, and other resources through these AppRoles
- Support single-level role inheritance
- Maintain high-performance authorization through denormalized permissions and aggressive caching

### Key Features

- **JWT Role Mapping**: Map multiple identity provider roles to a single AppRole
- **Denormalized Permissions**: Pre-computed effective permissions for O(1) authorization
- **Bidirectional Sync**: Update permissions from either role or resource admin views
- **Single-Level Inheritance**: AppRoles can inherit from one or more parent roles
- **Cache-Friendly**: 5-10 minute TTL with manual invalidation support
- **Default Role Fallback**: Users without mapped roles receive a configurable default role

---

## 2. Problem Statement

### Current State

The system currently has implicit, scattered role definitions:

1. **JWT roles** come from Entra ID (e.g., `Faculty`, `Staff`, `DotNetDevelopers`)
2. **Backend code** references these roles directly in:
   - Route protection: `require_roles("Admin", "SuperAdmin", "DotNetDevelopers")`
   - Model RBAC: `available_to_roles: ["Faculty", "Developer"]`
   - Future tool RBAC: `allowed_roles: ["Faculty", "Researcher"]`

### Problems

| Problem | Impact |
|---------|--------|
| No central role registry | Admins can't see what roles exist or their permissions |
| Scattered permissions | To see "Faculty" access, query tools, models, quotas separately |
| No role abstraction | Can't create app roles that combine multiple JWT roles |
| No inheritance | Can't say "PowerUser includes all Faculty permissions" |
| Hard-coded admin access | Changing admin requirements needs code deployment |

---

## 3. Design Goals

### 3.1 Performance Requirements

| Operation | Target | Notes |
|-----------|--------|-------|
| Authorization check | < 5ms | O(R) where R = user's roles (typically 1-3) |
| Role lookup (cache hit) | < 1ms | In-memory cache |
| Role lookup (cache miss) | < 50ms | Single DynamoDB read |
| Permission merge | < 2ms | O(P) where P = total permissions |
| Role save (admin) | < 500ms | Includes permission recomputation |

### 3.2 Design Principles

1. **Read-heavy optimization**: Authorization checks happen on every request
2. **Write-rarely pattern**: Role definitions change infrequently (daily/weekly)
3. **Denormalization for speed**: Pre-compute effective permissions on save
4. **Cache-friendly**: Role definitions cached aggressively (5-10 min TTL)
5. **Eventual consistency**: Permission changes take effect after cache refresh

---

## 4. System Architecture

### 4.1 High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              Request Flow                                      │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                                │
│  User Request                                                                  │
│       │                                                                        │
│       ▼                                                                        │
│  ┌─────────────┐    ┌─────────────────┐    ┌───────────────────────────┐     │
│  │ JWT Token   │───▶│ Extract JWT     │───▶│ Cache Lookup              │     │
│  │ (Entra ID)  │    │ Roles           │    │ (5 min TTL)               │     │
│  └─────────────┘    └─────────────────┘    └───────────┬───────────────┘     │
│                                                         │                      │
│                            Cache Hit ◄─────────────────┤                      │
│                                 │                       │ Cache Miss           │
│                                 │                       ▼                      │
│                                 │            ┌───────────────────────┐        │
│                                 │            │ DynamoDB Query        │        │
│                                 │            │ (JwtRoleMappingIndex) │        │
│                                 │            └───────────┬───────────┘        │
│                                 │                        │                     │
│                                 ▼                        ▼                     │
│                        ┌─────────────────────────────────────────┐            │
│                        │ AppRole[] matching JWT roles            │            │
│                        └───────────────────┬─────────────────────┘            │
│                                            │                                   │
│                                            ▼                                   │
│                        ┌─────────────────────────────────────────┐            │
│                        │ Merge effective_permissions             │            │
│                        │ - Tools: Union (most permissive)        │            │
│                        │ - Models: Union (most permissive)       │            │
│                        │ - Quota: Highest priority tier wins     │            │
│                        └───────────────────┬─────────────────────┘            │
│                                            │                                   │
│                                            ▼                                   │
│                        ┌─────────────────────────────────────────┐            │
│                        │ UserEffectivePermissions                │            │
│                        │ (Cached for subsequent requests)        │            │
│                        └───────────────────┬─────────────────────┘            │
│                                            │                                   │
│                                            ▼                                   │
│                        ┌─────────────────────────────────────────┐            │
│                        │ Authorize Request                       │            │
│                        │ (Check tool/model access)               │            │
│                        └─────────────────────────────────────────┘            │
│                                                                                │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 4.2 Component Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Backend Components                                 │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐        │
│  │ AppRoleService   │   │ AppRoleCache     │   │ AppRoleRepository│        │
│  │                  │   │                  │   │                  │        │
│  │ - resolve_user   │◄─▶│ - get/set roles  │◄─▶│ - DynamoDB CRUD  │        │
│  │ - check_access   │   │ - invalidate     │   │ - GSI queries    │        │
│  │ - sync_bidirect  │   │ - TTL management │   │                  │        │
│  └──────────────────┘   └──────────────────┘   └──────────────────┘        │
│           │                                                                  │
│           ▼                                                                  │
│  ┌──────────────────────────────────────────────────────────────────┐      │
│  │                    PermissionResolver                              │      │
│  │                                                                    │      │
│  │  - Resolves inheritance (single level)                            │      │
│  │  - Computes effective_permissions on role save                    │      │
│  │  - Merges permissions for multi-role users                        │      │
│  └──────────────────────────────────────────────────────────────────┘      │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 5. Data Models

### 5.1 AppRole Model

```python
from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime
from enum import Enum


@dataclass
class EffectivePermissions:
    """Pre-computed permissions for fast authorization checks."""
    tools: List[str] = field(default_factory=list)      # Tool IDs user can access
    models: List[str] = field(default_factory=list)     # Model IDs user can access
    quota_tier: Optional[str] = None                    # Default quota tier for this role
    # FUTURE: features: List[str] = field(default_factory=list)  # Feature flags


@dataclass
class AppRole:
    """
    Application-level role that maps JWT roles to permissions.

    Permissions are denormalized (pre-computed) on save for fast runtime lookups.
    """
    # Primary identifiers
    role_id: str                          # Unique identifier (e.g., "power_user", "researcher")
    display_name: str                     # Human-readable name (e.g., "Power User")
    description: str                      # Description for admin UI

    # JWT Mapping
    jwt_role_mappings: List[str]          # JWT roles that grant this app role
                                          # e.g., ["Faculty", "Researcher", "GraduateStudent"]

    # Inheritance (single level only)
    inherits_from: List[str] = field(default_factory=list)  # Other app role IDs to inherit from

    # Denormalized permissions (computed on save)
    effective_permissions: EffectivePermissions = field(default_factory=EffectivePermissions)

    # Direct permission grants (before inheritance resolution)
    # These are used to compute effective_permissions
    granted_tools: List[str] = field(default_factory=list)   # Directly granted tool IDs
    granted_models: List[str] = field(default_factory=list)  # Directly granted model IDs

    # Metadata
    priority: int = 0                     # Higher priority role's quota tier wins in conflicts
    is_system_role: bool = False          # True for roles that cannot be deleted (e.g., system_admin)
    enabled: bool = True                  # Disabled roles are ignored during resolution

    # Audit fields
    created_at: str = ""                  # ISO 8601 timestamp
    updated_at: str = ""                  # ISO 8601 timestamp
    created_by: Optional[str] = None      # Admin user_id who created this role


@dataclass
class UserEffectivePermissions:
    """
    Merged permissions for a specific user based on all their AppRoles.

    This is computed at runtime and cached per-user.
    """
    user_id: str
    app_roles: List[str]                  # AppRole IDs that apply to this user
    tools: List[str]                      # Union of all tool permissions
    models: List[str]                     # Union of all model permissions
    quota_tier: Optional[str]             # Highest priority role's tier
    resolved_at: str                      # ISO 8601 timestamp when this was computed
```

### 5.2 Example Role Configurations

```python
# System Admin Role (hardcoded, cannot be deleted)
system_admin = AppRole(
    role_id="system_admin",
    display_name="System Administrator",
    description="Full access to all system features including RBAC management",
    jwt_role_mappings=[],  # Configured via ADMIN_JWT_ROLES env var
    inherits_from=[],
    effective_permissions=EffectivePermissions(
        tools=["*"],  # Wildcard = all tools
        models=["*"],  # Wildcard = all models
        quota_tier=None,  # No quota limits
    ),
    priority=1000,
    is_system_role=True,
    enabled=True,
)

# Default Role (fallback for unmapped JWT roles)
default_role = AppRole(
    role_id="default",
    display_name="Default User",
    description="Minimal access for users without specific role mappings",
    jwt_role_mappings=[],  # Special: applies when no other roles match
    inherits_from=[],
    effective_permissions=EffectivePermissions(
        tools=["calculator"],  # Only basic tools
        models=["claude-sonnet"],  # Only one model
        quota_tier="tier_basic",
    ),
    priority=0,
    is_system_role=True,  # Cannot be deleted, but can be modified
    enabled=True,
)

# Power User Role (typical configuration)
power_user = AppRole(
    role_id="power_user",
    display_name="Power User",
    description="Advanced users with access to code execution and research tools",
    jwt_role_mappings=["Faculty", "Researcher", "GraduateStudent"],
    inherits_from=["basic_user"],  # Inherits all basic_user permissions
    granted_tools=["code_interpreter", "browser_navigate", "deep_research"],
    granted_models=["claude-opus", "gpt-4o"],
    effective_permissions=EffectivePermissions(
        # Computed on save: basic_user tools + granted_tools
        tools=["calculator", "web_search", "code_interpreter", "browser_navigate", "deep_research"],
        models=["claude-sonnet", "claude-opus", "gpt-4o"],
        quota_tier="tier_faculty",
    ),
    priority=100,
    is_system_role=False,
    enabled=True,
)
```

---

## 6. DynamoDB Schema

### 6.1 Table: AppRoles

This table will be created in `infrastructure/lib/app-api-stack.ts`.

```typescript
// AppRoles Table - Role definitions and permission mappings
const appRolesTable = new dynamodb.Table(this, 'AppRolesTable', {
  tableName: getResourceName(config, 'app-roles'),
  partitionKey: {
    name: 'PK',
    type: dynamodb.AttributeType.STRING,
  },
  sortKey: {
    name: 'SK',
    type: dynamodb.AttributeType.STRING,
  },
  billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
  pointInTimeRecovery: true,
  removalPolicy: config.environment === 'prod'
    ? cdk.RemovalPolicy.RETAIN
    : cdk.RemovalPolicy.DESTROY,
  encryption: dynamodb.TableEncryption.AWS_MANAGED,
});

// GSI1: JwtRoleMappingIndex - Fast lookup: "Given JWT role X, what AppRoles apply?"
// This is the critical index for authorization performance
appRolesTable.addGlobalSecondaryIndex({
  indexName: 'JwtRoleMappingIndex',
  partitionKey: {
    name: 'GSI1PK',
    type: dynamodb.AttributeType.STRING,
  },
  sortKey: {
    name: 'GSI1SK',
    type: dynamodb.AttributeType.STRING,
  },
  projectionType: dynamodb.ProjectionType.ALL,
});

// GSI2: ToolRoleMappingIndex - Reverse lookup: "What AppRoles grant access to tool X?"
// Used for bidirectional sync when updating tool permissions
appRolesTable.addGlobalSecondaryIndex({
  indexName: 'ToolRoleMappingIndex',
  partitionKey: {
    name: 'GSI2PK',
    type: dynamodb.AttributeType.STRING,
  },
  sortKey: {
    name: 'GSI2SK',
    type: dynamodb.AttributeType.STRING,
  },
  projectionType: dynamodb.ProjectionType.INCLUDE,
  nonKeyAttributes: ['roleId', 'displayName', 'enabled'],
});

// GSI3: ModelRoleMappingIndex - Reverse lookup: "What AppRoles grant access to model X?"
// Used for bidirectional sync when updating model permissions
appRolesTable.addGlobalSecondaryIndex({
  indexName: 'ModelRoleMappingIndex',
  partitionKey: {
    name: 'GSI3PK',
    type: dynamodb.AttributeType.STRING,
  },
  sortKey: {
    name: 'GSI3SK',
    type: dynamodb.AttributeType.STRING,
  },
  projectionType: dynamodb.ProjectionType.INCLUDE,
  nonKeyAttributes: ['roleId', 'displayName', 'enabled'],
});

// Store table name in SSM
new ssm.StringParameter(this, 'AppRolesTableNameParameter', {
  parameterName: `/${config.projectPrefix}/rbac/app-roles-table-name`,
  stringValue: appRolesTable.tableName,
  description: 'AppRoles table name for RBAC',
  tier: ssm.ParameterTier.STANDARD,
});

new ssm.StringParameter(this, 'AppRolesTableArnParameter', {
  parameterName: `/${config.projectPrefix}/rbac/app-roles-table-arn`,
  stringValue: appRolesTable.tableArn,
  description: 'AppRoles table ARN',
  tier: ssm.ParameterTier.STANDARD,
});

// Grant permissions to ECS task
appRolesTable.grantReadWriteData(taskDefinition.taskRole);

// Add table name to container environment
environment: {
  // ... existing env vars ...
  DYNAMODB_APP_ROLES_TABLE_NAME: appRolesTable.tableName,
}
```

### 6.2 Access Patterns

| Pattern | Key Structure | Index | Use Case |
|---------|--------------|-------|----------|
| Get role by ID | `PK=ROLE#{role_id}`, `SK=DEFINITION` | Table | Admin edit role |
| List all roles | `PK=ROLE#*` | Scan (admin only) | Admin list view |
| JWT → AppRoles | `GSI1PK=JWT_ROLE#{jwt_role}`, `GSI1SK=ROLE#{role_id}` | JwtRoleMappingIndex | Authorization check |
| Tool → Roles | `GSI2PK=TOOL#{tool_id}`, `GSI2SK=ROLE#{role_id}` | ToolRoleMappingIndex | Bidirectional sync |
| Model → Roles | `GSI3PK=MODEL#{model_id}`, `GSI3SK=ROLE#{role_id}` | ModelRoleMappingIndex | Bidirectional sync |

### 6.3 Item Structures

#### Role Definition Item

```json
{
  "PK": "ROLE#power_user",
  "SK": "DEFINITION",
  "roleId": "power_user",
  "displayName": "Power User",
  "description": "Advanced users with access to code execution and research tools",
  "jwtRoleMappings": ["Faculty", "Researcher", "GraduateStudent"],
  "inheritsFrom": ["basic_user"],
  "grantedTools": ["code_interpreter", "browser_navigate", "deep_research"],
  "grantedModels": ["claude-opus", "gpt-4o"],
  "effectivePermissions": {
    "tools": ["calculator", "web_search", "code_interpreter", "browser_navigate", "deep_research"],
    "models": ["claude-sonnet", "claude-opus", "gpt-4o"],
    "quotaTier": "tier_faculty"
  },
  "priority": 100,
  "isSystemRole": false,
  "enabled": true,
  "createdAt": "2025-01-15T10:30:00Z",
  "updatedAt": "2025-01-15T14:22:00Z",
  "createdBy": "123456789"
}
```

#### JWT Role Mapping Items (for GSI1)

For each JWT role in `jwtRoleMappings`, create a mapping item:

```json
{
  "PK": "ROLE#power_user",
  "SK": "JWT_MAPPING#Faculty",
  "GSI1PK": "JWT_ROLE#Faculty",
  "GSI1SK": "ROLE#power_user",
  "roleId": "power_user",
  "enabled": true
}
```

#### Tool Permission Mapping Items (for GSI2)

For each tool in `grantedTools`, create a mapping item:

```json
{
  "PK": "ROLE#power_user",
  "SK": "TOOL_GRANT#code_interpreter",
  "GSI2PK": "TOOL#code_interpreter",
  "GSI2SK": "ROLE#power_user",
  "roleId": "power_user",
  "displayName": "Power User",
  "enabled": true
}
```

#### Model Permission Mapping Items (for GSI3)

For each model in `grantedModels`, create a mapping item:

```json
{
  "PK": "ROLE#power_user",
  "SK": "MODEL_GRANT#claude-opus",
  "GSI3PK": "MODEL#claude-opus",
  "GSI3SK": "ROLE#power_user",
  "roleId": "power_user",
  "displayName": "Power User",
  "enabled": true
}
```

---

## 7. Caching Strategy

### 7.1 Cache Layers

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            Cache Architecture                                │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  Layer 1: User Permissions Cache (per-user, short TTL)                      │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │ Key: user:{user_id}:permissions                                        │ │
│  │ Value: UserEffectivePermissions                                        │ │
│  │ TTL: 5 minutes                                                         │ │
│  │ Invalidation: On role change affecting user's JWT roles                │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
│                                                                              │
│  Layer 2: Role Cache (per-role, medium TTL)                                 │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │ Key: role:{role_id}                                                    │ │
│  │ Value: AppRole (with effective_permissions)                            │ │
│  │ TTL: 10 minutes                                                        │ │
│  │ Invalidation: On role update                                           │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
│                                                                              │
│  Layer 3: JWT Mapping Cache (per-JWT-role, medium TTL)                      │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │ Key: jwt_mapping:{jwt_role}                                            │ │
│  │ Value: List[role_id]                                                   │ │
│  │ TTL: 10 minutes                                                        │ │
│  │ Invalidation: On role JWT mapping change                               │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 7.2 Cache Implementation

```python
from typing import Dict, Optional, List
from datetime import datetime, timedelta
from dataclasses import dataclass
import asyncio
import logging

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """Cache entry with TTL tracking."""
    value: any
    expires_at: datetime

    @property
    def is_expired(self) -> bool:
        return datetime.utcnow() >= self.expires_at


class AppRoleCache:
    """
    In-memory cache for AppRole data with TTL support.

    Cache invalidation occurs:
    - Automatically when TTL expires
    - Manually when admin updates roles (via invalidate methods)
    - On application restart (cache is not persistent)
    """

    DEFAULT_USER_TTL = timedelta(minutes=5)
    DEFAULT_ROLE_TTL = timedelta(minutes=10)
    DEFAULT_MAPPING_TTL = timedelta(minutes=10)

    def __init__(self):
        self._user_cache: Dict[str, CacheEntry] = {}
        self._role_cache: Dict[str, CacheEntry] = {}
        self._jwt_mapping_cache: Dict[str, CacheEntry] = {}
        self._lock = asyncio.Lock()

    # User Permissions Cache

    async def get_user_permissions(self, user_id: str) -> Optional[UserEffectivePermissions]:
        """Get cached user permissions."""
        entry = self._user_cache.get(f"user:{user_id}")
        if entry and not entry.is_expired:
            return entry.value
        return None

    async def set_user_permissions(
        self,
        user_id: str,
        permissions: UserEffectivePermissions,
        ttl: timedelta = None
    ):
        """Cache user permissions."""
        ttl = ttl or self.DEFAULT_USER_TTL
        self._user_cache[f"user:{user_id}"] = CacheEntry(
            value=permissions,
            expires_at=datetime.utcnow() + ttl
        )

    # Role Cache

    async def get_role(self, role_id: str) -> Optional[AppRole]:
        """Get cached role."""
        entry = self._role_cache.get(f"role:{role_id}")
        if entry and not entry.is_expired:
            return entry.value
        return None

    async def set_role(self, role: AppRole, ttl: timedelta = None):
        """Cache role."""
        ttl = ttl or self.DEFAULT_ROLE_TTL
        self._role_cache[f"role:{role.role_id}"] = CacheEntry(
            value=role,
            expires_at=datetime.utcnow() + ttl
        )

    # JWT Mapping Cache

    async def get_jwt_mapping(self, jwt_role: str) -> Optional[List[str]]:
        """Get cached JWT role → AppRole IDs mapping."""
        entry = self._jwt_mapping_cache.get(f"jwt:{jwt_role}")
        if entry and not entry.is_expired:
            return entry.value
        return None

    async def set_jwt_mapping(self, jwt_role: str, role_ids: List[str], ttl: timedelta = None):
        """Cache JWT role mapping."""
        ttl = ttl or self.DEFAULT_MAPPING_TTL
        self._jwt_mapping_cache[f"jwt:{jwt_role}"] = CacheEntry(
            value=role_ids,
            expires_at=datetime.utcnow() + ttl
        )

    # Invalidation

    async def invalidate_user(self, user_id: str):
        """Invalidate cache for a specific user."""
        key = f"user:{user_id}"
        if key in self._user_cache:
            del self._user_cache[key]
            logger.debug(f"Invalidated user cache: {user_id}")

    async def invalidate_role(self, role_id: str):
        """Invalidate cache for a specific role and all affected users."""
        async with self._lock:
            # Remove role cache
            role_key = f"role:{role_id}"
            if role_key in self._role_cache:
                del self._role_cache[role_key]

            # Clear all user caches (they may be affected)
            # In production, could be more targeted based on JWT mappings
            self._user_cache.clear()

            logger.info(f"Invalidated role cache: {role_id}, cleared all user caches")

    async def invalidate_jwt_mapping(self, jwt_role: str):
        """Invalidate JWT mapping cache."""
        key = f"jwt:{jwt_role}"
        if key in self._jwt_mapping_cache:
            del self._jwt_mapping_cache[key]

        # Clear affected user caches
        self._user_cache.clear()
        logger.debug(f"Invalidated JWT mapping cache: {jwt_role}")

    async def invalidate_all(self):
        """Invalidate all caches (nuclear option)."""
        async with self._lock:
            self._user_cache.clear()
            self._role_cache.clear()
            self._jwt_mapping_cache.clear()
            logger.info("Invalidated all AppRole caches")

    def get_stats(self) -> Dict:
        """Get cache statistics for monitoring."""
        now = datetime.utcnow()
        return {
            "user_cache_size": len(self._user_cache),
            "user_cache_expired": sum(1 for e in self._user_cache.values() if e.is_expired),
            "role_cache_size": len(self._role_cache),
            "role_cache_expired": sum(1 for e in self._role_cache.values() if e.is_expired),
            "jwt_mapping_cache_size": len(self._jwt_mapping_cache),
            "jwt_mapping_cache_expired": sum(1 for e in self._jwt_mapping_cache.values() if e.is_expired),
        }
```

### 7.3 Admin UI Cache Reminder

When permission changes are made, the admin UI should display:

```
⚠️ Changes saved. Updates will take effect within 5-10 minutes as caches refresh.
```

This reminder should appear on:
- Role create/update/delete confirmation
- Tool permission changes (when updating allowed_app_roles)
- Model permission changes (when updating allowed_app_roles)

---

## 8. Authorization Flow

### 8.1 Request Authorization Sequence

```python
from typing import List, Set
from apis.shared.auth.models import User


class AppRoleService:
    """
    Service for resolving and checking AppRole-based permissions.
    """

    def __init__(self, repository: AppRoleRepository, cache: AppRoleCache):
        self.repository = repository
        self.cache = cache

    async def resolve_user_permissions(self, user: User) -> UserEffectivePermissions:
        """
        Resolve effective permissions for a user based on their JWT roles.

        This is the main entry point for authorization checks.

        Algorithm:
        1. Check user cache
        2. For each JWT role, find matching AppRoles
        3. Merge permissions (union for tools/models, highest priority for quota)
        4. Cache and return
        """
        # Step 1: Check cache
        cached = await self.cache.get_user_permissions(user.user_id)
        if cached:
            return cached

        # Step 2: Get all AppRoles that match user's JWT roles
        matching_roles: List[AppRole] = []
        jwt_roles = user.roles or []

        for jwt_role in jwt_roles:
            # Check JWT mapping cache
            role_ids = await self.cache.get_jwt_mapping(jwt_role)

            if role_ids is None:
                # Cache miss - query database
                role_ids = await self.repository.get_roles_for_jwt_role(jwt_role)
                await self.cache.set_jwt_mapping(jwt_role, role_ids)

            # Get full role objects
            for role_id in role_ids:
                role = await self._get_role_with_cache(role_id)
                if role and role.enabled:
                    matching_roles.append(role)

        # Step 3: If no roles matched, use default role
        if not matching_roles:
            default_role = await self._get_role_with_cache("default")
            if default_role and default_role.enabled:
                matching_roles = [default_role]

        # Step 4: Merge permissions
        permissions = self._merge_permissions(user.user_id, matching_roles)

        # Step 5: Cache and return
        await self.cache.set_user_permissions(user.user_id, permissions)

        return permissions

    async def _get_role_with_cache(self, role_id: str) -> Optional[AppRole]:
        """Get role from cache or database."""
        cached = await self.cache.get_role(role_id)
        if cached:
            return cached

        role = await self.repository.get_role(role_id)
        if role:
            await self.cache.set_role(role)
        return role

    def _merge_permissions(
        self,
        user_id: str,
        roles: List[AppRole]
    ) -> UserEffectivePermissions:
        """
        Merge permissions from multiple AppRoles.

        Merge rules:
        - Tools: Union (user gets access to all tools from all roles)
        - Models: Union (user gets access to all models from all roles)
        - Quota Tier: Highest priority role's tier wins
        """
        if not roles:
            return UserEffectivePermissions(
                user_id=user_id,
                app_roles=[],
                tools=[],
                models=[],
                quota_tier=None,
                resolved_at=datetime.utcnow().isoformat() + 'Z'
            )

        # Collect all tools and models (union)
        all_tools: Set[str] = set()
        all_models: Set[str] = set()

        for role in roles:
            if role.effective_permissions:
                # Handle wildcard
                if "*" in role.effective_permissions.tools:
                    all_tools.add("*")
                else:
                    all_tools.update(role.effective_permissions.tools)

                if "*" in role.effective_permissions.models:
                    all_models.add("*")
                else:
                    all_models.update(role.effective_permissions.models)

        # Determine quota tier (highest priority wins)
        sorted_roles = sorted(roles, key=lambda r: r.priority, reverse=True)
        quota_tier = None
        for role in sorted_roles:
            if role.effective_permissions and role.effective_permissions.quota_tier:
                quota_tier = role.effective_permissions.quota_tier
                break

        return UserEffectivePermissions(
            user_id=user_id,
            app_roles=[r.role_id for r in roles],
            tools=list(all_tools),
            models=list(all_models),
            quota_tier=quota_tier,
            resolved_at=datetime.utcnow().isoformat() + 'Z'
        )

    async def can_access_tool(self, user: User, tool_id: str) -> bool:
        """Check if user can access a specific tool."""
        permissions = await self.resolve_user_permissions(user)

        # Wildcard grants access to all
        if "*" in permissions.tools:
            return True

        return tool_id in permissions.tools

    async def can_access_model(self, user: User, model_id: str) -> bool:
        """Check if user can access a specific model."""
        permissions = await self.resolve_user_permissions(user)

        # Wildcard grants access to all
        if "*" in permissions.models:
            return True

        return model_id in permissions.models

    async def get_accessible_tools(self, user: User) -> List[str]:
        """Get list of tool IDs user can access."""
        permissions = await self.resolve_user_permissions(user)
        return permissions.tools

    async def get_accessible_models(self, user: User) -> List[str]:
        """Get list of model IDs user can access."""
        permissions = await self.resolve_user_permissions(user)
        return permissions.models
```

### 8.2 FastAPI Integration

```python
from fastapi import Depends, HTTPException, status
from typing import Callable


def require_tool_access(tool_id: str) -> Callable:
    """
    FastAPI dependency that checks if user can access a specific tool.

    Usage:
        @router.post("/tools/code-interpreter/execute")
        async def execute_code(
            user: User = Depends(require_tool_access("code_interpreter"))
        ):
            # User has been verified to have access
            pass
    """
    async def checker(
        user: User = Depends(get_current_user),
        app_role_service: AppRoleService = Depends(get_app_role_service)
    ) -> User:
        if not await app_role_service.can_access_tool(user, tool_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to tool: {tool_id}"
            )
        return user

    return checker


def require_model_access(model_id: str) -> Callable:
    """
    FastAPI dependency that checks if user can access a specific model.
    """
    async def checker(
        user: User = Depends(get_current_user),
        app_role_service: AppRoleService = Depends(get_app_role_service)
    ) -> User:
        if not await app_role_service.can_access_model(user, model_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to model: {model_id}"
            )
        return user

    return checker
```

---

## 9. Admin Access Control

### 9.1 Hybrid Admin Role Strategy

The system uses a **hardcoded super-admin role + configurable JWT roles** approach:

1. **`system_admin` role**: A hardcoded AppRole that:
   - Cannot be deleted or disabled
   - Has wildcard access to all tools and models
   - Has no quota limits
   - Is granted via JWT roles configured in environment variables

2. **Configuration via Environment**:

```bash
# Environment variable to specify which JWT roles grant system admin access
ADMIN_JWT_ROLES=["DotNetDevelopers", "AgentCoreAdmin"]
```

### 9.2 Implementation

```python
import os
import json
from typing import List


class SystemAdminConfig:
    """
    Configuration for system administrator access.

    System admins have full access to all RBAC features and cannot be
    locked out by misconfigured roles.
    """

    @staticmethod
    def get_admin_jwt_roles() -> List[str]:
        """
        Get JWT roles that grant system admin access.

        Configured via ADMIN_JWT_ROLES environment variable.
        Defaults to ["DotNetDevelopers"] for backwards compatibility.
        """
        roles_json = os.getenv("ADMIN_JWT_ROLES", '["DotNetDevelopers"]')
        try:
            roles = json.loads(roles_json)
            if isinstance(roles, list):
                return roles
        except json.JSONDecodeError:
            pass
        return ["DotNetDevelopers"]

    @staticmethod
    def is_system_admin(user_roles: List[str]) -> bool:
        """Check if user has system admin access via JWT roles."""
        admin_roles = SystemAdminConfig.get_admin_jwt_roles()
        return any(role in user_roles for role in admin_roles)


# Predefined system admin role (created on startup if not exists)
SYSTEM_ADMIN_ROLE = AppRole(
    role_id="system_admin",
    display_name="System Administrator",
    description="Full access to all system features. This role cannot be deleted.",
    jwt_role_mappings=[],  # Determined by ADMIN_JWT_ROLES env var at runtime
    inherits_from=[],
    granted_tools=["*"],
    granted_models=["*"],
    effective_permissions=EffectivePermissions(
        tools=["*"],
        models=["*"],
        quota_tier=None,  # No quota limits
    ),
    priority=1000,
    is_system_role=True,
    enabled=True,
)


# FastAPI dependency for admin-only endpoints
async def require_system_admin(
    user: User = Depends(get_current_user)
) -> User:
    """
    Require system administrator access.

    This uses the hardcoded admin check, NOT the AppRole system,
    to prevent lockout scenarios.
    """
    if not SystemAdminConfig.is_system_admin(user.roles or []):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System administrator access required"
        )
    return user
```

### 9.3 Admin Protection Rules

| Action | Protection |
|--------|-----------|
| View roles | `require_admin` (existing) |
| Create role | `require_system_admin` |
| Edit role | `require_system_admin` |
| Delete role | `require_system_admin` + not `is_system_role` |
| Edit `system_admin` role | Denied (display_name/description only) |
| Delete `system_admin` role | Denied |
| Edit `default` role | `require_system_admin` |
| Delete `default` role | Denied |

---

## 10. API Design

### 10.1 Admin API Endpoints

Base path: `/api/admin/roles`

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| GET | `/` | List all roles | `require_admin` |
| GET | `/{role_id}` | Get role by ID | `require_admin` |
| POST | `/` | Create new role | `require_system_admin` |
| PATCH | `/{role_id}` | Update role | `require_system_admin` |
| DELETE | `/{role_id}` | Delete role | `require_system_admin` |
| POST | `/{role_id}/sync` | Recompute effective permissions | `require_system_admin` |
| GET | `/jwt-mappings` | List all JWT role mappings | `require_admin` |
| GET | `/cache/stats` | Get cache statistics | `require_system_admin` |
| POST | `/cache/invalidate` | Force cache invalidation | `require_system_admin` |

### 10.2 Request/Response Models

```python
from pydantic import BaseModel, Field
from typing import List, Optional


# Request Models

class AppRoleCreate(BaseModel):
    """Request body for creating a new AppRole."""
    role_id: str = Field(..., pattern=r"^[a-z][a-z0-9_]{2,49}$")
    display_name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=500)
    jwt_role_mappings: List[str] = Field(default_factory=list)
    inherits_from: List[str] = Field(default_factory=list)
    granted_tools: List[str] = Field(default_factory=list)
    granted_models: List[str] = Field(default_factory=list)
    priority: int = Field(0, ge=0, le=999)
    enabled: bool = True

    # FUTURE: quota_tier: Optional[str] = None


class AppRoleUpdate(BaseModel):
    """Request body for updating an AppRole (partial update)."""
    display_name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    jwt_role_mappings: Optional[List[str]] = None
    inherits_from: Optional[List[str]] = None
    granted_tools: Optional[List[str]] = None
    granted_models: Optional[List[str]] = None
    priority: Optional[int] = Field(None, ge=0, le=999)
    enabled: Optional[bool] = None

    # FUTURE: quota_tier: Optional[str] = None

    class Config:
        # Use camelCase in JSON
        populate_by_name = True


# Response Models

class EffectivePermissionsResponse(BaseModel):
    """Computed effective permissions."""
    tools: List[str]
    models: List[str]
    quota_tier: Optional[str] = None
    # FUTURE: features: List[str] = []


class AppRoleResponse(BaseModel):
    """Response model for an AppRole."""
    role_id: str = Field(..., alias="roleId")
    display_name: str = Field(..., alias="displayName")
    description: str
    jwt_role_mappings: List[str] = Field(..., alias="jwtRoleMappings")
    inherits_from: List[str] = Field(..., alias="inheritsFrom")
    granted_tools: List[str] = Field(..., alias="grantedTools")
    granted_models: List[str] = Field(..., alias="grantedModels")
    effective_permissions: EffectivePermissionsResponse = Field(..., alias="effectivePermissions")
    priority: int
    is_system_role: bool = Field(..., alias="isSystemRole")
    enabled: bool
    created_at: str = Field(..., alias="createdAt")
    updated_at: str = Field(..., alias="updatedAt")
    created_by: Optional[str] = Field(None, alias="createdBy")

    class Config:
        populate_by_name = True


class AppRoleListResponse(BaseModel):
    """Response model for listing roles."""
    roles: List[AppRoleResponse]
    total: int


class CacheStatsResponse(BaseModel):
    """Cache statistics response."""
    user_cache_size: int = Field(..., alias="userCacheSize")
    user_cache_expired: int = Field(..., alias="userCacheExpired")
    role_cache_size: int = Field(..., alias="roleCacheSize")
    role_cache_expired: int = Field(..., alias="roleCacheExpired")
    jwt_mapping_cache_size: int = Field(..., alias="jwtMappingCacheSize")
    jwt_mapping_cache_expired: int = Field(..., alias="jwtMappingCacheExpired")

    class Config:
        populate_by_name = True
```

### 10.3 Route Implementation

```python
from fastapi import APIRouter, Depends, HTTPException, status
from typing import List

router = APIRouter(prefix="/admin/roles", tags=["Admin - Roles"])


@router.get("/", response_model=AppRoleListResponse)
async def list_roles(
    enabled_only: bool = False,
    admin: User = Depends(require_admin),
    service: AppRoleAdminService = Depends(get_app_role_admin_service)
):
    """List all application roles."""
    roles = await service.list_roles(enabled_only=enabled_only)
    return AppRoleListResponse(roles=roles, total=len(roles))


@router.get("/{role_id}", response_model=AppRoleResponse)
async def get_role(
    role_id: str,
    admin: User = Depends(require_admin),
    service: AppRoleAdminService = Depends(get_app_role_admin_service)
):
    """Get a role by ID."""
    role = await service.get_role(role_id)
    if not role:
        raise HTTPException(status_code=404, detail=f"Role '{role_id}' not found")
    return role


@router.post("/", response_model=AppRoleResponse, status_code=201)
async def create_role(
    role_data: AppRoleCreate,
    admin: User = Depends(require_system_admin),
    service: AppRoleAdminService = Depends(get_app_role_admin_service)
):
    """
    Create a new application role.

    Requires system administrator access.
    """
    try:
        role = await service.create_role(role_data, admin)
        return role
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{role_id}", response_model=AppRoleResponse)
async def update_role(
    role_id: str,
    updates: AppRoleUpdate,
    admin: User = Depends(require_system_admin),
    service: AppRoleAdminService = Depends(get_app_role_admin_service)
):
    """
    Update an application role.

    Requires system administrator access.
    System roles have limited editability.
    """
    try:
        role = await service.update_role(role_id, updates, admin)
        if not role:
            raise HTTPException(status_code=404, detail=f"Role '{role_id}' not found")
        return role
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{role_id}", status_code=204)
async def delete_role(
    role_id: str,
    admin: User = Depends(require_system_admin),
    service: AppRoleAdminService = Depends(get_app_role_admin_service)
):
    """
    Delete an application role.

    Requires system administrator access.
    System roles cannot be deleted.
    """
    try:
        success = await service.delete_role(role_id, admin)
        if not success:
            raise HTTPException(status_code=404, detail=f"Role '{role_id}' not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{role_id}/sync", response_model=AppRoleResponse)
async def sync_role_permissions(
    role_id: str,
    admin: User = Depends(require_system_admin),
    service: AppRoleAdminService = Depends(get_app_role_admin_service)
):
    """
    Force recomputation of effective permissions for a role.

    Useful after inheritance changes or to fix data inconsistencies.
    """
    role = await service.sync_effective_permissions(role_id, admin)
    if not role:
        raise HTTPException(status_code=404, detail=f"Role '{role_id}' not found")
    return role


@router.get("/cache/stats", response_model=CacheStatsResponse)
async def get_cache_stats(
    admin: User = Depends(require_system_admin),
    cache: AppRoleCache = Depends(get_app_role_cache)
):
    """Get cache statistics."""
    return cache.get_stats()


@router.post("/cache/invalidate", status_code=204)
async def invalidate_cache(
    admin: User = Depends(require_system_admin),
    cache: AppRoleCache = Depends(get_app_role_cache)
):
    """Force invalidation of all role caches."""
    await cache.invalidate_all()
```

---

## 11. Frontend Admin UI

### 11.1 Navigation Structure

Add new admin section:

```
/admin
├── /roles                    # Role Management (new)
│   ├── /                     # Role list
│   ├── /new                  # Create role
│   └── /edit/:roleId         # Edit role
├── /manage-models            # Existing - add AppRole multi-select
├── /quota                    # Existing
└── ...
```

### 11.2 Role List Page

**Route**: `/admin/roles`

**Features**:
- Table listing all roles with columns:
  - Display Name
  - Role ID
  - JWT Mappings (pill badges)
  - Status (enabled/disabled)
  - Priority
  - System Role (badge)
  - Actions (Edit, Delete)
- Filter by enabled/disabled
- Search by name/ID
- Sort by priority, name, created date
- "Create Role" button

**Wireframe**:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Application Roles                                        [+ Create Role]    │
├─────────────────────────────────────────────────────────────────────────────┤
│ ┌─────────────────────────────────────┐  ┌──────────────┐                  │
│ │ 🔍 Search roles...                   │  │ All Statuses ▼│                  │
│ └─────────────────────────────────────┘  └──────────────┘                  │
├─────────────────────────────────────────────────────────────────────────────┤
│ Display Name      │ Role ID      │ JWT Mappings          │ Pri │ Status    │
├───────────────────┼──────────────┼───────────────────────┼─────┼───────────┤
│ System Admin      │ system_admin │ DotNetDevelopers      │ 1000│ ✓ Enabled │
│ 🔒 System                                                       │ [Edit]    │
├───────────────────┼──────────────┼───────────────────────┼─────┼───────────┤
│ Power User        │ power_user   │ Faculty Researcher    │ 100 │ ✓ Enabled │
│                   │              │ GraduateStudent       │     │ [Edit][🗑]│
├───────────────────┼──────────────┼───────────────────────┼─────┼───────────┤
│ Basic User        │ basic_user   │ Staff All-Employees   │ 50  │ ✓ Enabled │
│                   │              │                       │     │ [Edit][🗑]│
├───────────────────┼──────────────┼───────────────────────┼─────┼───────────┤
│ Default User      │ default      │ (fallback)            │ 0   │ ✓ Enabled │
│ 🔒 System                                                       │ [Edit]    │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 11.3 Role Create/Edit Form

**Route**: `/admin/roles/new` or `/admin/roles/edit/:roleId`

**Form Sections**:

1. **Basic Information**
   - Role ID (create only, read-only on edit)
   - Display Name
   - Description
   - Priority (0-999)
   - Enabled toggle

2. **JWT Role Mappings**
   - Multi-select chips for known JWT roles
   - Option to add custom JWT role names
   - Helper text: "Users with these identity provider roles will be granted this app role"

3. **Inheritance**
   - Multi-select dropdown of other AppRoles
   - Shows inherited permissions preview
   - Helper text: "This role will inherit all permissions from selected roles"

4. **Tool Permissions**
   - Multi-select list of available tools
   - Grouped by category (Built-in, Local, MCP)
   - Shows both directly granted and inherited tools

5. **Model Permissions**
   - Multi-select list of available models
   - Grouped by provider (Bedrock, OpenAI, Gemini)
   - Shows both directly granted and inherited models

6. **Computed Permissions Preview** (read-only)
   - Shows final effective_permissions
   - Updated in real-time as form changes

**Wireframe**:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ ← Back to Roles                                                              │
│                                                                              │
│ Create Application Role                                                      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│ BASIC INFORMATION                                                            │
│ ┌─────────────────────────────────────────────────────────────────────────┐ │
│ │ Role ID *                                                               │ │
│ │ ┌─────────────────────────────────────┐                                 │ │
│ │ │ power_user                          │                                 │ │
│ │ └─────────────────────────────────────┘                                 │ │
│ │ Lowercase letters, numbers, and underscores only                        │ │
│ │                                                                         │ │
│ │ Display Name *                                                          │ │
│ │ ┌─────────────────────────────────────┐                                 │ │
│ │ │ Power User                          │                                 │ │
│ │ └─────────────────────────────────────┘                                 │ │
│ │                                                                         │ │
│ │ Description                                                             │ │
│ │ ┌─────────────────────────────────────────────────────────────────────┐ │ │
│ │ │ Advanced users with access to code execution and research tools     │ │ │
│ │ └─────────────────────────────────────────────────────────────────────┘ │ │
│ │                                                                         │ │
│ │ Priority                    Enabled                                     │ │
│ │ ┌──────────┐               [✓]                                         │ │
│ │ │ 100      │                                                            │ │
│ │ └──────────┘                                                            │ │
│ │ Higher priority role's quota tier wins when user has multiple roles    │ │
│ └─────────────────────────────────────────────────────────────────────────┘ │
│                                                                              │
│ JWT ROLE MAPPINGS                                                            │
│ ┌─────────────────────────────────────────────────────────────────────────┐ │
│ │ Users with these identity provider roles will be granted this app role │ │
│ │                                                                         │ │
│ │ ┌─────────────────────────────────────────────────────────────────────┐ │ │
│ │ │ [Faculty ×] [Researcher ×] [GraduateStudent ×] [+ Add role]         │ │ │
│ │ └─────────────────────────────────────────────────────────────────────┘ │ │
│ │                                                                         │ │
│ │ Available JWT roles:                                                    │ │
│ │ [ ] Admin            [ ] Staff              [✓] Faculty                │ │
│ │ [✓] Researcher       [ ] PSSTUCURTERM       [✓] GraduateStudent        │ │
│ │ [ ] DotNetDevelopers [ ] All-Employees      [ ] AWS-BoiseStateAI       │ │
│ └─────────────────────────────────────────────────────────────────────────┘ │
│                                                                              │
│ INHERITS FROM                                                                │
│ ┌─────────────────────────────────────────────────────────────────────────┐ │
│ │ This role will inherit all permissions from selected roles              │ │
│ │                                                                         │ │
│ │ ┌─────────────────────────────────────────┐                             │ │
│ │ │ Select roles to inherit from...       ▼│                             │ │
│ │ └─────────────────────────────────────────┘                             │ │
│ │                                                                         │ │
│ │ Selected: [basic_user ×]                                                │ │
│ │                                                                         │ │
│ │ Inherited permissions preview:                                          │ │
│ │ • Tools: calculator, web_search                                         │ │
│ │ • Models: claude-sonnet                                                 │ │
│ └─────────────────────────────────────────────────────────────────────────┘ │
│                                                                              │
│ TOOL PERMISSIONS                                                             │
│ ┌─────────────────────────────────────────────────────────────────────────┐ │
│ │ Grant access to tools (in addition to inherited)                        │ │
│ │                                                                         │ │
│ │ Built-in Tools                                                          │ │
│ │ [✓] code_interpreter    [✓] browser_navigate    [ ] browser_screenshot │ │
│ │                                                                         │ │
│ │ Local Tools                                                             │ │
│ │ [✓] deep_research       [ ] weather             [ ] visualization     │ │
│ │                                                                         │ │
│ │ MCP Tools                                                               │ │
│ │ [ ] wikipedia           [ ] arxiv               [✓] financial_analysis │ │
│ └─────────────────────────────────────────────────────────────────────────┘ │
│                                                                              │
│ MODEL PERMISSIONS                                                            │
│ ┌─────────────────────────────────────────────────────────────────────────┐ │
│ │ Grant access to models (in addition to inherited)                       │ │
│ │                                                                         │ │
│ │ AWS Bedrock                                                             │ │
│ │ [ ] claude-sonnet       [✓] claude-opus         [ ] nova-pro           │ │
│ │                                                                         │ │
│ │ OpenAI                                                                  │ │
│ │ [✓] gpt-4o              [ ] gpt-4o-mini         [ ] o1                  │ │
│ │                                                                         │ │
│ │ Google                                                                  │ │
│ │ [ ] gemini-pro          [ ] gemini-flash                                │ │
│ └─────────────────────────────────────────────────────────────────────────┘ │
│                                                                              │
│ ┌─────────────────────────────────────────────────────────────────────────┐ │
│ │ EFFECTIVE PERMISSIONS (computed)                                        │ │
│ │                                                                         │ │
│ │ Tools: calculator, web_search, code_interpreter, browser_navigate,     │ │
│ │        deep_research, financial_analysis                                │ │
│ │                                                                         │ │
│ │ Models: claude-sonnet, claude-opus, gpt-4o                              │ │
│ │                                                                         │ │
│ │ Quota Tier: (from inherited basic_user or configure separately)         │ │
│ └─────────────────────────────────────────────────────────────────────────┘ │
│                                                                              │
│ ⚠️ Changes will take effect within 5-10 minutes as caches refresh.          │
│                                                                              │
│                                              [Cancel]  [Save Role]           │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 11.4 Model Admin Integration

Update the existing model create/edit form to include AppRole multi-select:

**Location**: `/admin/manage-models/new` and `/admin/manage-models/edit/:id`

**Addition**:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ ACCESS CONTROL                                                               │
│ ┌─────────────────────────────────────────────────────────────────────────┐ │
│ │ Select which application roles can access this model                    │ │
│ │                                                                         │ │
│ │ ┌─────────────────────────────────────────┐                             │ │
│ │ │ Select roles...                        ▼│                             │ │
│ │ └─────────────────────────────────────────┘                             │ │
│ │                                                                         │ │
│ │ Selected: [power_user ×] [researcher ×] [basic_user ×]                  │ │
│ │                                                                         │ │
│ │ ⚠️ Changes will take effect within 5-10 minutes as caches refresh.      │ │
│ └─────────────────────────────────────────────────────────────────────────┘ │
```

### 11.5 Angular Component Example

```typescript
import {
  Component,
  ChangeDetectionStrategy,
  input,
  output,
  computed,
  inject,
  signal
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { ReactiveFormsModule, FormBuilder, Validators } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroShieldCheck, heroUsers, heroCog } from '@ng-icons/heroicons/outline';
import { AppRoleService } from './services/app-role.service';
import { AppRole, AppRoleCreate } from './models/app-role.model';

@Component({
  selector: 'app-role-form',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, ReactiveFormsModule, NgIcon],
  providers: [provideIcons({ heroShieldCheck, heroUsers, heroCog })],
  template: `
    <form [formGroup]="form" (ngSubmit)="onSubmit()">
      <!-- Basic Information -->
      <section class="mb-6">
        <h3 class="text-lg font-medium mb-4">Basic Information</h3>

        <div class="space-y-4">
          <div>
            <label for="roleId" class="block text-sm font-medium mb-1">
              Role ID
            </label>
            <input
              id="roleId"
              type="text"
              formControlName="roleId"
              [readonly]="isEdit()"
              class="w-full px-3 py-2 border rounded-sm focus:outline-hidden focus:ring-3"
              [class.bg-gray-100]="isEdit()"
            />
            @if (!isEdit()) {
              <p class="text-sm text-gray-500 mt-1">
                Lowercase letters, numbers, and underscores only
              </p>
            }
          </div>

          <div>
            <label for="displayName" class="block text-sm font-medium mb-1">
              Display Name
            </label>
            <input
              id="displayName"
              type="text"
              formControlName="displayName"
              class="w-full px-3 py-2 border rounded-sm focus:outline-hidden focus:ring-3"
            />
          </div>

          <div class="flex gap-4">
            <div class="flex-1">
              <label for="priority" class="block text-sm font-medium mb-1">
                Priority
              </label>
              <input
                id="priority"
                type="number"
                formControlName="priority"
                min="0"
                max="999"
                class="w-full px-3 py-2 border rounded-sm"
              />
            </div>

            <div class="flex items-center gap-2 pt-6">
              <input
                id="enabled"
                type="checkbox"
                formControlName="enabled"
                class="size-4"
              />
              <label for="enabled" class="text-sm font-medium">Enabled</label>
            </div>
          </div>
        </div>
      </section>

      <!-- JWT Role Mappings -->
      <section class="mb-6">
        <h3 class="text-lg font-medium mb-4">
          <ng-icon name="heroUsers" class="size-5 inline mr-2" />
          JWT Role Mappings
        </h3>

        <p class="text-sm text-gray-600 mb-3">
          Users with these identity provider roles will be granted this app role
        </p>

        <div class="flex flex-wrap gap-2 mb-3">
          @for (role of selectedJwtRoles(); track role) {
            <span class="inline-flex items-center gap-1 px-3 py-1 bg-blue-100 text-blue-800 rounded-full text-sm">
              {{ role }}
              <button
                type="button"
                (click)="removeJwtRole(role)"
                class="hover:text-blue-600"
              >
                &times;
              </button>
            </span>
          }
        </div>

        <div class="grid grid-cols-3 gap-2">
          @for (role of availableJwtRoles(); track role) {
            <label class="flex items-center gap-2 p-2 border rounded-sm hover:bg-gray-50">
              <input
                type="checkbox"
                [checked]="selectedJwtRoles().includes(role)"
                (change)="toggleJwtRole(role)"
              />
              <span class="text-sm">{{ role }}</span>
            </label>
          }
        </div>
      </section>

      <!-- Effective Permissions Preview -->
      <section class="mb-6 p-4 bg-gray-50 rounded-sm">
        <h3 class="text-lg font-medium mb-4">
          <ng-icon name="heroShieldCheck" class="size-5 inline mr-2" />
          Effective Permissions (computed)
        </h3>

        <div class="space-y-2 text-sm">
          <p>
            <strong>Tools:</strong>
            {{ effectivePermissions()?.tools?.join(', ') || 'None' }}
          </p>
          <p>
            <strong>Models:</strong>
            {{ effectivePermissions()?.models?.join(', ') || 'None' }}
          </p>
        </div>
      </section>

      <!-- Cache Warning -->
      <div class="mb-6 p-3 bg-amber-50 border border-amber-200 rounded-sm">
        <p class="text-sm text-amber-800">
          Changes will take effect within 5-10 minutes as caches refresh.
        </p>
      </div>

      <!-- Actions -->
      <div class="flex justify-end gap-3">
        <button
          type="button"
          (click)="cancelled.emit()"
          class="px-4 py-2 border rounded-sm hover:bg-gray-50"
        >
          Cancel
        </button>
        <button
          type="submit"
          [disabled]="!form.valid || saving()"
          class="px-4 py-2 bg-blue-600 text-white rounded-sm hover:bg-blue-700 disabled:opacity-50"
        >
          {{ saving() ? 'Saving...' : 'Save Role' }}
        </button>
      </div>
    </form>
  `
})
export class RoleFormComponent {
  // Inputs
  role = input<AppRole | null>(null);
  isEdit = computed(() => !!this.role());

  // Outputs
  saved = output<AppRoleCreate>();
  cancelled = output<void>();

  // State
  saving = signal(false);
  selectedJwtRoles = signal<string[]>([]);

  // Services
  private fb = inject(FormBuilder);
  private roleService = inject(AppRoleService);

  // Form
  form = this.fb.group({
    roleId: ['', [Validators.required, Validators.pattern(/^[a-z][a-z0-9_]{2,49}$/)]],
    displayName: ['', [Validators.required, Validators.maxLength(100)]],
    description: ['', Validators.maxLength(500)],
    priority: [0, [Validators.min(0), Validators.max(999)]],
    enabled: [true],
  });

  // Available JWT roles (would come from service in real implementation)
  availableJwtRoles = signal([
    'Admin', 'Faculty', 'Staff', 'Researcher',
    'PSSTUCURTERM', 'GraduateStudent', 'DotNetDevelopers',
    'All-Employees Entra Sync', 'AWS-BoiseStateAI'
  ]);

  // Computed effective permissions
  effectivePermissions = computed(() => {
    // In real implementation, this would call the service to compute
    return this.role()?.effectivePermissions;
  });

  toggleJwtRole(role: string): void {
    this.selectedJwtRoles.update(roles => {
      if (roles.includes(role)) {
        return roles.filter(r => r !== role);
      }
      return [...roles, role];
    });
  }

  removeJwtRole(role: string): void {
    this.selectedJwtRoles.update(roles => roles.filter(r => r !== role));
  }

  onSubmit(): void {
    if (this.form.valid) {
      const formValue = this.form.getRawValue();
      this.saved.emit({
        ...formValue,
        jwtRoleMappings: this.selectedJwtRoles(),
        inheritsFrom: [],
        grantedTools: [],
        grantedModels: [],
      } as AppRoleCreate);
    }
  }
}
```

---

## 12. Future Integrations

This section documents how the AppRole system will integrate with other systems in future phases.

### 12.1 Tool RBAC Integration

<!-- FUTURE: Tool RBAC Integration
When implementing Tool RBAC:

1. Add `allowedAppRoles: List[str]` field to Tool model
2. Update tool registry to store role requirements
3. Modify tool filtering in ChatbotAgent to use AppRoleService.can_access_tool()
4. Add AppRole multi-select to tool creation UI (if/when admin tool management is added)
5. Implement bidirectional sync:
   - When tool's allowedAppRoles changes → update affected AppRoles' granted_tools
   - When AppRole's granted_tools changes → update affected tools' allowedAppRoles

Key files to modify:
- backend/src/agentcore/local_tools/*.py - Add role requirements
- backend/src/agentcore/agent/agent.py:277-318 - Update tool filtering
- backend/src/apis/app_api/admin/tools/ - New admin endpoints (if needed)
-->

**Planned Integration Points**:

1. **Tool Definition Enhancement**:
   ```python
   @dataclass
   class ToolDefinition:
       tool_id: str
       name: str
       description: str
       allowed_app_roles: List[str] = field(default_factory=list)  # New field
   ```

2. **Agent Tool Filtering** (`agent.py:277-318`):
   ```python
   async def _filter_tools_for_user(self, user: User, tools: List[Tool]) -> List[Tool]:
       """Filter tools based on user's AppRole permissions."""
       permissions = await self.app_role_service.resolve_user_permissions(user)

       if "*" in permissions.tools:
           return tools  # Wildcard = all tools

       return [t for t in tools if t.tool_id in permissions.tools]
   ```

### 12.2 Model RBAC Integration

<!-- FUTURE: Model RBAC Integration
The ManagedModels table already has `available_to_roles` field.

To integrate with AppRole:
1. Rename `available_to_roles` to `allowedAppRoles` (or add new field)
2. Update model service to check via AppRoleService.can_access_model()
3. Update model admin UI to use AppRole multi-select instead of JWT role multi-select
4. Implement bidirectional sync similar to tools

Key files to modify:
- backend/src/apis/app_api/admin/routes.py - ManagedModel endpoints
- backend/src/apis/app_api/admin/models.py - ManagedModel schema
- frontend/ai.client/src/app/admin/manage-models/ - UI components
-->

**Current State**: `ManagedModels` already has `available_to_roles: List[str]` which stores JWT role names.

**Planned Migration**:
1. Add `allowed_app_roles: List[str]` field to ManagedModel
2. Deprecate `available_to_roles` (keep for backwards compat during transition)
3. Update model access check to use AppRoleService

### 12.3 Quota Integration

<!-- FUTURE: Quota Integration
The quota system already has comprehensive assignment types.

To add AppRole as an assignment type:
1. Add "app_role" to AssignmentType enum
2. Update QuotaAssignment model with app_role_id field
3. Update quota resolver priority (between direct_user and jwt_role):
   1. Direct user (highest)
   2. AppRole ← New
   3. JWT role
   4. Email pattern
   5. Email domain
   6. Default tier (lowest)
4. Add AppRole to assignment creation UI

Key files to modify:
- backend/src/agents/strands_agent/quota/models.py - AssignmentType enum
- backend/src/agents/strands_agent/quota/resolver.py - Resolution logic
- frontend/ai.client/src/app/admin/quota/assignments/ - UI
-->

**Planned Priority Order**:
1. Direct user assignment (highest)
2. **AppRole** ← New
3. JWT role
4. Email pattern
5. Email domain
6. Default tier (lowest)

---

## 13. Implementation Phases

### Phase 1: Core AppRole System (This Spec)

**Duration**: ~2-3 weeks

**Deliverables**:
1. DynamoDB table creation in CDK
2. AppRole data models and repository
3. Caching layer implementation
4. AppRoleService with permission resolution
5. Admin API endpoints
6. Frontend role management UI
7. System admin protection
8. Default role fallback

**Out of Scope**:
- Tool RBAC integration
- Model RBAC integration (keep existing `available_to_roles`)
- Quota integration

### Phase 2: Model Integration

**Dependencies**: Phase 1 complete

**Deliverables**:
1. Add `allowed_app_roles` to ManagedModel
2. Update model service to use AppRoleService
3. Update model admin UI with AppRole multi-select
4. Bidirectional sync for model-role relationships
5. Deprecation path for `available_to_roles`

### Phase 3: Tool RBAC Integration

**Dependencies**: Phase 1 complete

**Deliverables**:
1. Tool definition enhancement with `allowed_app_roles`
2. Agent tool filtering via AppRoleService
3. Tool admin UI (if applicable)
4. Bidirectional sync for tool-role relationships

### Phase 4: Quota Integration

**Dependencies**: Phase 1 complete

**Deliverables**:
1. Add "app_role" assignment type
2. Update quota resolver with AppRole priority
3. Update quota assignment UI
4. Migration of existing JWT role assignments (optional)

---

## 14. Security Considerations

### 14.1 Access Control

| Risk | Mitigation |
|------|-----------|
| Privilege escalation via role creation | Only system admins can create/edit roles |
| Lockout via role misconfiguration | System admin role cannot be disabled/deleted |
| JWT role spoofing | JWT validation happens before role resolution |
| Cache poisoning | Cache is in-memory, not externally accessible |

### 14.2 Audit Logging

All role modifications should be logged:

```python
logger.info(
    f"AppRole modified",
    extra={
        "event": "app_role_modified",
        "action": "create|update|delete",
        "role_id": role_id,
        "admin_user_id": admin.user_id,
        "admin_email": admin.email,
        "changes": changes_dict,
        "timestamp": datetime.utcnow().isoformat()
    }
)
```

### 14.3 Input Validation

- Role IDs: Alphanumeric + underscore, 3-50 chars, lowercase
- JWT role names: Non-empty strings, max 100 chars
- Tool/Model IDs: Validated against existing entities
- Priority: Integer 0-999

---

## 15. Configuration

### 15.1 Environment Variables

```bash
# Required for DynamoDB table
DYNAMODB_APP_ROLES_TABLE_NAME=bsu-agentcore-app-roles

# Admin access configuration
ADMIN_JWT_ROLES=["DotNetDevelopers", "AgentCoreAdmin"]

# Cache configuration (optional, defaults shown)
APP_ROLE_USER_CACHE_TTL_MINUTES=5
APP_ROLE_ROLE_CACHE_TTL_MINUTES=10
APP_ROLE_MAPPING_CACHE_TTL_MINUTES=10
```

### 15.2 CDK Configuration

Add to `infrastructure/lib/app-api-stack.ts`:

```typescript
// Add to container environment
environment: {
  // ... existing env vars ...
  DYNAMODB_APP_ROLES_TABLE_NAME: appRolesTable.tableName,
  ADMIN_JWT_ROLES: JSON.stringify(config.appApi.adminJwtRoles || ["DotNetDevelopers"]),
}
```

### 15.3 README Documentation

Add to project README:

```markdown
## Admin Access Configuration

System administrator access is controlled via the `ADMIN_JWT_ROLES` environment variable.
Users with any of the specified JWT roles from your identity provider will have full
access to the RBAC admin features.

### Default Configuration

```bash
ADMIN_JWT_ROLES=["DotNetDevelopers"]
```

### Adding Additional Admin Roles

To grant admin access to additional JWT roles:

```bash
ADMIN_JWT_ROLES=["DotNetDevelopers", "AgentCoreAdmin", "ITSecurityTeam"]
```

### Important Notes

1. The `system_admin` AppRole is protected and cannot be deleted
2. Admin access is determined by JWT roles, not by the AppRole system itself (prevents lockout)
3. Changes to `ADMIN_JWT_ROLES` require application restart
```

---

## Appendix A: Migration Checklist

When implementing this spec, use this checklist:

- [ ] Create DynamoDB table in `app-api-stack.ts`
- [ ] Add table name to container environment variables
- [ ] Create `backend/src/apis/shared/rbac/` directory structure
- [ ] Implement `AppRole` data models
- [ ] Implement `AppRoleRepository`
- [ ] Implement `AppRoleCache`
- [ ] Implement `AppRoleService`
- [ ] Implement `AppRoleAdminService`
- [ ] Add admin routes to FastAPI
- [ ] Create `require_system_admin` dependency
- [ ] Seed `system_admin` and `default` roles on startup
- [ ] Create Angular `app-role.service.ts`
- [ ] Create Angular role list component
- [ ] Create Angular role form component
- [ ] Add routes to `app.routes.ts`
- [ ] Add admin dashboard card for roles
- [ ] Update README with admin configuration
- [ ] Write unit tests for repository
- [ ] Write unit tests for service
- [ ] Write integration tests for API

---

## Appendix B: Glossary

| Term | Definition |
|------|-----------|
| **AppRole** | Application-level role that maps JWT roles to permissions |
| **JWT Role** | Role claim from identity provider (Entra ID) token |
| **Effective Permissions** | Pre-computed, denormalized permissions for fast authorization |
| **System Role** | AppRole that cannot be deleted (e.g., `system_admin`, `default`) |
| **Bidirectional Sync** | Keeping role→resource and resource→role mappings in sync |
| **Cache TTL** | Time-to-live for cached data before automatic refresh |

---

*End of Specification*
