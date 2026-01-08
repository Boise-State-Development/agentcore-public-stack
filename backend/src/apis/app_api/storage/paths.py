"""Centralized path utilities for session and message storage

This module provides a single source of truth for all file paths used in
local storage. This prevents path construction bugs and makes it easy to
change storage locations via environment variables.
"""

import os
from pathlib import Path


def get_sessions_root() -> Path:
    """
    Get the root directory for session storage

    Returns:
        Path: Root directory for all sessions

    Environment Variables:
        SESSIONS_DIR: Override default sessions directory location
    """
    sessions_dir = os.environ.get('SESSIONS_DIR')
    if sessions_dir:
        return Path(sessions_dir)

    # Default: backend/src/sessions
    # Navigate from this file: storage/paths.py -> app_api -> apis -> src -> sessions
    return Path(__file__).parent.parent.parent.parent / "sessions"


def get_session_dir(session_id: str) -> Path:
    """
    Get the directory for a specific session

    Args:
        session_id: Session identifier

    Returns:
        Path: Directory for the session

    Example:
        sessions/session_abc123/
    """
    return get_sessions_root() / f"session_{session_id}"


def get_messages_dir(session_id: str) -> Path:
    """
    Get the directory containing all messages for a session

    Args:
        session_id: Session identifier

    Returns:
        Path: Directory containing message files

    Example:
        sessions/session_abc123/agents/agent_default/messages/
    """
    return get_session_dir(session_id) / "agents" / "agent_default" / "messages"


def get_message_path(session_id: str, message_id: int) -> Path:
    """
    Get the file path for a specific message

    Args:
        session_id: Session identifier
        message_id: Message number (1, 2, 3, ...)

    Returns:
        Path: Full path to the message file

    Example:
        sessions/session_abc123/agents/agent_default/messages/message_1.json
    """
    return get_messages_dir(session_id) / f"message_{message_id}.json"


def get_session_metadata_path(session_id: str) -> Path:
    """
    Get the file path for session metadata

    Args:
        session_id: Session identifier

    Returns:
        Path: Full path to the session metadata file

    Example:
        sessions/session_abc123/session-metadata.json

    Note:
        This file contains conversation-level metadata like:
        - title, status, createdAt, lastMessageAt
        - messageCount, preferences, tags

        Stored separately from session.json (used by Strands library)
        to avoid conflicts when running in local mode.
    """
    return get_session_dir(session_id) / "session-metadata.json"


def get_message_metadata_path(session_id: str) -> Path:
    """
    Get the file path for message metadata index

    Args:
        session_id: Session identifier

    Returns:
        Path: Full path to the message metadata index file

    Example:
        sessions/session_abc123/message-metadata.json

    Note:
        This file contains metadata for all messages in a session:
        - Token usage (input, output, cache)
        - Latency metrics (TTFT, end-to-end)
        - Model information
        - Attribution data

        Stored separately from message files to better simulate
        the cloud architecture where metadata is in a separate
        DynamoDB table.

        File structure:
        {
          "0": { "latency": {...}, "tokenUsage": {...}, ... },
          "1": { "latency": {...}, "tokenUsage": {...}, ... },
          ...
        }
    """
    return get_session_dir(session_id) / "message-metadata.json"


def get_assistants_root() -> Path:
    """
    Get the root directory for assistant storage

    Returns:
        Path: Root directory for all assistants

    Environment Variables:
        ASSISTANTS_DIR: Override default assistants directory location

    Example:
        backend/src/assistants/
    """
    assistants_dir = os.environ.get('ASSISTANTS_DIR')
    if assistants_dir:
        return Path(assistants_dir)

    # Default: backend/src/assistants
    # Navigate from this file: storage/paths.py -> app_api -> apis -> src -> assistants
    return Path(__file__).parent.parent.parent.parent / "assistants"


def get_assistant_path(assistant_id: str) -> Path:
    """
    Get the file path for a specific assistant

    Args:
        assistant_id: Assistant identifier

    Returns:
        Path: Full path to the assistant file

    Example:
        backend/src/assistants/assistant_AST-1234.json
    """
    return get_assistants_root() / f"assistant_{assistant_id}.json"
