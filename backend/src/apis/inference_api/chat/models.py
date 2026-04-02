"""Chat feature models

Contains Pydantic models for chat API requests and responses.
"""

import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class FileContent(BaseModel):
    """File content (base64 encoded)"""

    filename: str
    content_type: str
    bytes: str  # Base64 encoded


class InvocationRequest(BaseModel):
    """Input for /invocations endpoint with multi-provider support"""

    session_id: str
    message: str
    model_id: Optional[str] = None
    temperature: Optional[float] = None
    system_prompt: Optional[str] = None
    caching_enabled: Optional[bool] = None
    enabled_tools: Optional[List[str]] = None  # User-specific tool preferences
    files: Optional[List[FileContent]] = None  # Direct file content (base64-encoded)
    file_upload_ids: Optional[List[str]] = None  # Upload IDs to resolve from S3
    provider: Optional[str] = None  # LLM provider: "bedrock", "openai", or "gemini"
    max_tokens: Optional[int] = None  # Maximum tokens to generate
    # NOTE: Field name is 'rag_assistant_id' to avoid collision with AWS Bedrock
    # AgentCore Runtime's internal 'assistant_id' field handling.
    # AgentCore Runtime returns 424 when it sees a non-empty 'assistant_id' field,
    # likely trying to resolve it as an AWS Bedrock Agent ID.
    rag_assistant_id: Optional[str] = None


class InvocationResponse(BaseModel):
    """AgentCore Runtime standard response format"""

    output: Dict[str, Any]


class ChatRequest(BaseModel):
    """Chat request from client"""

    session_id: str
    message: str
    files: Optional[List[FileContent]] = None  # Direct file content (base64-encoded)
    file_upload_ids: Optional[List[str]] = None  # Upload IDs to resolve from S3
    enabled_tools: Optional[List[str]] = None  # User-specific tool preferences (tool IDs)
    assistant_id: Optional[str] = None  # Assistant ID for RAG-enabled chat


class ChatEvent(BaseModel):
    """SSE event sent to client"""

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


class GenerateTitleRequest(BaseModel):
    """Request to generate a conversation title"""

    session_id: str
    input: str  # Truncated user message (up to ~500 tokens)


class GenerateTitleResponse(BaseModel):
    """Response with generated conversation title"""

    title: str
    session_id: str


# ---------------------------------------------------------------------------
# API Converse models (direct Bedrock Converse API via API key auth)
# ---------------------------------------------------------------------------


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
    temperature: Optional[float] = 0.7
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
