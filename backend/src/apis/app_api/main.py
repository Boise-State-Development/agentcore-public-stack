"""
Agent Core Service

Handles:
1. Strands Agent execution
2. Session management (agent pool)
3. Tool execution (MCP clients)
4. SSE streaming
"""

from pathlib import Path
from dotenv import load_dotenv
import os

# Load .env file from backend/src directory (parent of apis/)
env_path = Path(__file__).parent.parent.parent / '.env'
load_dotenv(dotenv_path=env_path, override=True)

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("=== AgentCore Public Stack API Starting ===")
    logger.info("Agent execution engine initialized")

    # Create output directories if they don't exist
    base_dir = Path(__file__).parent.parent
    output_dir = os.path.join(base_dir, "output")
    uploads_dir = os.path.join(base_dir, "uploads")
    generated_images_dir = os.path.join(base_dir, "generated_images")

    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(uploads_dir, exist_ok=True)
    os.makedirs(generated_images_dir, exist_ok=True)
    logger.info("Output directories ready")

    yield  # Application is running

    # Shutdown
    logger.info("=== Agent Core Service Shutting Down ===")
    # TODO: Cleanup agent pool, MCP clients, etc.

# Create FastAPI app with lifespan
app = FastAPI(
    title="Agent Core Public Stack - API",
    version=os.environ.get("APP_VERSION", "unknown"),
    description="Agent execution and tool orchestration service",
    lifespan=lifespan
)

# Add CORS middleware - origins from CDK-provided CORS_ORIGINS env var
_cors_origins = os.environ.get("CORS_ORIGINS", "").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_origins if o.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Bridge OAuth2CallbackUrl (sent by the frontend on connector calls) onto
# BedrockAgentCoreContext so IdentityClient can read it during consent flows.
# WorkloadAccessToken is absent here (no runtime gateway in front of app-api);
# IdentityClient mints one via AGENTCORE_RUNTIME_WORKLOAD_NAME.
from apis.shared.middleware.agentcore_context import AgentCoreContextMiddleware
app.add_middleware(AgentCoreContextMiddleware)
logger.info("Added AgentCore context middleware")


# Import routers
from apis.app_api.health import router as health_router
from apis.app_api.auth.routes import router as auth_router
from apis.app_api.auth.api_keys.routes import router as api_keys_router
from apis.app_api.sessions.routes import router as sessions_router
from apis.app_api.admin.routes import router as admin_router
from apis.app_api.models.routes import router as models_router
from apis.app_api.costs.routes import router as costs_router
from apis.app_api.chat.routes import router as chat_router
from apis.app_api.chat.converse_routes import router as converse_router
from apis.app_api.memory.routes import router as memory_router
from apis.app_api.tools.routes import router as tools_router
from apis.app_api.files.routes import router as files_router
from apis.app_api.assistants.routes import router as assistants_router
from apis.app_api.documents.routes import router as documents_router
from apis.app_api.users.routes import router as users_router
from apis.app_api.user_settings.routes import router as user_settings_router
from apis.app_api.connectors.routes import router as connectors_router
from apis.app_api.system.routes import router as system_router
from apis.app_api.shares.routes import conversations_share_router, shares_router, shared_view_router

# Include routers
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(api_keys_router)
app.include_router(sessions_router)
app.include_router(admin_router)
app.include_router(assistants_router)
app.include_router(documents_router)
app.include_router(users_router)
app.include_router(user_settings_router)
app.include_router(models_router)
app.include_router(costs_router)
app.include_router(chat_router)  # Application-specific chat endpoints
app.include_router(converse_router)  # Proxies to Inference API for cost accounting
app.include_router(memory_router)  # AgentCore Memory access endpoints
app.include_router(tools_router)  # Tool discovery and permissions
app.include_router(files_router)  # File upload via pre-signed URLs
app.include_router(connectors_router)  # User-facing connector catalog + consent flows
app.include_router(system_router)  # System status and first-boot endpoints
app.include_router(conversations_share_router)  # Share conversations endpoints
app.include_router(shares_router)  # Share management (update, revoke, export)
app.include_router(shared_view_router)  # Shared conversation read-only view

# Conditionally register fine-tuning routes
if os.environ.get("FINE_TUNING_ENABLED", "false").lower() == "true":
    from apis.app_api.fine_tuning.routes import router as fine_tuning_router
    app.include_router(fine_tuning_router)
    logger.info("Fine-tuning routes enabled")

# Mount static file directories for serving generated content
# These are created by tools (visualization, code interpreter, etc.)
# Use parent directory (src/) as base
base_dir = Path(__file__).parent.parent
output_dir = os.path.join(base_dir, "output")
uploads_dir = os.path.join(base_dir, "uploads")
generated_images_dir = os.path.join(base_dir, "generated_images")

if os.path.exists(output_dir):
    app.mount("/output", StaticFiles(directory=output_dir), name="output")
    logger.info(f"Mounted static files: /output -> {output_dir}")

if os.path.exists(uploads_dir):
    app.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")
    logger.info(f"Mounted static files: /uploads -> {uploads_dir}")

if os.path.exists(generated_images_dir):
    app.mount("/generated_images", StaticFiles(directory=generated_images_dir), name="generated_images")
    logger.info(f"Mounted static files: /generated_images -> {generated_images_dir}")

if __name__ == "__main__":
    import uvicorn
    # Watch the full backend/src tree so edits to shared modules outside
    # app_api/ (apis/shared/, agents/) trigger reload instead of defaulting
    # to cwd, which only sees this API's own files.
    src_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    # Run with full module path when executing directly
    uvicorn.run(
        "apis.app_api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_dirs=[src_root],
        log_level="info"
    )
