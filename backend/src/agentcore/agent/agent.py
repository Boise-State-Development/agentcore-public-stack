"""
ChatbotAgent for Agent Core
- Uses Strands Agent with local tools
- Session management with AgentCore Memory
- User preference and conversation persistence
- Streaming with event processing
"""

import logging
import os
from typing import AsyncGenerator, Dict, Any, List, Optional
from pathlib import Path
from datetime import datetime
from strands import Agent
from strands.models import BedrockModel
from strands.session.file_session_manager import FileSessionManager
from strands.tools.executors import SequentialToolExecutor
from api.utils.event_processor import StreamEventProcessor
from agentcore.agent.hooks import StopHook, ConversationCachingHook

logger = logging.getLogger(__name__)

# Import timezone support (zoneinfo for Python 3.9+, fallback to pytz)
try:
    from zoneinfo import ZoneInfo
    TIMEZONE_AVAILABLE = True
except ImportError:
    try:
        import pytz
        TIMEZONE_AVAILABLE = True
    except ImportError:
        TIMEZONE_AVAILABLE = False
        logger.warning("Neither zoneinfo nor pytz available - date will use UTC")

# AgentCore Memory integration (optional, only for cloud deployment)
try:
    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig, RetrievalConfig
    from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
    AGENTCORE_MEMORY_AVAILABLE = True
except ImportError:
    AGENTCORE_MEMORY_AVAILABLE = False

# Import Strands built-in tools
from strands_tools.calculator import calculator

# Import local tools module (general-purpose, agent-core integrated)
from agentcore import local_tools

# Import built-in tools module (AWS Bedrock-powered tools)
from agentcore import builtin_tools

# Import Gateway MCP client
from agentcore.agent.gateway_mcp_client import get_gateway_client_if_enabled


def get_current_date_pacific() -> str:
    """Get current date and hour in US Pacific timezone (America/Los_Angeles)"""
    try:
        if TIMEZONE_AVAILABLE:
            try:
                # Try zoneinfo first (Python 3.9+)
                from zoneinfo import ZoneInfo
                pacific_tz = ZoneInfo("America/Los_Angeles")
                now = datetime.now(pacific_tz)
                # Get timezone abbreviation (PST/PDT)
                tz_abbr = now.strftime("%Z")
            except (ImportError, NameError):
                # Fallback to pytz
                import pytz
                pacific_tz = pytz.timezone("America/Los_Angeles")
                now = datetime.now(pacific_tz)
                # Get timezone abbreviation (PST/PDT)
                tz_abbr = now.strftime("%Z")

            return now.strftime(f"%Y-%m-%d (%A) %H:00 {tz_abbr}")
        else:
            # Fallback to UTC if no timezone library available
            now = datetime.utcnow()
            return now.strftime("%Y-%m-%d (%A) %H:00 UTC")
    except Exception as e:
        logger.warning(f"Failed to get Pacific time: {e}, using UTC")
        now = datetime.utcnow()
        return now.strftime("%Y-%m-%d (%A) %H:00 UTC")


# Global stream processor instance
_global_stream_processor = None

def get_global_stream_processor():
    """Get the global stream processor instance"""
    return _global_stream_processor


# Tool ID to tool object mapping
# Start with Strands built-in tools (externally managed)
TOOL_REGISTRY = {
    "calculator": calculator,
}

# Dynamically load all local tools from local_tools.__all__
# This ensures we only need to maintain the list in one place (__init__.py)
for tool_name in local_tools.__all__:
    tool_obj = getattr(local_tools, tool_name)
    TOOL_REGISTRY[tool_name] = tool_obj
    logger.info(f"Registered local tool: {tool_name}")

# Dynamically load all builtin tools from builtin_tools.__all__
# This ensures we only need to maintain the list in one place (__init__.py)
for tool_name in builtin_tools.__all__:
    tool_obj = getattr(builtin_tools, tool_name)
    TOOL_REGISTRY[tool_name] = tool_obj
    logger.info(f"Registered builtin tool: {tool_name}")


