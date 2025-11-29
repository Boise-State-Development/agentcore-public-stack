"""Chat feature models

Contains Pydantic models for chat API requests and responses.
"""

from pydantic import BaseModel
from typing import Dict, Any, Optional, List

from models.schemas import FileContent


class InvocationInput(BaseModel):
    """Input for /invocations endpoint"""
    user_id: str
    session_id: str
    message: str
    model_id: Optional[str] = None
    temperature: Optional[float] = None
    system_prompt: Optional[str] = None
    caching_enabled: Optional[bool] = None
    enabled_tools: Optional[List[str]] = None  # User-specific tool preferences
    files: Optional[List[FileContent]] = None  # Multimodal file attachments


class InvocationRequest(BaseModel):
    """AgentCore Runtime standard request format"""
    input: InvocationInput


class InvocationResponse(BaseModel):
    """AgentCore Runtime standard response format"""
    output: Dict[str, Any]

