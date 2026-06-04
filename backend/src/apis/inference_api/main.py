"""
AgentCore Runtime API

Handles AgentCore Runtime standard endpoints:
1. GET /ping - Health check (required by AgentCore Runtime)
2. POST /invocations - Agent invocation endpoint (required by AgentCore Runtime)

This API is designed to comply with AWS Bedrock AgentCore Runtime requirements.
All endpoints are at root level as required by the AgentCore Runtime specification.
"""

from pathlib import Path
from dotenv import load_dotenv
import os

import logging as _logging

# Load .env file from backend/src directory (parent of apis/)
# override=True so .env values win over shell env vars during local development.
# In production (containers), there is no .env file, so container-injected env vars are used directly.
env_path = Path(__file__).parent.parent.parent / '.env'
_startup_logger = _logging.getLogger(__name__)
if env_path.exists():
    load_dotenv(dotenv_path=env_path, override=True)
    _startup_logger.info("Loaded environment variables from: %s", env_path)
else:
    _startup_logger.warning(".env file not found at %s", env_path)

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from contextlib import asynccontextmanager
import logging

from apis.shared.middleware.agentcore_context import AgentCoreContextMiddleware

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
    
    # Log configuration
    logger.info(f"Environment: {os.getenv('ENVIRONMENT', 'development')}")
    logger.info(f"Log Level: {os.getenv('LOG_LEVEL', 'INFO')}")
    logger.info(f"AWS Region: {os.getenv('AWS_REGION', 'not set')}")
    
    # Log AgentCore Runtime environment variables
    memory_arn = os.getenv('MEMORY_ARN')
    memory_id = os.getenv('AGENTCORE_MEMORY_ID')
    browser_id = os.getenv('BROWSER_ID')
    code_interpreter_id = os.getenv('AGENTCORE_CODE_INTERPRETER_ID')

    if memory_arn:
        logger.info(f"AgentCore Memory ARN: {memory_arn}")
    if memory_id:
        logger.info(f"AgentCore Memory ID: {memory_id}")
    if browser_id:
        logger.info(f"AgentCore Browser ID: {browser_id}")
    if code_interpreter_id:
        logger.info(f"AgentCore Code Interpreter ID: {code_interpreter_id}")
    
    # Log storage directories
    upload_dir = os.getenv('UPLOAD_DIR', 'uploads')
    output_dir_name = os.getenv('OUTPUT_DIR', 'output')
    generated_images_dir_name = os.getenv('GENERATED_IMAGES_DIR', 'generated_images')
    logger.info(f"Storage directories - Upload: {upload_dir}, Output: {output_dir_name}, Images: {generated_images_dir_name}")
    
    # Log API URLs (if configured)
    frontend_url = os.getenv('FRONTEND_URL')
    if frontend_url:
        logger.info(f"Frontend URL: {frontend_url}")
    
    # Log CORS configuration
    cors_origins = os.getenv('CORS_ORIGINS')
    if cors_origins:
        logger.info(f"CORS Origins: {cors_origins}")
    
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
    logger.info("=== Inference API Shutting Down ===")
    # TODO: Cleanup agent pool, MCP clients, etc.

# Create FastAPI app with lifespan
app = FastAPI(
    title="AgentCore Runtime API",
    version=os.environ.get("APP_VERSION", "unknown"),
    description="AgentCore Runtime standard endpoints (ping, invocations) for AWS Bedrock AgentCore Runtime",
    lifespan=lifespan
)

# Centralized exception handlers: AWS ClientError responses are mapped to
# generic 400/502 bodies (logged server-side) so AWS error messages, internal
# parameter names, and reflected user input never leak into HTTP responses.
from apis.shared.security import register_aws_client_error_handler
register_aws_client_error_handler(app)
logger.info("Registered AWS ClientError handler")

# Add GZip compression middleware for SSE streams
# Compresses responses over 1KB, reducing bandwidth by 50-70%
app.add_middleware(
    GZipMiddleware,
    minimum_size=1000,  # Only compress responses > 1KB
    compresslevel=6  # Balance between speed and compression ratio (1-9)
)
logger.info("Added GZip middleware for response compression")

# Bridge AgentCore Runtime headers (WorkloadAccessToken, OAuth2CallbackUrl,
# session ID) into BedrockAgentCoreContext so downstream code can look up
# per-user OAuth tokens via AgentCore Identity.
app.add_middleware(AgentCoreContextMiddleware)
logger.info("Added AgentCore Runtime context middleware")

# Add CORS middleware - origins from CDK-provided CORS_ORIGINS env var
_cors_origins = os.environ.get("CORS_ORIGINS", "").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_origins if o.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Import routers
#from health.health import router as health_router
from apis.inference_api.chat.routes import router as agentcore_router
from apis.inference_api.chat.converse_routes import router as converse_router
from apis.inference_api.chat.voice_routes import router as voice_router
# Include routers
#app.include_router(health_router)
app.include_router(agentcore_router)  # AgentCore Runtime endpoints: /ping, /invocations
app.include_router(converse_router)  # API-key authenticated converse endpoint
app.include_router(voice_router)  # WebSocket voice streaming endpoint
# Connector consent flows live on app-api now: the AgentCore Runtime data plane
# only proxies /invocations and /ping, so user-facing /connectors/* paths can't
# be reached through this service. See apis/app_api/connectors/routes.py.

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
    # Watch the full backend/src tree so edits to shared modules (agents/,
    # apis/shared/) trigger reload. Without this uvicorn defaults to cwd,
    # which hides changes outside inference_api/.
    src_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    uvicorn.run(
        "apis.inference_api.main:app",
        host="0.0.0.0",
        port=8001,
        reload=True,
        reload_dirs=[src_root],
        log_level="info"
    )
