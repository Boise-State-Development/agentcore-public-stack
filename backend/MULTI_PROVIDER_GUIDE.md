# Multi-Provider LLM Support Guide

## Overview

The Strands Agent now supports three major LLM providers:

1. **AWS Bedrock** (default) - Claude models via AWS infrastructure
2. **OpenAI** - GPT-4o, GPT-4 Turbo, o1 series
3. **Google Gemini** - Gemini 2.5 Flash, Gemini 2.5 Pro

This guide explains how to configure and use each provider in your application.

---

## Quick Start

### 1. Configure API Keys

Copy the example environment file and add your API keys:

```bash
cd backend/src
cp .env.example .env
```

Edit `.env` and add the keys for the providers you want to use:

```bash
# OpenAI (optional)
OPENAI_API_KEY=sk-proj-...

# Google Gemini (optional)
GOOGLE_GEMINI_API_KEY=AIza...

# AWS Bedrock uses AWS credentials (already configured)
AWS_REGION=us-west-2
AWS_PROFILE=default
```

### 2. Use Different Providers

The agent automatically detects the provider from the `model_id`, or you can specify it explicitly:

```python
from agents.strands_agent.strands_agent import StrandsAgent

# Auto-detect from model_id (OpenAI)
agent = StrandsAgent(
    session_id="session-123",
    model_id="gpt-4o"
)

# Explicit provider specification
agent = StrandsAgent(
    session_id="session-123",
    model_id="gemini-2.5-flash",
    provider="gemini"
)

# Bedrock (default)
agent = StrandsAgent(
    session_id="session-123",
    model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0"
)
```

---

## Supported Models

### AWS Bedrock

**API Key**: Not required (uses AWS credentials)

**Available Models**:
- `us.anthropic.claude-haiku-4-5-20251001-v1:0` (fast, cost-effective)
- `us.anthropic.claude-sonnet-4-20250514-v1:0` (balanced)
- `us.anthropic.claude-opus-4-20250514-v1:0` (most capable)

**Features**:
- ✅ Prompt caching (set `caching_enabled=True`)
- ✅ All tools supported
- ✅ Multimodal input (images, documents)

**Example**:
```python
agent = StrandsAgent(
    session_id="session-123",
    model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
    temperature=0.7,
    caching_enabled=True
)
```

---

### OpenAI

