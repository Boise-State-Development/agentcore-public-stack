# Quick Start Guide - Strands Agent

Get started with the refactored Strands Agent in 5 minutes.

## Installation

No additional dependencies needed! The refactored agent uses the same dependencies as the original.

## Basic Usage

### 1. Import the Agent

```python
from agents.strands_agent import StrandsAgent
```

### 2. Create an Agent

```python
agent = StrandsAgent(
    session_id="my-session",
    enabled_tools=["calculator", "weather"]
)
```

### 3. Stream Responses

```python
import asyncio

async def chat():
    async for event in agent.stream_async("What's 25 * 47?"):
        print(event, end="")

asyncio.run(chat())
```

That's it! 🎉

## Drop-in Replacement

Replace your existing `ChatbotAgent` usage:

### Before
```python
from agentcore.agent.agent import ChatbotAgent

agent = ChatbotAgent(
    session_id="session-123",
    enabled_tools=["calculator"]
)
```

### After
```python
from agents.strands_agent import StrandsAgent

agent = StrandsAgent(
    session_id="session-123",
    enabled_tools=["calculator"]
)
```

**Same API, better architecture!**

## Common Scenarios

### Simple Chat
```python
from agents.strands_agent import StrandsAgent

agent = StrandsAgent(session_id="chat-001")

async for event in agent.stream_async("Hello!"):
    print(event)
```

### With Custom Model
```python
agent = StrandsAgent(
    session_id="session-001",
    model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    temperature=0.5
)
```

### With Tools
```python
agent = StrandsAgent(
    session_id="tools-session",
    enabled_tools=["calculator", "weather", "gateway_wikipedia"]
)

async for event in agent.stream_async("What's the weather in Seattle?"):
    print(event)
```

### With Images
```python
import base64
from dataclasses import dataclass

@dataclass
class FileContent:
    filename: str
    content_type: str
    bytes: str

# Load image
with open("chart.png", "rb") as f:
    image_bytes = base64.b64encode(f.read()).decode()

files = [FileContent("chart.png", "image/png", image_bytes)]

agent = StrandsAgent(session_id="image-session")
async for event in agent.stream_async("Analyze this chart", files=files):
    print(event)
```

## Configuration Options

All constructor parameters:

```python
agent = StrandsAgent(
    session_id="required-session-id",
    user_id="optional-user-id",              # defaults to session_id
    enabled_tools=["calculator", "weather"], # None = no tools
    model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
    temperature=0.7,                         # 0.0 - 1.0
    system_prompt="Custom prompt",           # optional
    caching_enabled=True                     # default: True
)
```

## Environment Setup

### Cloud Mode (AgentCore Memory)
```bash
export AGENTCORE_MEMORY_ID="your-memory-id"
export AWS_REGION="us-west-2"
```

### Local Mode (File-based)
No environment variables needed. Sessions stored in `backend/src/sessions/`.

## Next Steps

- 📖 Read [README.md](./strands_agent/README.md) for architecture details
- 📝 See [USAGE_EXAMPLES.md](./strands_agent/USAGE_EXAMPLES.md) for advanced patterns
- 🔧 Explore individual modules in `strands_agent/` directory

## Help

- **Documentation**: See `strands_agent/README.md`
- **Examples**: See `strands_agent/USAGE_EXAMPLES.md`
- **Source**: Browse `strands_agent/` directory

## Troubleshooting

### Import Error
```python
# Error: ModuleNotFoundError: No module named 'agents'

# Solution: Make sure you're running from backend/src directory
cd backend/src
python your_script.py
```

### No Tools Available
```python
# If no tools are working, check enabled_tools list

agent = StrandsAgent(
    session_id="debug",
    enabled_tools=["calculator"]  # Make sure tools are listed
)

# Check available tools
stats = agent.get_tool_statistics()
print(stats)
```

### Session Not Persisting
```python
# Make sure session_id is consistent across calls

# Good: Same session_id
agent1 = StrandsAgent(session_id="user-123")
agent2 = StrandsAgent(session_id="user-123")  # Same conversation

# Bad: Different session_id
agent1 = StrandsAgent(session_id="session-1")
agent2 = StrandsAgent(session_id="session-2")  # New conversation
```

## Performance Tips

1. **Enable Caching** (enabled by default)
   ```python
   agent = StrandsAgent(session_id="s", caching_enabled=True)
   ```

2. **Use Appropriate Model**
   - Fast: `claude-haiku-4-5`
   - Balanced: `claude-sonnet-4-5`

3. **Only Enable Needed Tools**
   ```python
   # Good: Only what you need
   enabled_tools=["calculator"]

   # Avoid: All tools when not needed
   enabled_tools=["calculator", "weather", "browser", ...]
   ```

## Migration Checklist

- [ ] Replace imports: `ChatbotAgent` → `StrandsAgent`
- [ ] Test basic streaming
- [ ] Test with your enabled_tools
- [ ] Test multimodal (if used)
- [ ] Verify session persistence
- [ ] Check error handling
- [ ] Update documentation
- [ ] Deploy and monitor

## That's It!

You're ready to use the refactored Strands Agent. The API is identical to `ChatbotAgent` but with better architecture under the hood.

Happy coding! 🚀
