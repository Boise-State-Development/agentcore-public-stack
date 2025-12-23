# Memory Dashboard Implementation Summary

This document summarizes the implementation of the Memory Dashboard feature, which allows users to view and manage memories that the AI has learned about them.

## Overview

The Memory Dashboard provides a user-friendly interface for viewing, searching, and deleting memories stored in AWS Bedrock AgentCore Memory. Memories are categorized into two types:

- **Preferences**: Behavioral patterns learned from conversations (e.g., "User prefers concise responses")
- **Facts**: Information learned about the user (e.g., "User is a software engineer")

## Architecture

### Frontend Components

**Location**: `frontend/ai.client/src/app/memory/`

| File | Purpose |
|------|---------|
| `memory-dashboard.page.ts` | Main dashboard component with template, pipe, and logic |
| `services/memory.service.ts` | Angular service for API communication |
| `models/memory.model.ts` | TypeScript interfaces for memory data |

### Backend Components

**Location**: `backend/src/apis/app_api/memory/`

| File | Purpose |
|------|---------|
| `routes.py` | FastAPI endpoints for memory operations |
| `models.py` | Pydantic models for request/response validation |
| `services/memory_service.py` | Business logic for AgentCore Memory operations |

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/memory/status` | Check if AgentCore Memory is available |
| `GET` | `/memory` | Get all memories (preferences + facts) |
| `GET` | `/memory/preferences` | Get user preferences |
| `GET` | `/memory/facts` | Get user facts |
| `POST` | `/memory/search` | Semantic search across memories |
| `GET` | `/memory/strategies` | Get configured memory strategies |
| `DELETE` | `/memory/{record_id}` | Delete a specific memory |

## Features

### 1. Memory Display

- **Parsed Preferences**: JSON content is parsed to extract main text and categories
- **Relative Timestamps**: Displayed as "Learned X ago" format
- **Category Badges**: Color-coded badges with consistent hash-based colors
- **Relevance Scores**: Shown for search results

### 2. Tab Navigation

- **All Memories**: Shows both preferences and facts in sections
- **Preferences Only**: Filtered view with sparkles icon
- **Facts Only**: Filtered view with lightbulb icon

### 3. Search

- Semantic search using AgentCore Memory's vector search
- Results appear at the top with relevance scores
- "Clear search" button to return to normal view

### 4. Delete Functionality

- Hover to reveal delete button (trash icon)
- Loading spinner during deletion
- Automatic refresh after successful deletion
- Uses boto3 `batch_delete_memory_records` API directly

## Technical Details

### Frontend Patterns

```typescript
// Signal-based state management
readonly activeTab = signal<'all' | 'preferences' | 'facts'>('all');
readonly deletingMemoryId = signal<string | null>(null);

// Computed values for derived state
readonly preferences = computed(() => {
  const data = this.allMemories.value();
  return data?.preferences?.memories ?? [];
});

// Resource-based data fetching
readonly allMemories = resource({
  loader: async () => {
    await this.authService.ensureAuthenticated();
    return this.fetchAllMemories();
  }
});
```

### Parse Preference Pipe

The `ParsePreferencePipe` extracts structured content from JSON preferences:

```typescript
interface ParsedPreference {
  mainText: string;      // Primary preference text
  categories?: string[]; // Optional category tags
}
```

It looks for main text in keys: `preference`, `value`, `text`, `content`, `description`, `setting`, `summary`

### Category Color System

Deterministic color assignment based on category name hash:

```typescript
getCategoryColor(category: string): { bg: string; text: string } {
  let hash = 0;
  for (let i = 0; i < category.length; i++) {
    hash = ((hash << 5) - hash) + category.charCodeAt(i);
    hash = hash & hash;
  }
  const index = Math.abs(hash) % this.categoryColors.length;
  return this.categoryColors[index];
}
```

### Backend Memory Deletion

The MemoryClient SDK doesn't expose delete methods, so boto3 is used directly:

```python
async def delete_memory(user_id: str, record_id: str) -> bool:
    client = boto3.client('bedrock-agentcore', region_name=config.region)
    response = client.batch_delete_memory_records(
        memoryId=config.memory_id,
        records=[{'memoryRecordId': record_id}]
    )
    return len(response.get('successfulRecords', [])) > 0
```

## UI/UX Design

### Layout

- Max width of `max-w-3xl` (~720px) for readability
- Centered content similar to sessions page
- Responsive padding with `px-4 py-8`

### Dark Mode Support

All components support dark mode with appropriate color variants:
- Light: `bg-gray-100`, `text-gray-900`
- Dark: `dark:bg-gray-900`, `dark:text-white`

### Hover Interactions

- Delete buttons hidden by default, revealed on row hover
- Uses Tailwind's `group` and `group-hover:opacity-100` pattern
- Smooth transitions with `transition-opacity`

## Memory Storage Architecture

### Strategies and Namespaces

AgentCore Memory organizes data using **strategies** and **namespaces**:

- **Strategies**: Define how memories are processed and stored
  - `SEMANTIC`: Extracts factual information (e.g., "The user's name is Phil")
  - `USER_PREFERENCE`: Captures user preferences (e.g., "User prefers concise responses")
  - `SUMMARIZATION`: Creates conversation summaries

- **Namespaces**: Hierarchical paths for organizing memories
  - Pattern: `/strategies/{strategyId}/actors/{actorId}`
  - Example: `/strategies/SemanticFactExtraction-ee9IM7Bhn4/actors/user123`

### Dynamic Strategy Discovery

Strategy IDs are dynamically discovered at runtime since they're generated by AWS:

```python
@lru_cache(maxsize=1)
def _get_strategy_namespaces() -> Tuple[Optional[str], Optional[str]]:
    """Discover actual strategy IDs from AgentCore Memory."""
    client = _get_memory_client()
    strategies = client.get_memory_strategies(memory_id=config.memory_id)

    for strategy in strategies:
        strategy_type = strategy.get('type') or strategy.get('memoryStrategyType')
        strategy_id = strategy.get('strategyId') or strategy.get('memoryStrategyId')

        if strategy_type == 'SEMANTIC':
            semantic_id = strategy_id
        elif strategy_type == 'USER_PREFERENCE':
            preference_id = strategy_id

    return semantic_id, preference_id
