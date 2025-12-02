# AgentCore Backend

This backend uses a unified dependency management approach with a single `pyproject.toml` file.

## Installation

### For App API (Authentication Service)

The App API only needs core dependencies (FastAPI, auth utilities):

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -e "."
```

### For Inference API (Agent Execution Service)

The Inference API needs core dependencies + AgentCore-specific packages:

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -e ".[agentcore]"
```

### For Development (All Dependencies + Dev Tools)

Install everything including pytest, black, ruff, mypy:

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -e ".[agentcore,dev]"
```

## Project Structure

```
backend/
├── pyproject.toml          # Single source of truth for all dependencies
├── src/
│   ├── agentcore/          # Agent orchestration & tools
│   └── apis/
│       ├── shared/         # Shared auth utilities
│       ├── app_api/        # Authentication API (port 8000)
│       └── inference_api/  # Agent execution API (port 8001)
```

## Running APIs

### App API (Authentication)
```bash
cd backend
source venv/bin/activate
cd src/apis/app_api
python main.py
# Runs on http://localhost:8000
```

### Inference API (Agent Execution)
```bash
cd backend
source venv/bin/activate
cd src/apis/inference_api
python main.py
# Runs on http://localhost:8001
```

## Module Imports

All modules are properly packaged and can be imported directly:

```python
# Import shared auth utilities
from apis.shared.auth import get_current_user, User, StateStore

# Import agentcore modules
from agentcore.agent.agent import ChatbotAgent
from agentcore.local_tools.weather import get_weather
```

## Dependencies Overview

### Core Dependencies (all APIs)
- FastAPI 0.116.1
- Uvicorn 0.35.0
- Boto3 (AWS SDK)
- python-dotenv
- httpx
- PyJWT (authentication)

### AgentCore Dependencies (inference_api only)
- strands-agents 1.14.0
- strands-agents-tools 0.2.3
- bedrock-agentcore
- nova-act 2.3.18.0
- aws-opentelemetry-distro
- ddgs (DuckDuckGo search)

### Development Dependencies (optional)
- pytest
- pytest-asyncio
- black (code formatter)
- ruff (linter)
- mypy (type checker)

## Migration Notes

This structure replaces the previous approach that used:
- Multiple `pyproject.toml` files (one per package)
- Multiple `setup.py` files
- Multiple `requirements.txt` files with `-e` editable installs
- Manual `sys.path` manipulation

**Benefits:**
- Single source of truth for dependencies
- Consistent import patterns across all code
- Proper package resolution
- Better IDE support
- Easier dependency updates
- Works in containers without path hacks
