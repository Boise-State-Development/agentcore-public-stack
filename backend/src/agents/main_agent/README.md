# Strands Agent - Modular Architecture

A well-architected, maintainable implementation of a conversational AI agent with clear separation of concerns.

## Architecture Overview

This refactored agent implementation splits the monolithic `agentcore.agent.agent.py` into focused, single-responsibility modules:

```
main_agent/
├── main_agent.py              # Main orchestrator (~200 lines)
├── core/                         # Core orchestration
│   ├── model_config.py           # Model configuration & validation
│   ├── system_prompt_builder.py  # System prompt construction
│   └── agent_factory.py          # Strands Agent factory
├── session/                      # Session management
│   ├── session_factory.py        # Session manager selection (cloud vs local)
│   └── hooks/                    # Agent lifecycle hooks
├── tools/                        # Tool management
│   ├── tool_registry.py          # Tool discovery & registration
│   ├── tool_filter.py            # User preference filtering
│   └── gateway_integration.py    # MCP Gateway client management
├── multimodal/                   # Multimodal content handling
│   ├── prompt_builder.py         # ContentBlock construction
│   ├── image_handler.py          # Image format detection
│   ├── document_handler.py       # Document format detection
│   └── file_sanitizer.py         # Filename sanitization
├── streaming/                    # Streaming coordination
│   └── stream_coordinator.py     # Streaming lifecycle management
└── utils/                        # Shared utilities
    ├── timezone.py               # Date/timezone helpers
    └── global_state.py           # Global stream processor
```

## Design Principles

### 1. Single Responsibility
Each module has one focused responsibility:
- `ModelConfig`: Model configuration only
- `ToolRegistry`: Tool discovery and storage
- `ToolFilter`: Tool filtering logic
- `SessionFactory`: Session manager creation

### 2. Dependency Injection
Components receive dependencies rather than creating them:
```python
tool_filter = ToolFilter(registry=tool_registry)
```

### 3. Testability
Small, focused modules with clear interfaces are easy to unit test.

### 4. Maintainability
Changes are isolated to specific modules. For example:
- Add new document format → Edit `document_handler.py`
- Change session logic → Edit `session_factory.py`
- Add new tool source → Edit `tool_registry.py`

## Usage

### Basic Usage

```python
from agents.main_agent import MainAgent

# Create agent
agent = MainAgent(
    session_id="session-123",
    user_id="user-456",
    enabled_tools=["calculator", "fetch_url_content", "gateway_wikipedia"],
    model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
    temperature=0.7,
    caching_enabled=True
)

# Stream responses
async for event in agent.stream_async(
    message="Summarize this page: https://example.com",
    files=None
):
    print(event)
```

### With Multimodal Input

```python
from agents.main_agent import MainAgent

agent = MainAgent(session_id="session-123")

# Stream with files
files = [
    FileContent(
        filename="chart.png",
        content_type="image/png",
        bytes=base64_encoded_bytes
    )
]

async for event in agent.stream_async(
    message="Analyze this chart",
    files=files
):
    print(event)
```

### Using Individual Modules

```python
from agents.main_agent import (
    ModelConfig,
    ToolRegistry,
    ToolFilter,
    SystemPromptBuilder
)

# Configure model
config = ModelConfig.from_params(
    model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    temperature=0.5,
    caching_enabled=True
)

# Build custom prompt
builder = SystemPromptBuilder(base_prompt="You are a helpful assistant")
prompt = builder.build(include_date=True)

# Filter tools
registry = create_default_registry()
filter = ToolFilter(registry)
tools, gateway_ids = filter.filter_tools(["calculator", "fetch_url_content"])
```

## Module Details

### Core Module

**ModelConfig** (`core/model_config.py`)
- Manages model configuration (model_id, temperature, caching)
- Converts to BedrockModel format
- Factory method for creating from optional params

**SystemPromptBuilder** (`core/system_prompt_builder.py`)
- Builds system prompts with optional date injection
- Supports custom base prompts
- Handles BFF-provided prompts (date already included)

**AgentFactory** (`core/agent_factory.py`)
- Creates Strands Agent instances
- Assembles model, tools, session manager, and hooks
- Uses SequentialToolExecutor for browser tools

### Session Module

**SessionFactory** (`session/session_factory.py`)
- Selects appropriate session manager based on environment
- Cloud mode: AgentCore Memory with turn-based buffering
- Local mode: File-based with buffering wrapper
- Handles user preferences and facts retrieval

**Hooks** (`session/hooks/`)
- Re-exports existing hooks from `agentcore.agent.hooks`
- `StopHook`: Session cancellation support
- Note: Prompt caching is handled by `CacheConfig(strategy="auto")` in the BedrockModel

### Tools Module

**ToolRegistry** (`tools/tool_registry.py`)
- Discovers and registers tools from modules
- Supports Strands built-in, local, and AWS SDK tools
- Provides tool lookup by ID

**ToolFilter** (`tools/tool_filter.py`)
- Filters tools based on user preferences
- Separates local tools from gateway tools
- Provides filtering statistics

**GatewayIntegration** (`tools/gateway_integration.py`)
- Manages MCP Gateway client lifecycle
- Integrates with Strands 1.16+ Managed Integration
- Handles gateway tool filtering