```

### Memory Scoping

Memories are scoped per user via the `actor_id` parameter (equivalent to `user_id` in our application). This ensures users only see and access their own memories.

## Memory Retrieval in Conversations

For the agent to recall stored facts in new sessions, the `TurnBasedSessionManager` must register a hook to retrieve long-term memories.

### Hook Registration

In `turn_based_session_manager.py`, the `register_hooks` method includes:

```python
def register_hooks(self, registry, **kwargs):
    # ... other hooks ...

    # CRITICAL: Register retrieve_customer_context hook for long-term memory retrieval
    # This queries the configured namespaces (preferences, facts) and injects
    # relevant memories as <user_context> into the conversation when a user message is added
    registry.add_callback(
        MessageAddedEvent,
        lambda event: self.base_manager.retrieve_customer_context(event)
    )
```

### How It Works

When a user sends a message:
1. `MessageAddedEvent` triggers the `retrieve_customer_context` hook
2. The hook queries configured namespaces using the user's query
3. Relevant memories are injected as `<user_context>` into the conversation
4. The agent can then reference this context in its response

### Retrieval Configuration

The `retrieval_config` in `session_factory.py` defines which namespaces to query:

```python
retrieval_config: Dict[str, RetrievalConfig] = {}

if preference_id:
    preference_namespace = f"/strategies/{preference_id}/actors/{{actorId}}"
    retrieval_config[preference_namespace] = RetrievalConfig(
        top_k=5,           # Return top 5 preferences
        relevance_score=0.5 # Minimum relevance threshold
    )

if semantic_id:
    facts_namespace = f"/strategies/{semantic_id}/actors/{{actorId}}"
    retrieval_config[facts_namespace] = RetrievalConfig(
        top_k=10,          # Return top 10 facts
        relevance_score=0.3 # Lower threshold for facts
    )
```

## Troubleshooting

### Memories Not Being Retrieved in Conversations

**Symptom**: Facts like "The user's name is Phil" exist in memory but the agent doesn't recall them in new sessions.

**Cause**: The `retrieve_customer_context` hook was not registered in `TurnBasedSessionManager.register_hooks()`.

**Fix**: Add the hook registration:

```python
registry.add_callback(
    MessageAddedEvent,
    lambda event: self.base_manager.retrieve_customer_context(event)
)
```

### Memories Not Showing in Dashboard

**Symptom**: Dashboard shows "No memories yet" despite having conversations.

**Cause**: Memory service was using incorrect namespace patterns (`/preferences/{userId}` instead of `/strategies/{strategyId}/actors/{userId}`).

**Fix**: Updated `memory_service.py` to dynamically discover strategy IDs and build correct namespaces using `_get_strategy_namespaces()`.

### Memory Delete Not Working

**Symptom**: Delete button doesn't remove memories.

**Cause**: The MemoryClient SDK wrapper doesn't expose delete methods.

**Fix**: Use boto3 directly with the `batch_delete_memory_records` API (see Backend Memory Deletion section above).

## Configuration

### Environment Variables

| Variable | Description |
|----------|-------------|
| `AGENTCORE_MEMORY_ID` | The AgentCore Memory ID from AWS Bedrock |
| `AWS_REGION` | AWS region (default: us-west-2) |

### Memory Mode Detection

The application automatically detects cloud vs local mode:

```python
def load_memory_config() -> MemoryConfig:
    memory_id = os.getenv("AGENTCORE_MEMORY_ID")
    is_cloud_mode = bool(memory_id) and AGENTCORE_MEMORY_AVAILABLE
    return MemoryConfig(
        memory_id=memory_id,
        region=os.getenv("AWS_REGION", "us-west-2"),
        is_cloud_mode=is_cloud_mode
    )
```

## Dependencies

### Frontend
- `@ng-icons/core` and `@ng-icons/heroicons/outline` for icons
- Angular signals and resources for state management

### Backend
- `boto3` for AWS API calls
- `bedrock_agentcore.memory` for MemoryClient (read operations)
- FastAPI for API routing

## Future Enhancements

1. **Bulk Delete**: Allow selecting and deleting multiple memories at once
2. **Edit Memories**: Allow users to modify memory content
3. **Memory Categories**: Filter by category/type
4. **Export**: Export memories as JSON/CSV
5. **Pagination**: Handle large numbers of memories with pagination
