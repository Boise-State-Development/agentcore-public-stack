"""Chat router - handles agent execution and SSE streaming

This module maintains backward compatibility by re-exporting the router
from the refactored routes module. The code has been organized into:
- models.py: Pydantic models for requests/responses
- service.py: Business logic for agent creation
- routes.py: Route handlers and endpoint definitions
"""

# Re-export router for backward compatibility
from .routes import router

__all__ = ["router"]
