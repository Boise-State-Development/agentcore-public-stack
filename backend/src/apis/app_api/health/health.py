"""Health check endpoint"""

from fastapi import APIRouter

from apis.shared.build_info import health_fields

router = APIRouter(tags=["health"])

@router.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "agent-core",
        **health_fields(),
    }