**API Key**: Get from [platform.openai.com/api-keys](https://platform.openai.com/api-keys)

**Available Models**:
- `gpt-4o` (recommended, multimodal)
- `gpt-4o-mini` (fast, cost-effective)
- `gpt-4-turbo` (previous generation)
- `o1-preview` (reasoning model)
- `o1-mini` (fast reasoning)

**Features**:
- ✅ All tools supported
- ✅ Multimodal input (images)
- ✅ Function calling
- ❌ Prompt caching (not available via OpenAI API)

**Example**:
```python
agent = StrandsAgent(
    session_id="session-123",
    model_id="gpt-4o",
    provider="openai",  # Optional, auto-detected
    temperature=0.7,
    max_tokens=4096
)
```

---

### Google Gemini

**API Key**: Get from [aistudio.google.com/apikey](https://aistudio.google.com/apikey)

**Available Models**:
- `gemini-2.5-flash` (fast, cost-effective)
- `gemini-2.5-pro` (most capable)
- `gemini-1.5-flash` (previous generation)
- `gemini-1.5-pro` (previous generation)

**Features**:
- ✅ All tools supported
- ✅ Multimodal input (images, documents)
- ✅ Function calling
- ❌ Prompt caching (not available via Gemini API)

**Example**:
```python
agent = StrandsAgent(
    session_id="session-123",
    model_id="gemini-2.5-flash",
    provider="gemini",  # Optional, auto-detected
    temperature=0.7,
    max_tokens=8192
)
```

---

## API Usage

### REST API Request

Send provider configuration via the `/chat/invocations` endpoint:

```json
POST /chat/invocations
{
  "session_id": "session-123",
  "message": "What's the weather in Tokyo?",
  "model_id": "gpt-4o",
  "provider": "openai",
  "temperature": 0.7,
  "max_tokens": 2048,
  "enabled_tools": ["weather", "web_search"]
}
```

### Provider Auto-Detection

You can omit the `provider` field - it will be auto-detected from `model_id`:

```json
{
  "session_id": "session-123",
  "message": "Analyze this data",
  "model_id": "gemini-2.5-flash"
  // provider auto-detected as "gemini"
}
```

**Detection Rules**:
- Starts with `gpt-` or `o1-` → OpenAI
- Starts with `gemini-` → Gemini
- Contains `anthropic` or `claude` → Bedrock
- Default → Bedrock

---

## Configuration Parameters

### Common Parameters

All providers support these parameters:

| Parameter | Type | Description | Default |
|-----------|------|-------------|---------|
| `session_id` | string | Session identifier (required) | - |
| `model_id` | string | Model identifier | Provider default |
| `provider` | string | Provider name (`bedrock`, `openai`, `gemini`) | Auto-detect |
| `temperature` | float | Sampling temperature (0.0-1.0) | 0.7 |
| `max_tokens` | int | Maximum tokens to generate | Provider default |
| `system_prompt` | string | System prompt text | Default agent prompt |
| `enabled_tools` | list | List of tool IDs to enable | All tools |

### Provider-Specific Parameters

**Bedrock Only**:
- `caching_enabled` (bool): Enable prompt caching for cost savings (default: `true`)

**OpenAI & Gemini**:
- Prompt caching not available
- Set `caching_enabled=false` or omit it

---

## Cost Optimization

### Provider Cost Comparison (Approximate)

| Provider | Model | Input (per 1M tokens) | Output (per 1M tokens) |
|----------|-------|----------------------|------------------------|
| Bedrock | Claude Haiku 4.5 | $0.80 | $4.00 |
| Bedrock | Claude Sonnet 4 | $3.00 | $15.00 |
| OpenAI | GPT-4o | $2.50 | $10.00 |
| OpenAI | GPT-4o-mini | $0.15 | $0.60 |
| Gemini | Gemini 2.5 Flash | $0.10 | $0.40 |
| Gemini | Gemini 2.5 Pro | $1.25 | $5.00 |

### Recommendations

**For Production (Quality + Cost)**:
- Primary: `us.anthropic.claude-sonnet-4-20250514-v1:0` (Bedrock)
- Alternative: `gpt-4o` (OpenAI)

**For Development/Testing**:
- Primary: `gemini-2.5-flash` (Gemini) - cheapest
- Alternative: `gpt-4o-mini` (OpenAI)
- Bedrock: `us.anthropic.claude-haiku-4-5-20251001-v1:0`

**For Maximum Performance**:
- Bedrock: `us.anthropic.claude-opus-4-20250514-v1:0`
- Gemini: `gemini-2.5-pro`

### Prompt Caching (Bedrock Only)

Enable prompt caching to reduce costs by up to 90% for repeated system prompts:

```python
agent = StrandsAgent(
    session_id="session-123",
    model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
    caching_enabled=True  # Bedrock only
)
```

---

## Error Handling

### Missing API Keys

If an API key is not configured, you'll get a clear error:

```python
ValueError: OPENAI_API_KEY environment variable is required for OpenAI models.
Please set it in your .env file.
```

**Solution**: Add the required API key to your `.env` file.

### Invalid Provider

```python
ValueError: Unsupported model provider: invalid-provider
```

**Solution**: Use one of: `bedrock`, `openai`, or `gemini`

### Rate Limiting

Each provider has different rate limits:

- **OpenAI**: Tier-based (see your [usage limits](https://platform.openai.com/account/limits))
- **Gemini**: 1500 requests/day (free tier), higher for paid
- **Bedrock**: Based on AWS account limits

**Recommendation**: Implement exponential backoff retry logic in production.

---

## Migration Guide

### From Bedrock-Only to Multi-Provider

**Before**:
```python
agent = StrandsAgent(
    session_id="session-123",
    model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0"
)
```

**After (with OpenAI)**:
```python
# Option 1: Auto-detect provider
agent = StrandsAgent(
    session_id="session-123",
    model_id="gpt-4o"  # Auto-detects OpenAI
)

# Option 2: Explicit provider
agent = StrandsAgent(
    session_id="session-123",
    model_id="gpt-4o",
    provider="openai"
)
```

### Frontend Changes

Update your chat request to include provider:

```typescript
// Before
const request = {
  session_id: sessionId,
  message: userMessage,
  model_id: "us.anthropic.claude-haiku-4-5-20251001-v1:0"
};

// After
const request = {
  session_id: sessionId,
  message: userMessage,
  model_id: "gpt-4o",
  provider: "openai",  // Optional
  max_tokens: 4096
};
```

---

## Architecture Details

### Component Overview

```
┌─────────────────────────────────────────┐
│         StrandsAgent                    │
│  (Multi-provider orchestration)         │
└───────────────┬─────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────┐
│         ModelConfig                     │
│  (Provider detection & config)          │
└───────────────┬─────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────┐
│         AgentFactory                    │
│  (Creates provider-specific models)     │
└─────┬──────────────┬──────────────┬─────┘
      │              │              │
      ▼              ▼              ▼
┌──────────┐  ┌───────────┐  ┌────────────┐
│ Bedrock  │  │  OpenAI   │  │   Gemini   │
│  Model   │  │   Model   │  │    Model   │
└──────────┘  └───────────┘  └────────────┘
```

### Key Files Modified

1. **`agents/strands_agent/core/model_config.py`**
   - Added `ModelProvider` enum
   - Added provider auto-detection
   - Added `to_openai_config()` and `to_gemini_config()` methods

2. **`agents/strands_agent/core/agent_factory.py`**
   - Added `_create_openai_model()` and `_create_gemini_model()`
   - Added provider-based model instantiation
   - Added API key validation

3. **`agents/strands_agent/strands_agent.py`**
   - Added `provider` and `max_tokens` parameters
   - Updated documentation

4. **`apis/inference_api/chat/service.py`**
   - Updated cache key to include provider
   - Updated `get_agent()` signature

5. **`apis/inference_api/chat/models.py`**
   - Added `provider` and `max_tokens` fields to `InvocationRequest`

---

## Testing

### Test OpenAI Integration

```python
import os
os.environ['OPENAI_API_KEY'] = 'sk-proj-...'

from agents.strands_agent.strands_agent import StrandsAgent

agent = StrandsAgent(
    session_id="test-openai",
    model_id="gpt-4o-mini",
    temperature=0.7
)

# Test streaming
async for event in agent.stream_async("Hello, can you hear me?"):
    print(event)
```

### Test Gemini Integration

```python
import os
os.environ['GOOGLE_GEMINI_API_KEY'] = 'AIza...'

from agents.strands_agent.strands_agent import StrandsAgent

agent = StrandsAgent(
    session_id="test-gemini",
    model_id="gemini-2.5-flash",
    temperature=0.7
)

# Test streaming
async for event in agent.stream_async("What's the capital of France?"):
    print(event)
```

---

## Troubleshooting

### Issue: "Cannot import name 'OpenAIModel'"

**Cause**: The Strands library doesn't export OpenAI/Gemini models by default.

**Solution**: The code uses direct imports:
```python
from strands.models.openai import OpenAIModel
from strands.models.gemini import GeminiModel
```

### Issue: Tools not working with OpenAI/Gemini

**Cause**: All Strands-based tools should work across providers.

**Solution**: If you encounter issues, check the Strands documentation for provider-specific tool limitations.

### Issue: Prompt caching not reducing costs

**Cause**: Prompt caching only works with Bedrock.

**Solution**: Use `caching_enabled=True` with Bedrock models only. For OpenAI/Gemini, focus on reducing prompt length.

---

## Best Practices

1. **Use environment variables** for API keys (never hardcode)
2. **Auto-detect providers** when possible (simpler code)
3. **Enable caching for Bedrock** (significant cost savings)
4. **Set appropriate max_tokens** to control costs
5. **Monitor usage** via provider dashboards
6. **Implement retry logic** for rate limits
7. **Test with cheaper models** before production deployment

---

## Future Enhancements

Planned features:

- [ ] Azure OpenAI support
- [ ] Anthropic Direct API support
- [ ] Model usage tracking and analytics
- [ ] Automatic failover between providers
- [ ] Cost estimation per request
- [ ] Provider-specific optimization strategies

---

## Support

For issues or questions:

1. Check this guide first
2. Review the [Strands Agent documentation](https://github.com/aws-samples/sample-strands-agent-with-agentcore)
3. Check provider-specific documentation:
   - [OpenAI API Docs](https://platform.openai.com/docs)
   - [Gemini API Docs](https://ai.google.dev/docs)
   - [AWS Bedrock Docs](https://docs.aws.amazon.com/bedrock)
4. Open an issue on GitHub

---

## Changelog

### v1.0.0 (2025-01-XX)

- ✨ Added OpenAI provider support
- ✨ Added Google Gemini provider support
- ✨ Auto-detection of provider from model_id
- ✨ Added `max_tokens` parameter
- 📝 Comprehensive multi-provider documentation
- 🔧 Updated API models and routes
- 🔧 Enhanced caching strategy for multi-provider support
