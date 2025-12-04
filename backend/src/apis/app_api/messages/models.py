"""Messages API models"""

from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class MessageContent(BaseModel):
    """Individual content block in a message"""
    type: str = Field(..., description="Content type (text, toolUse, toolResult, etc.)")
    text: Optional[str] = Field(None, description="Text content")
    # Add other fields as needed for different content types
    tool_use: Optional[Dict[str, Any]] = Field(None, alias="toolUse")
    tool_result: Optional[Dict[str, Any]] = Field(None, alias="toolResult")
    image: Optional[Dict[str, Any]] = Field(None)
    document: Optional[Dict[str, Any]] = Field(None)

    class Config:
        populate_by_name = True


class Message(BaseModel):
    """Individual message in a conversation"""
    role: str = Field(..., description="Message role (user, assistant)")
    content: List[MessageContent] = Field(..., description="Message content blocks")
    timestamp: Optional[str] = Field(None, description="Message timestamp")

    class Config:
        populate_by_name = True


class GetMessagesResponse(BaseModel):
    """Response for get messages endpoint"""
    session_id: str = Field(..., description="Session identifier")
    user_id: str = Field(..., description="User identifier")
    messages: List[Message] = Field(..., description="List of messages in the session")
    total_count: int = Field(..., description="Total number of messages")

    class Config:
        populate_by_name = True
