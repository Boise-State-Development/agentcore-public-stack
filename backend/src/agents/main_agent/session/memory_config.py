"""
Memory storage configuration for AgentCore

This module provides configuration for AgentCore Memory storage (DynamoDB via AWS Bedrock).
"""
import os
import logging
from dataclasses import dataclass

from agents.main_agent.config.constants import EnvVars, Defaults

logger = logging.getLogger(__name__)


@dataclass
class MemoryStorageConfig:
    """Configuration for AgentCore Memory storage"""
    memory_id: str
    region: str

    @property
    def is_cloud_mode(self) -> bool:
        """Always True — only DynamoDB/AgentCore storage is supported"""
        return True


def load_memory_config() -> MemoryStorageConfig:
    """
    Load memory storage configuration from environment variables.

    Environment Variables:
        AGENTCORE_MEMORY_ID: Memory ID (REQUIRED)
        AWS_REGION: AWS region (default: "us-west-2")

    Returns:
        MemoryStorageConfig: Validated configuration

    Raises:
        RuntimeError: If AGENTCORE_MEMORY_ID is not set
    """
    memory_id = os.environ.get(EnvVars.MEMORY_ID) or None
    region = os.environ.get(EnvVars.AWS_REGION, Defaults.AWS_REGION)

    if not memory_id:
        raise RuntimeError(
            "AGENTCORE_MEMORY_ID environment variable is required. "
            "Set it to your AWS Bedrock AgentCore Memory ID."
        )

    config = MemoryStorageConfig(memory_id=memory_id, region=region)

    logger.info(f"🚀 AgentCore Memory Config: AWS Bedrock AgentCore Memory")
    logger.info(f"   • Memory ID: {config.memory_id}")
    logger.info(f"   • Region: {config.region}")
    logger.info(f"   • Storage: AWS-managed DynamoDB")

    return config
