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

# Load environment variables from .env file only if not already set
# In production (Docker/AWS), env vars are set directly in the container
# In local development, we fall back to .env file
if not os.getenv('AWS_REGION') or not os.getenv('AWS_DEFAULT_REGION'):
    env_path = Path(__file__).parent.parent.parent / '.env'
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
        print(f"Loaded environment variables from: {env_path}")
    else:
        print(f"Warning: .env file not found at {env_path}")
else:
    print("Using environment variables from container runtime")

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
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
    
    # Log configuration
    logger.info(f"Environment: {os.getenv('ENVIRONMENT', 'development')}")
    logger.info(f"Log Level: {os.getenv('LOG_LEVEL', 'INFO')}")
    logger.info(f"AWS Region: {os.getenv('AWS_REGION', 'not set')}")
    
    # Log authentication settings
    auth_enabled = os.getenv('ENABLE_AUTHENTICATION', 'false')
    logger.info(f"Authentication: {'enabled' if auth_enabled.lower() == 'true' else 'disabled'}")
    
    # Log AgentCore Runtime environment variables
    memory_arn = os.getenv('MEMORY_ARN')
    memory_id = os.getenv('AGENTCORE_MEMORY_ID')
    browser_id = os.getenv('BROWSER_ID')
    code_interpreter_id = os.getenv('CODE_INTERPRETER_ID')

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
    api_url = os.getenv('API_URL')
    frontend_url = os.getenv('FRONTEND_URL')
    if api_url:
        logger.info(f"API URL: {api_url}")
    if frontend_url:
        logger.info(f"Frontend URL: {frontend_url}")
    
    # Log CORS configuration
    cors_origins = os.getenv('CORS_ORIGINS')
    if cors_origins:
        logger.info(f"CORS Origins: {cors_origins}")
    
    # Log API key availability (without exposing values)
    tavily_key = os.getenv('TAVILY_API_KEY')
    nova_key = os.getenv('NOVA_ACT_API_KEY')
    if tavily_key:
        logger.info(f"Tavily API Key: configured ({tavily_key[:10]}...)")
    else:
        logger.info("Tavily API Key: not configured")
    if nova_key:
        logger.info(f"Nova Act API Key: configured ({nova_key[:10]}...)")
    else:
        logger.info("Nova Act API Key: not configured")

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
    version="2.0.0",
    description="AgentCore Runtime standard endpoints (ping, invocations) for AWS Bedrock AgentCore Runtime",
    lifespan=lifespan
)

# Add GZip compression middleware for SSE streams
# Compresses responses over 1KB, reducing bandwidth by 50-70%
app.add_middleware(
    GZipMiddleware,
    minimum_size=1000,  # Only compress responses > 1KB
    compresslevel=6  # Balance between speed and compression ratio (1-9)
)
logger.info("Added GZip middleware for response compression")

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
#from health.health import router as health_router
from .chat.routes import router as agentcore_router
# Include routers
#app.include_router(health_router)
app.include_router(agentcore_router)  # AgentCore Runtime endpoints: /ping, /invocations

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
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8001,
        reload=True,
        log_level="info"
    )
