# Architecture Debt & Technical Issues

This document tracks architectural issues, technical debt, and areas requiring refactoring.

## Cross-Service Dependencies

### Issue: Inference API depends on App API modules

**Status**: 🔴 Active Issue  
**Severity**: High  
**Date Identified**: 2025-01-28

#### Problem

The Inference API (AgentCore Runtime) has direct Python imports from the App API codebase:

```python
# In backend/src/apis/inference_api/chat/service.py
from apis.app_api.sessions.models import SessionMetadata
from apis.app_api.sessions.services.metadata import store_session_metadata

# In backend/src/apis/inference_api/chat/routes.py
from apis.app_api.admin.services.managed_models import list_managed_models
from apis.app_api.files.file_resolver import get_file_resolver
from apis.app_api.assistants.services.assistant_service import get_assistant_with_access_check
from apis.app_api.assistants.services.rag_service import search_assistant_knowledgebase_with_formatting
from apis.app_api.sessions.services.metadata import get_session_metadata, store_session_metadata
from apis.app_api.sessions.models import SessionMetadata, SessionPreferences
from apis.app_api.sessions.services.messages import get_messages
```

#### Impact

1. **Tight Coupling**: Inference API cannot be deployed independently of App API code
2. **Log Namespace Pollution**: App API logs (`apis.app_api.*`) appear in AgentCore Runtime CloudWatch logs
3. **Deployment Complexity**: Docker image for inference API must include app_api code
4. **Maintenance Burden**: Changes to app_api modules can break inference API
5. **Testing Difficulty**: Cannot test inference API in isolation

#### Root Cause

The services were initially developed as a monolith and later split into separate deployment targets (ECS for App API, AgentCore Runtime for Inference API) without properly separating the codebases.

#### Affected Modules

| App API Module | Used By Inference API For |
|----------------|---------------------------|
| `apis.app_api.sessions.models` | Session metadata models |
| `apis.app_api.sessions.services.metadata` | Storing/retrieving session metadata |
| `apis.app_api.sessions.services.messages` | Retrieving conversation history |
| `apis.app_api.admin.services.managed_models` | Looking up model capabilities (e.g., `supports_caching`) |
| `apis.app_api.files.file_resolver` | Resolving file references in chat |
| `apis.app_api.assistants.services.assistant_service` | Assistant access control |
| `apis.app_api.assistants.services.rag_service` | RAG knowledge base search |

#### Recommended Solutions

**Option 1: Move Shared Code to `apis.shared` (Preferred)**
- Move session models and services to `apis.shared.sessions`
- Move file resolver to `apis.shared.files`
- Both services import from shared module
- Pros: Clean separation, single source of truth
- Cons: Requires refactoring both services

**Option 2: Service-to-Service API Calls**
- Inference API calls App API via HTTP for managed models, file resolution, etc.
- Pros: True service independence
- Cons: Network latency, requires authentication between services

**Option 3: Pass Data Through Request Payload**
- Client includes necessary metadata in inference API requests
- Pros: No cross-service dependencies
- Cons: Larger payloads, client complexity

**Option 4: Duplicate Code**
- Copy necessary modules to inference API
- Pros: Quick fix, complete independence
- Cons: Code duplication, maintenance nightmare

#### Action Items

- [ ] Decide on refactoring approach (recommend Option 1)
- [ ] Create `apis.shared.sessions` module
- [ ] Create `apis.shared.files` module
- [ ] Refactor inference API to use shared modules
- [ ] Refactor app API to use shared modules
- [ ] Update Docker builds to ensure shared modules are included
- [ ] Add integration tests to verify separation
- [ ] Document new architecture in README

#### Workarounds

**Current State**: Both services share the same codebase in Docker images, so the cross-imports work but create the issues listed above.

**Temporary Mitigation**: None - this requires architectural refactoring to properly resolve.

---

## Other Known Issues

### Issue: DynamoDB GSI Permissions Not Granted by `grantReadWriteData()`

**Status**: ✅ Fixed  
**Date Fixed**: 2025-01-28

#### Problem
CDK's `table.grantReadWriteData()` method doesn't automatically grant permissions to query Global Secondary Indexes (GSIs). This caused `AccessDeniedException` errors when querying the `OwnerStatusIndex` GSI on the assistants table.

#### Solution
Added explicit IAM policy statements to grant `dynamodb:Query` and `dynamodb:Scan` permissions on `table-arn/index/*` pattern.

```typescript
taskDefinition.taskRole.addToPrincipalPolicy(
  new iam.PolicyStatement({
    effect: iam.Effect.ALLOW,
    actions: ['dynamodb:Query', 'dynamodb:Scan'],
    resources: [`${assistantsTable.tableArn}/index/*`],
  })
);
```

---

### Issue: Silent Failures in Assistant Service

**Status**: ✅ Fixed  
**Date Fixed**: 2025-01-28

#### Problem
The `_list_user_assistants_cloud()` function caught all exceptions and returned empty arrays `([], None)` instead of propagating errors. This caused API endpoints to return `200 OK` with empty results even when DynamoDB operations failed.

#### Solution
Modified exception handlers to re-raise exceptions so they propagate to the endpoint, which returns appropriate HTTP error codes (500).

```python
except ClientError as e:
    error_code = e.response.get('Error', {}).get('Code', 'Unknown')
    error_message = e.response.get('Error', {}).get('Message', str(e))
    logger.error(f"Failed to list user assistants from DynamoDB: {error_code} - {error_message}")
    raise Exception(f"DynamoDB error ({error_code}): {error_message}") from e
```

---

## Contributing

When you identify new architectural issues or technical debt:

1. Add a new section to this document
2. Include: Status, Severity, Date, Problem, Impact, Root Cause, Solutions, Action Items
3. Update status as work progresses
4. Move to "Fixed" section when resolved
