"""Health check endpoint"""

from fastapi import APIRouter

router = APIRouter(tags=["health"])

@router.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "agent-core",
        "version": "2.0.0"
    }

@router.get("/ping")
async def ping():
    """Ping endpoint for AgentCore Runtime health checks"""
    return {"status": "ok"}
