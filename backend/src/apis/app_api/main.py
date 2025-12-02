"""
Agent Core Service

Handles:
1. Strands Agent execution
2. Session management (agent pool)
3. Tool execution (MCP clients)
4. SSE streaming
"""

import sys
from pathlib import Path

# Add src directory to Python path (parent of apis/)
# This allows imports like: from apis.shared.auth import ...
src_path = Path(__file__).parent.parent.parent
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

# Load environment variables from .env file
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

# Add CORS middleware for local development
# In production (AWS), CloudFront handles routing so CORS is not needed
if os.getenv('ENVIRONMENT', 'development') == 'development':
    logger.info("Adding CORS middleware for local development")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:4200",  # Frontend dev server
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Import routers
from health.health import router as health_router
from auth.routes import router as auth_router
# Include routers
app.include_router(health_router)
app.include_router(auth_router)

# Mount static file directories for serving generated content
# These are created by tools (visualization, code interpreter, etc.)
# Use parent directory (agentcore/) as base, not src/
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
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
