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
load_dotenv(dotenv_path=env_path)

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

# Lifespan event handler (replaces on_event)
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

    # Seed system roles for RBAC
    try:
        from apis.shared.rbac import ensure_system_roles
        await ensure_system_roles()
        logger.info("RBAC system roles initialized")
    except Exception as e:
        logger.warning(f"Failed to seed RBAC system roles: {e}")
        # Don't fail startup - roles can be seeded later

    yield  # Application is running

    # Shutdown
    logger.info("=== Agent Core Service Shutting Down ===")
    # TODO: Cleanup agent pool, MCP clients, etc.

# Create FastAPI app with lifespan
app = FastAPI(
    title="Agent Core Public Stack - API",
    version="2.0.0",
    description="Agent execution and tool orchestration service",
    lifespan=lifespan
)

# Add CORS middleware
# Backend (ECS Fargate + ALB) is on api.beta.boisestate.ai
# Frontend (CloudFront) is on beta.boisestate.ai - needs CORS for cross-origin requests
allowed_origins = []

if os.getenv('ENVIRONMENT', 'development') == 'development':
    allowed_origins.append("http://localhost:4200")  # Frontend dev server
    logger.info("CORS: Added local development origin")

# Always allow production frontend in non-local environments
if os.getenv('ENVIRONMENT') != 'development':
    allowed_origins.append("https://beta.boisestate.ai")
    logger.info("CORS: Added production frontend origin")

if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# Import routers
from apis.app_api.health import router as health_router
from apis.app_api.auth.routes import router as auth_router
from apis.app_api.sessions.routes import router as sessions_router
from apis.app_api.admin.routes import router as admin_router
from apis.app_api.models.routes import router as models_router
from apis.app_api.costs.routes import router as costs_router
from apis.app_api.chat.routes import router as chat_router
from apis.app_api.memory.routes import router as memory_router
from apis.app_api.tools.routes import router as tools_router
from apis.app_api.files.routes import router as files_router
from apis.app_api.assistants.routes import router as assistants_router
from apis.app_api.documents.routes import router as documents_router

# Include routers
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(sessions_router)
app.include_router(admin_router)
app.include_router(assistants_router)
app.include_router(documents_router)
app.include_router(models_router)
app.include_router(costs_router)
app.include_router(chat_router)  # Application-specific chat endpoints
app.include_router(memory_router)  # AgentCore Memory access endpoints
app.include_router(tools_router)  # Tool discovery and permissions
app.include_router(files_router)  # File upload via pre-signed URLs

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
    # Run with full module path when executing directly
    uvicorn.run(
        "apis.app_api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