### Multimodal Module

**PromptBuilder** (`multimodal/prompt_builder.py`)
- Converts text + files to ContentBlock format
- Delegates to specialized handlers
- Provides content type summaries

**ImageHandler** (`multimodal/image_handler.py`)
- Detects image format (png, jpeg, gif, webp)
- Creates image ContentBlocks
- Validates supported formats

**DocumentHandler** (`multimodal/document_handler.py`)
- Detects document format (pdf, csv, docx, etc.)
- Creates document ContentBlocks
- Maps file extensions to formats

**FileSanitizer** (`multimodal/file_sanitizer.py`)
- Sanitizes filenames for AWS Bedrock
- Removes special characters
- Handles whitespace normalization

### Streaming Module

**StreamCoordinator** (`streaming/stream_coordinator.py`)
- Manages streaming lifecycle
- Handles session flushing
- Emergency flush on errors
- Creates SSE error events

### Utils Module

**Timezone** (`utils/timezone.py`)
- Gets current date in Pacific timezone
- Handles zoneinfo and pytz fallbacks
- Formats for system prompts

**Global State** (`utils/global_state.py`)
- Manages global stream processor reference
- Temporary solution (consider DI refactoring)

## Migration from Original Agent

The new `MainAgent` class is a drop-in replacement for `ChatbotAgent`:

### Before (Original)
```python
from agentcore.agent.agent import ChatbotAgent

agent = ChatbotAgent(
    session_id="session-123",
    user_id="user-456",
    enabled_tools=["calculator"],
    model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
    temperature=0.7,
    caching_enabled=True
)
```

### After (Refactored)
```python
from agents.main_agent import MainAgent

agent = MainAgent(
    session_id="session-123",
    user_id="user-456",
    enabled_tools=["calculator"],
    model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
    temperature=0.7,
    caching_enabled=True
)
```

**API Compatibility:**
- `stream_async()` - Identical signature
- `get_model_config()` - Identical return format
- All constructor parameters supported

## Benefits of Refactored Architecture

### 1. Maintainability
- **Before**: 546 lines in single file
- **After**: Largest module is ~150 lines
- Changes isolated to specific modules

### 2. Testability
- Each module can be unit tested independently
- Mock dependencies easily
- Clear interfaces between components

### 3. Reusability
- Modules can be used independently
- Easy to create variants (e.g., different prompt builders)
- Tool registry can be customized per agent

### 4. Extensibility
- Add new document format: Edit `document_handler.py`
- Add new tool source: Edit `tool_registry.py`
- Add new session type: Edit `session_factory.py`
- No need to touch other modules

### 5. Code Organization
- Related functionality grouped together
- Clear module boundaries
- Easy to find and understand code

## Future Enhancements

### Dependency Injection
Replace global stream processor with proper DI:
```python
agent = MainAgent(
    session_id="session-123",
    stream_processor=custom_processor  # Inject instead of global
)
```

### Plugin Architecture
Support dynamic tool loading:
```python
registry = ToolRegistry()
registry.load_plugins_from_directory("./custom_tools")
```

### Configuration Files
Support YAML/JSON configuration:
```python
agent = MainAgent.from_config_file("agent_config.yaml")
```

### Metrics & Monitoring
Add dedicated metrics module:
```python
from agents.main_agent.metrics import AgentMetrics

metrics = AgentMetrics(agent)
print(metrics.get_tool_usage_stats())
```

## Comparison: Old vs New

| Aspect | Old (`agent.py`) | New (Modular) |
|--------|------------------|---------------|
| Lines of code | 546 in 1 file | ~150 max per module |
| Modules | 1 monolithic | 9 focused modules |
| Testability | Low (many dependencies) | High (isolated modules) |
| Maintainability | Difficult (find code) | Easy (clear structure) |
| Extensibility | Modify large file | Edit specific module |
| Reusability | All or nothing | Pick modules needed |
| Documentation | Comments in code | Module-level docs |

## File Size Comparison

```
Original:
  agent.py: 546 lines

Refactored:
  main_agent.py: ~200 lines (orchestrator)

  core/
    model_config.py: ~70 lines
    system_prompt_builder.py: ~80 lines
    agent_factory.py: ~65 lines

  session/
    session_factory.py: ~145 lines

  tools/
    tool_registry.py: ~105 lines
    tool_filter.py: ~110 lines
    gateway_integration.py: ~75 lines

  multimodal/
    prompt_builder.py: ~125 lines
    image_handler.py: ~80 lines
    document_handler.py: ~90 lines
    file_sanitizer.py: ~30 lines

  streaming/
    stream_coordinator.py: ~110 lines

  utils/
    timezone.py: ~55 lines
    global_state.py: ~25 lines
```

**Total**: ~1,365 lines (vs 546 original)

While line count increased, code is:
- More maintainable (smaller files)
- Better documented
- More testable
- More reusable
- Easier to understand

## Contributing

When adding new functionality:

1. **Identify the correct module** based on responsibility
2. **Keep modules focused** - if a module grows too large, consider splitting
3. **Update `__init__.py`** exports for public APIs
4. **Document changes** in module docstrings
5. **Maintain backward compatibility** in `MainAgent` public API

## License

Same as parent project (MIT)
