"""Request/response models for the API-key authenticated converse endpoint.

These DTOs are owned by app-api because the ``/chat/api-converse`` handler
lives here (see ``converse_routes.py``). External API-key consumers post to
this shape and receive ``ConverseResponse`` for non-streaming requests.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class ConverseMessage(BaseModel):
    """A single message in the conversation."""

    role: str  # "user" or "assistant"
    content: str


class ConverseRequest(BaseModel):
    """Request model for /chat/api-converse endpoint.

    Supports both single-shot and multi-turn conversations.
    """

    model_id: str  # Bedrock model ID (e.g. "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    messages: List[ConverseMessage]
    system_prompt: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = 4096
    stream: bool = False  # Whether to stream the response via SSE
    top_p: Optional[float] = None


class ConverseResponse(BaseModel):
    """Non-streaming response from /chat/api-converse."""

    role: str = "assistant"
    content: str
    model_id: str
    usage: Optional[Dict[str, Any]] = None
    stop_reason: Optional[str] = None
    reasoning: Optional[str] = None  # Populated for reasoning models
