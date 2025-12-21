# Strands Agent Usage Examples

Practical examples for using the refactored Strands Agent.

## Basic Usage

### Simple Text Conversation

```python
import asyncio
from agents.strands_agent import StrandsAgent

async def simple_chat():
    # Create agent with default settings
    agent = StrandsAgent(
        session_id="demo-session-001",
        enabled_tools=["calculator", "weather"]
    )

    # Stream a simple message
    async for event in agent.stream_async("What's 25 * 47?"):
        print(event, end="")

# Run
asyncio.run(simple_chat())
```

### Custom Model Configuration

```python
from agents.strands_agent import StrandsAgent

agent = StrandsAgent(
    session_id="session-001",
    user_id="user-123",
    enabled_tools=["calculator", "weather", "visualization"],
    model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    temperature=0.3,
    caching_enabled=True
)

async for event in agent.stream_async("Analyze Q4 sales trends"):
    print(event)
```

### Custom System Prompt

```python
from agents.strands_agent import StrandsAgent

custom_prompt = """You are a financial analyst AI assistant.
Focus on providing data-driven insights with citations.
Current date: 2024-01-15"""

agent = StrandsAgent(
    session_id="finance-session",
    system_prompt=custom_prompt,
    enabled_tools=["calculator", "gateway_finance"]
)
```

## Multimodal Examples

### Image Analysis

```python
import base64
from dataclasses import dataclass
from agents.strands_agent import StrandsAgent

@dataclass
class FileContent:
    filename: str
    content_type: str
    bytes: str  # base64 encoded

async def analyze_image():
    agent = StrandsAgent(session_id="image-session")

    # Load and encode image
    with open("chart.png", "rb") as f:
        image_bytes = base64.b64encode(f.read()).decode()

    files = [
        FileContent(
            filename="chart.png",
            content_type="image/png",
            bytes=image_bytes
        )
    ]

    async for event in agent.stream_async(
        message="What trends do you see in this chart?",
        files=files
    ):
        print(event)

asyncio.run(analyze_image())
```

### Document Processing

```python
import base64
from agents.strands_agent import StrandsAgent

async def process_pdf():
    agent = StrandsAgent(
        session_id="doc-session",
        enabled_tools=["calculator"]
    )

    # Load PDF
    with open("report.pdf", "rb") as f:
        pdf_bytes = base64.b64encode(f.read()).decode()

    files = [
        FileContent(
            filename="Q4_Report.pdf",
            content_type="application/pdf",
            bytes=pdf_bytes
        )
    ]

    async for event in agent.stream_async(
        message="Summarize the key findings in this report",
        files=files
    ):
        print(event)

asyncio.run(process_pdf())
```

### Multiple Files

```python
async def multi_file_analysis():
    agent = StrandsAgent(session_id="multi-file")

    files = [
        FileContent(
            filename="sales_data.csv",
            content_type="text/csv",
            bytes=base64_encode(csv_content)
        ),
        FileContent(
            filename="previous_chart.png",
            content_type="image/png",
            bytes=base64_encode(image_content)
        ),
        FileContent(
            filename="analysis.docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            bytes=base64_encode(docx_content)
        )
    ]

    async for event in agent.stream_async(
        message="Compare this quarter's data to the previous analysis",
        files=files
    ):
        print(event)
```

## Advanced Usage

### Using Individual Modules

#### Custom Tool Registry

```python
from agents.strands_agent import ToolRegistry, ToolFilter
from strands_tools.calculator import calculator

# Create custom registry
registry = ToolRegistry()
registry.register_tool("calculator", calculator)

# Add custom tool
from my_custom_tools import custom_search

registry.register_tool("custom_search", custom_search)

# Filter tools
filter = ToolFilter(registry)
tools, gateway_ids = filter.filter_tools(["calculator", "custom_search"])

print(f"Enabled tools: {len(tools)}")
print(f"Statistics: {filter.get_statistics(['calculator', 'custom_search'])}")
```

#### Custom System Prompt Builder

