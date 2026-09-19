"""
Configuration management for AgentCore

NOTE: the directories below are NOT the ones the FastAPI apps mount at
/output, /uploads and /generated_images. Those resolve to <src>/apis/* while
these resolve to <src>/* — they have disagreed by one directory level since
they were written, which means the download_url built in
agents/main_agent/streaming/tool_result_processor.py points at a mount that
cannot serve it. Left as-is here deliberately: see
apis/shared/runtime_paths.py for why unifying them is a security decision
rather than a cleanup.
"""
from pathlib import Path

from apis.shared.runtime_paths import get_agent_workspace_dir


class Config:
    """Configuration class for AgentCore paths and settings"""

    @staticmethod
    def get_base_dir() -> Path:
        """Get the base directory the agents package writes into.

        Defaults to backend/src/ (the historical location); RUNTIME_DATA_DIR
        relocates it out of the source tree so the image can ship the code
        read-only.
        """
        return get_agent_workspace_dir()

    @staticmethod
    def get_output_dir() -> Path:
        """Get the output directory path (agentcore/output)"""
        output_dir = Config.get_base_dir() / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    @staticmethod
    def get_session_output_dir(session_id: str) -> Path:
        """Get the session-specific output directory (agentcore/output/session_id)"""
        session_dir = Config.get_output_dir() / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir

    @staticmethod
    def get_uploads_dir() -> Path:
        """Get the uploads directory path (agentcore/uploads)"""
        uploads_dir = Config.get_base_dir() / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        return uploads_dir

    @staticmethod
    def get_generated_images_dir() -> Path:
        """Get the generated images directory path (agentcore/generated_images)"""
        images_dir = Config.get_base_dir() / "generated_images"
        images_dir.mkdir(parents=True, exist_ok=True)
        return images_dir