class ChatbotAgent:
    """Main ChatbotAgent for Agent Core with user-specific configuration"""

    def __init__(
        self,
        session_id: str,
        user_id: Optional[str] = None,
        enabled_tools: Optional[List[str]] = None,
        model_id: Optional[str] = None,
        temperature: Optional[float] = None,
        system_prompt: Optional[str] = None,
        caching_enabled: Optional[bool] = None
    ):
        """
        Initialize agent with specific configuration and AgentCore Memory

        Args:
            session_id: Session identifier for message persistence
            user_id: User identifier for cross-session preferences (defaults to session_id)
            enabled_tools: List of tool IDs to enable. If None, all tools are enabled.
            model_id: Bedrock model ID to use
            temperature: Model temperature (0.0 - 1.0)
            system_prompt: System prompt text
            caching_enabled: Whether to enable prompt caching
        """
        global _global_stream_processor
        self.stream_processor = StreamEventProcessor()
        _global_stream_processor = self.stream_processor
        self.agent = None
        self.session_id = session_id
        self.user_id = user_id or session_id  # Use session_id as user_id if not provided
        self.enabled_tools = enabled_tools
        self.gateway_client = None  # Store Gateway MCP client for lifecycle management

        # Store model configuration
        self.model_id = model_id or "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        self.temperature = temperature if temperature is not None else 0.7

        # Use provided system prompt or default prompt
        # Note: Date is already added by BFF (chatbot-app/frontend/src/app/api/stream/chat/route.ts)
        # If no system_prompt provided, use default with date
        if system_prompt:
            # BFF already added date, use as-is
            self.system_prompt = system_prompt
            logger.info("Using system prompt from BFF (with date already included)")
        else:
            # Fallback: Add date here (for direct AgentCore usage without BFF)
            base_system_prompt = """You are an intelligent AI agent with dynamic tool capabilities. You can perform various tasks based on the combination of tools available to you.

Key guidelines:
- You can ONLY use tools that are explicitly provided to you in each conversation
- Available tools may change throughout the conversation based on user preferences
- When multiple tools are available, select and use the most appropriate combination in the optimal order to fulfill the user's request
- Break down complex tasks into steps and use multiple tools sequentially or in parallel as needed
- Always explain your reasoning when using tools
- If you don't have the right tool for a task, clearly inform the user about the limitation

Browser Automation Best Practices:
- **ALWAYS prefer direct URLs with search parameters** over multi-step form filling
- Examples:
  ✓ Use: "https://www.google.com/search?q=AI+news" (1 step)
  ✗ Avoid: Navigate to google.com → find search box → type → click search (3-4 steps)
  ✓ Use: "https://www.amazon.com/s?k=wireless+headphones"
  ✗ Avoid: Navigate to amazon.com → find search → type → submit
- This reduces steps, improves reliability, and bypasses CAPTCHA challenges more effectively
- Only use browser_act for interactions when direct URL navigation is not possible

Your goal is to be helpful, accurate, and efficient in completing user requests using the available tools."""
            current_date = get_current_date_pacific()
            self.system_prompt = f"{base_system_prompt}\n\nCurrent date: {current_date}"
            logger.info(f"Using default system prompt with current date: {current_date}")

        self.caching_enabled = caching_enabled if caching_enabled is not None else True

        # Session Manager Selection: AgentCore Memory (cloud) vs File-based (local)
        memory_id = os.environ.get('MEMORY_ID')
        aws_region = os.environ.get('AWS_REGION', 'us-west-2')

        if memory_id and AGENTCORE_MEMORY_AVAILABLE:
            # Cloud deployment: Use AgentCore Memory
            logger.info(f"🚀 Cloud mode: Using AgentCore Memory (memory_id={memory_id})")

            # Configure AgentCore Memory with user preferences and facts retrieval
            agentcore_memory_config = AgentCoreMemoryConfig(
                memory_id=memory_id,
                session_id=session_id,
                actor_id=self.user_id,
                enable_prompt_caching=caching_enabled if caching_enabled is not None else True,
                retrieval_config={
                    # User-specific preferences (e.g., coding style, language preference)
                    f"/preferences/{self.user_id}": RetrievalConfig(top_k=5, relevance_score=0.7),
                    # User-specific facts (e.g., learned information)
                    f"/facts/{self.user_id}": RetrievalConfig(top_k=10, relevance_score=0.3),
                }
            )

            # Create Turn-based Session Manager (reduces API calls by 75%)
            from agentcore.agent.turn_based_session_manager import TurnBasedSessionManager

            self.session_manager = TurnBasedSessionManager(
                agentcore_memory_config=agentcore_memory_config,
                region_name=aws_region
            )

            logger.info(f"✅ AgentCore Memory initialized: user_id={self.user_id}")
        else:
            # Local development: Use file-based session manager with buffering wrapper
            logger.info(f"💻 Local mode: Using FileSessionManager with buffering")
            sessions_dir = Path(__file__).parent.parent.parent / "sessions"
            sessions_dir.mkdir(exist_ok=True)

            base_file_manager = FileSessionManager(
                session_id=session_id,
                storage_dir=str(sessions_dir)
            )

            # Wrap with local buffering manager for stop functionality
            from agentcore.agent.local_session_buffer import LocalSessionBuffer
            self.session_manager = LocalSessionBuffer(
                base_manager=base_file_manager,
                session_id=session_id
            )

            logger.info(f"✅ FileSessionManager with buffering initialized: {sessions_dir}")

        self.create_agent()

    def get_model_config(self) -> Dict[str, Any]:
        """Return model configuration"""
        return {
            "model_id": self.model_id,
            "temperature": self.temperature,
            "system_prompts": [self.system_prompt],
            "caching_enabled": self.caching_enabled
        }


    def get_filtered_tools(self) -> List:
        """
        Get tools filtered by enabled_tools list.
        Includes both local tools and Gateway MCP client (Managed Integration).
        """
        # If no enabled_tools specified (None or empty), return NO tools
        if self.enabled_tools is None or len(self.enabled_tools) == 0:
            logger.info("No enabled_tools specified - Agent will run WITHOUT any tools")
            return []

        # Filter local tools based on enabled_tools
        filtered_tools = []
        gateway_tool_ids = []

        for tool_id in self.enabled_tools:
            if tool_id in TOOL_REGISTRY:
                # Local tool
                filtered_tools.append(TOOL_REGISTRY[tool_id])
            elif tool_id.startswith("gateway_"):
                # Gateway MCP tool - collect for filtering
                gateway_tool_ids.append(tool_id)
            else:
                logger.warning(f"Tool '{tool_id}' not found in registry, skipping")

        logger.info(f"Local tools enabled: {len(filtered_tools)}")
        logger.info(f"Gateway tools enabled: {len(gateway_tool_ids)}")

        # Add Gateway MCP client if Gateway tools are enabled
        # Store as instance variable to keep session alive during Agent lifecycle
        if gateway_tool_ids:
            self.gateway_client = get_gateway_client_if_enabled(enabled_tool_ids=gateway_tool_ids)
            if self.gateway_client:
                # Using Managed Integration (Strands 1.16+) - pass MCPClient directly to Agent
                # Agent will automatically manage lifecycle and filter tools
                filtered_tools.append(self.gateway_client)
                logger.info(f"✅ Gateway MCP client added (Managed Integration with Strands 1.16+)")
                logger.info(f"   Enabled Gateway tool IDs: {gateway_tool_ids}")
            else:
                logger.warning("⚠️  Gateway MCP client not available")

        logger.info(f"Total enabled tools: {len(filtered_tools)} (local + gateway client)")
        return filtered_tools

    def create_agent(self):
        """Create Strands agent with filtered tools and session management"""
        try:
            config = self.get_model_config()

            # Create model configuration
            model_config = {
                "model_id": config["model_id"],
                "temperature": config.get("temperature", 0.7)
            }

            # Add cache_prompt if caching is enabled (BedrockModel handles SystemContentBlock formatting)
            if self.caching_enabled:
                model_config["cache_prompt"] = "default"
                logger.info("✅ System prompt caching enabled (cache_prompt=default)")

            model = BedrockModel(**model_config)

            # Get filtered tools based on user preferences
            tools = self.get_filtered_tools()

            # Create hooks
            hooks = []

            # Add stop hook for session cancellation (always enabled)
            stop_hook = StopHook(self.session_manager)
            hooks.append(stop_hook)
            logger.info("✅ Stop hook enabled (BeforeToolCallEvent)")

            # Add conversation caching hook if enabled
            if self.caching_enabled:
                conversation_hook = ConversationCachingHook(enabled=True)
                hooks.append(conversation_hook)
                logger.info("✅ Conversation caching hook enabled")

            # Create agent with session manager, hooks, and system prompt
            # Use SequentialToolExecutor to prevent concurrent browser operations
            # This prevents "Failed to start and initialize Playwright" errors with NovaAct
            self.agent = Agent(
                model=model,
                system_prompt=self.system_prompt,  # Always string - BedrockModel handles caching internally
                tools=tools,
                tool_executor=SequentialToolExecutor(),
                session_manager=self.session_manager,
                hooks=hooks if hooks else None
            )

            logger.info(f"✅ Agent created with {len(tools)} tools")
            logger.info(f"✅ Session Manager: {type(self.session_manager).__name__}")

            if AGENTCORE_MEMORY_AVAILABLE and os.environ.get('MEMORY_ID'):
                logger.info(f"   • Session: {self.session_id}, User: {self.user_id}")
                logger.info(f"   • Short-term memory: Conversation history (90 days retention)")
                logger.info(f"   • Long-term memory: User preferences and facts across sessions")
            else:
                logger.info(f"   • Session: {self.session_id}")
                logger.info(f"   • File-based persistence: {self.session_manager.storage_dir}")

        except Exception as e:
            logger.error(f"Error creating agent: {e}")
            raise

    async def stream_async(self, message: str, session_id: str = None, files: Optional[List] = None) -> AsyncGenerator[str, None]:
        """
        Stream responses using StreamEventProcessor

        Args:
            message: User message text
            session_id: Session identifier
            files: Optional list of FileContent objects (with base64 bytes)
        """

        if not self.agent:
            self.create_agent()

        # Set SESSION_ID for browser session isolation (each conversation has isolated browser)
        import os
        os.environ['SESSION_ID'] = self.session_id
        os.environ['USER_ID'] = self.user_id or self.session_id

        try:
            logger.info(f"Streaming message: {message[:50]}...")
            if files:
                logger.info(f"Processing {len(files)} file(s)")

            # Convert files to Strands ContentBlock format if provided
            prompt = self._build_prompt(message, files)

            # Log prompt type for debugging (without printing bytes)
            if isinstance(prompt, list):
                logger.info(f"Prompt is list with {len(prompt)} content blocks")
            else:
                logger.info(f"Prompt is string: {prompt[:100]}")

            # Use stream processor to handle Strands agent streaming
            async for event in self.stream_processor.process_stream(
                self.agent,
                prompt,  # Can be str or list[ContentBlock]
                file_paths=None,
                session_id=session_id or "default"
            ):
                yield event

            # Flush any buffered messages (turn-based session manager)
            if hasattr(self.session_manager, 'flush'):
                self.session_manager.flush()
                logger.debug(f"💾 Session flushed after streaming complete")

        except Exception as e:
            import traceback
            logger.error(f"Error in stream_async: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")

            # Emergency flush: save buffered messages before losing them
            if hasattr(self.session_manager, 'flush'):
                try:
                    self.session_manager.flush()
                    logger.warning(f"🚨 Emergency flush on error - saved {len(getattr(self.session_manager, 'pending_messages', []))} buffered messages")
                except Exception as flush_error:
                    logger.error(f"Failed to emergency flush: {flush_error}")

            # Send error event
            import json
            error_event = {
                "type": "error",
                "message": str(e)
            }
            yield f"data: {json.dumps(error_event)}\n\n"

    def _sanitize_filename(self, filename: str) -> str:
        """
        Sanitize filename to meet AWS Bedrock requirements:
        - Only alphanumeric, whitespace, hyphens, parentheses, and square brackets
        - No consecutive whitespace
        """
        import re

        # Replace special characters (except allowed ones) with underscore
        sanitized = re.sub(r'[^a-zA-Z0-9\s\-\(\)\[\]]', '_', filename)

        # Replace consecutive whitespace with single space
        sanitized = re.sub(r'\s+', ' ', sanitized)

        # Trim whitespace
        sanitized = sanitized.strip()

        return sanitized

    def _build_prompt(self, message: str, files: Optional[List] = None):
        """
        Build prompt for Strands Agent

        Args:
            message: User message text
            files: Optional list of FileContent objects with base64 bytes

        Returns:
            str or list[ContentBlock]: Prompt for Strands Agent
        """
        import base64

        # If no files, return simple text
        if not files or len(files) == 0:
            return message

        # Build ContentBlock list for multimodal input
        content_blocks = []

        # Add text first
        content_blocks.append({"text": message})

        # Add each file as appropriate ContentBlock
        for file in files:
            content_type = file.content_type.lower()
            filename = file.filename.lower()

            # Decode base64 to bytes
            file_bytes = base64.b64decode(file.bytes)

            # Determine file type and create appropriate ContentBlock
            if content_type.startswith("image/") or filename.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                # Image content
                image_format = self._get_image_format(content_type, filename)
                content_blocks.append({
                    "image": {
                        "format": image_format,
                        "source": {
                            "bytes": file_bytes
                        }
                    }
                })
                logger.info(f"Added image: {filename} (format: {image_format})")

            elif filename.endswith((".pdf", ".csv", ".doc", ".docx", ".xls", ".xlsx", ".html", ".txt", ".md")):
                # Document content
                doc_format = self._get_document_format(filename)

                # Sanitize filename for Bedrock
                sanitized_name = self._sanitize_filename(file.filename)

                content_blocks.append({
                    "document": {
                        "format": doc_format,
                        "name": sanitized_name,
                        "source": {
                            "bytes": file_bytes
                        }
                    }
                })
                logger.info(f"Added document: {filename} -> {sanitized_name} (format: {doc_format})")

            else:
                logger.warning(f"Unsupported file type: {filename} ({content_type})")

        return content_blocks

    def _get_image_format(self, content_type: str, filename: str) -> str:
        """Determine image format from content type or filename"""
        if "png" in content_type or filename.endswith(".png"):
            return "png"
        elif "jpeg" in content_type or "jpg" in content_type or filename.endswith((".jpg", ".jpeg")):
            return "jpeg"
        elif "gif" in content_type or filename.endswith(".gif"):
            return "gif"
        elif "webp" in content_type or filename.endswith(".webp"):
            return "webp"
        else:
            return "png"  # default

    def _get_document_format(self, filename: str) -> str:
        """Determine document format from filename"""
        if filename.endswith(".pdf"):
            return "pdf"
        elif filename.endswith(".csv"):
            return "csv"
        elif filename.endswith(".doc"):
            return "doc"
        elif filename.endswith(".docx"):
            return "docx"
        elif filename.endswith(".xls"):
            return "xls"
        elif filename.endswith(".xlsx"):
            return "xlsx"
        elif filename.endswith(".html"):
            return "html"
        elif filename.endswith(".txt"):
            return "txt"
        elif filename.endswith(".md"):
            return "md"
        else:
            return "txt"  # default