```python
from agents.strands_agent import SystemPromptBuilder

# Custom base prompt
builder = SystemPromptBuilder(
    base_prompt="You are a coding assistant specializing in Python."
)

# Build with date
prompt = builder.build(include_date=True)
print(prompt)

# Build without date
prompt_no_date = builder.build(include_date=False)
```

#### Model Configuration

```python
from agents.strands_agent import ModelConfig

# Create configuration
config = ModelConfig.from_params(
    model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
    temperature=0.8,
    caching_enabled=False
)

# Get Bedrock-compatible config
bedrock_config = config.to_bedrock_config()
print(bedrock_config)
# {'model_id': '...', 'temperature': 0.8}

# Get full config
full_config = config.to_dict()
print(full_config)
# {'model_id': '...', 'temperature': 0.8, 'caching_enabled': False}
```

### Gateway MCP Tools

```python
from agents.strands_agent import StrandsAgent

# Enable Gateway MCP tools (Wikipedia, ArXiv, Finance)
agent = StrandsAgent(
    session_id="research-session",
    enabled_tools=[
        "calculator",
        "gateway_wikipedia",
        "gateway_arxiv",
        "gateway_finance"
    ]
)

async for event in agent.stream_async(
    "Find recent research papers on quantum computing and summarize key findings"
):
    print(event)
```

### Session Management

#### Cloud Mode (AgentCore Memory)

```python
import os
from agents.strands_agent import StrandsAgent

# Set environment for cloud mode
os.environ['AGENTCORE_MEMORY_ID'] = 'your-memory-id'
os.environ['AWS_REGION'] = 'us-west-2'

# Agent will automatically use AgentCore Memory
agent = StrandsAgent(
    session_id="cloud-session",
    user_id="user-123",  # For cross-session preferences
    enabled_tools=["calculator", "weather"]
)

# User preferences and facts are automatically retrieved
async for event in agent.stream_async("What were we discussing last time?"):
    print(event)
```

#### Local Mode (File-based)

```python
from agents.strands_agent import StrandsAgent

# No AGENTCORE_MEMORY_ID set - automatically uses file-based storage
agent = StrandsAgent(
    session_id="local-session",
    enabled_tools=["calculator"]
)

# Sessions stored in backend/src/sessions/
async for event in agent.stream_async("Calculate 100 * 200"):
    print(event)
```

### Tool Statistics

```python
from agents.strands_agent import StrandsAgent

agent = StrandsAgent(
    session_id="stats-session",
    enabled_tools=["calculator", "weather", "gateway_wikipedia", "unknown_tool"]
)

# Get tool statistics
stats = agent.get_tool_statistics()
print(stats)
# {
#     'total_requested': 4,
#     'local_tools': 2,
#     'gateway_tools': 1,
#     'unknown_tools': 1
# }
```

### Model Configuration Inspection

```python
from agents.strands_agent import StrandsAgent

agent = StrandsAgent(
    session_id="config-session",
    model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    temperature=0.5,
    caching_enabled=True
)

# Get current configuration
config = agent.get_model_config()
print(config)
# {
#     'model_id': 'us.anthropic.claude-sonnet-4-5-20250929-v1:0',
#     'temperature': 0.5,
#     'caching_enabled': True,
#     'system_prompts': ['...']
# }
```

## Integration Examples

### FastAPI Integration

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from agents.strands_agent import StrandsAgent

app = FastAPI()

@app.post("/chat/stream")
async def chat_stream(session_id: str, message: str, enabled_tools: list[str]):
    agent = StrandsAgent(
        session_id=session_id,
        enabled_tools=enabled_tools
    )

    return StreamingResponse(
        agent.stream_async(message),
        media_type="text/event-stream"
    )
```

### With Error Handling

```python
import asyncio
from agents.strands_agent import StrandsAgent

async def safe_stream():
    agent = StrandsAgent(session_id="safe-session")

    try:
        async for event in agent.stream_async("What's the weather?"):
            print(event)
    except Exception as e:
        print(f"Error: {e}")
        # Error is also sent as SSE event to client

asyncio.run(safe_stream())
```

### Logging and Monitoring

```python
import logging
from agents.strands_agent import StrandsAgent

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

