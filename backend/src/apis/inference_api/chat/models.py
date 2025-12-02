"""Chat feature models

Contains Pydantic models for chat API requests and responses.
"""

from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import json


class InvocationRequest(BaseModel):
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


# class InvocationRequest(BaseModel):
#     """AgentCore Runtime standard request format"""
#     input: InvocationInput


class InvocationResponse(BaseModel):
    """AgentCore Runtime standard response format"""
    output: Dict[str, Any]

class FileContent(BaseModel):
    """File content (base64 encoded)"""
    filename: str
    content_type: str
    bytes: str  # Base64 encoded

class ChatRequest(BaseModel):
    """Chat request from BFF"""
    session_id: str
    message: str
    files: Optional[List[FileContent]] = None
    enabled_tools: Optional[List[str]] = None  # User-specific tool preferences (tool IDs)

class ChatEvent(BaseModel):
    """SSE event sent to BFF"""
    type: str  # "text" | "tool_use" | "tool_result" | "error" | "complete"
    content: str
    metadata: Optional[Dict[str, Any]] = None

    def to_json(self) -> str:
        """Convert to JSON string"""
        return json.dumps(self.model_dump(), ensure_ascii=False)

class SessionInfo(BaseModel):
    """Session information"""
    session_id: str
    message_count: int
    created_at: str
    updated_at: str