agent = StrandsAgent(
    session_id="monitored-session",
    enabled_tools=["calculator", "weather"]
)

# All agent operations are logged
# - Tool loading
# - Session creation
# - Streaming events
# - Errors

async for event in agent.stream_async("Calculate 5 + 5"):
    print(event)
```

## Testing Examples

### Unit Testing Individual Modules

```python
import pytest
from agents.strands_agent import ModelConfig, ToolRegistry

def test_model_config():
    config = ModelConfig.from_params(temperature=0.9)
    assert config.temperature == 0.9
    assert config.caching_enabled == True

    bedrock_config = config.to_bedrock_config()
    assert "cache_prompt" in bedrock_config

def test_tool_registry():
    registry = ToolRegistry()
    registry.register_tool("test_tool", lambda: "test")

    assert registry.has_tool("test_tool")
    assert registry.get_tool_count() == 1
```

### Integration Testing

```python
import asyncio
import pytest
from agents.strands_agent import StrandsAgent

@pytest.mark.asyncio
async def test_agent_streaming():
    agent = StrandsAgent(
        session_id="test-session",
        enabled_tools=["calculator"]
    )

    events = []
    async for event in agent.stream_async("What is 2 + 2?"):
        events.append(event)

    assert len(events) > 0
```

## Migration Examples

### Migrating from ChatbotAgent

**Before:**
```python
from agentcore.agent.agent import ChatbotAgent

agent = ChatbotAgent(
    session_id="session-123",
    user_id="user-456",
    enabled_tools=["calculator", "weather"],
    model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
    temperature=0.7,
    system_prompt=None,
    caching_enabled=True
)

async for event in agent.stream_async(message="Hello", session_id="session-123"):
    print(event)
```

**After:**
```python
from agents.strands_agent import StrandsAgent

agent = StrandsAgent(
    session_id="session-123",
    user_id="user-456",
    enabled_tools=["calculator", "weather"],
    model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
    temperature=0.7,
    system_prompt=None,
    caching_enabled=True
)

async for event in agent.stream_async(message="Hello", session_id="session-123"):
    print(event)
```

**Identical API** - Just change the import!

## Best Practices

### 1. Session Management

```python
# Good: Reuse agent for same session
agent = StrandsAgent(session_id="user-session-123")

async for event in agent.stream_async("First message"):
    print(event)

async for event in agent.stream_async("Follow-up message"):
    print(event)  # Has context from first message


# Avoid: Creating new agent per message
# (loses conversation context)
```

### 2. Tool Selection

```python
# Good: Only enable tools you need
agent = StrandsAgent(
    session_id="math-session",
    enabled_tools=["calculator"]  # Only math tools
)

# Avoid: Enabling all tools unnecessarily
# (increases token usage, slower responses)
```

### 3. Error Handling

```python
# Good: Handle errors gracefully
try:
    async for event in agent.stream_async(message):
        process_event(event)
except Exception as e:
    logger.error(f"Stream failed: {e}")
    # Error already sent to client as SSE event
```

### 4. Resource Cleanup

```python
# Agents clean up automatically
# Session managers flush on completion
# No manual cleanup needed

async for event in agent.stream_async(message):
    print(event)
# Session flushed automatically
```

## Performance Tips

### 1. Enable Caching

```python
# Recommended: Enable caching for better performance
agent = StrandsAgent(
    session_id="session",
    caching_enabled=True  # Reduces token usage
)
```

### 2. Use Appropriate Model

```python
# Fast responses: Use Haiku
agent = StrandsAgent(
    session_id="quick-session",
    model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0"
)

# Complex reasoning: Use Sonnet
agent = StrandsAgent(
    session_id="complex-session",
    model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)
```

### 3. Optimize File Sizes

```python
# Good: Resize images before sending
from PIL import Image
import io

img = Image.open("large_image.png")
img.thumbnail((1024, 1024))  # Reduce size

buffer = io.BytesIO()
img.save(buffer, format="PNG")
optimized_bytes = base64.b64encode(buffer.getvalue()).decode()
```
