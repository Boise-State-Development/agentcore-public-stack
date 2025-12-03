import json
from typing import Dict, Any, List, Tuple
from .tool_result_processor import ToolResultProcessor

class StreamEventFormatter:
    """Handles formatting of streaming events for SSE"""

    @staticmethod
    def format_sse_event(event_data: dict) -> str:
        """Format event data as Server-Sent Event with proper JSON serialization"""
        try:
            return f"data: {json.dumps(event_data)}\n\n"
        except (TypeError, ValueError) as e:
            # Fallback for non-serializable objects
            return f"data: {json.dumps({'type': 'error', 'message': f'Serialization error: {str(e)}'})}\n\n"
    
    @staticmethod
    def extract_final_result_data(final_result) -> Tuple[List[Dict[str, str]], str]:
        """Extract images and text from final result with simplified logic"""
        images = []
        result_text = str(final_result)
        
        try:
            if hasattr(final_result, 'message') and hasattr(final_result.message, 'content'):
                content = final_result.message.content
                text_parts = []
                
                for item in content:
                    if isinstance(item, dict):
                        if "text" in item:
                            text_parts.append(item["text"])
                        elif "image" in item and "source" in item["image"]:
                            # Simple image extraction
                            image_data = item["image"]
                            images.append({
                                "format": image_data.get("format", "png"),
                                "data": image_data["source"].get("data", "")
                            })
                
                if text_parts:
                    result_text = " ".join(text_parts)
        
        except Exception as e:
            pass
        
        return images, result_text
    
    @staticmethod
    def create_init_event() -> str:
        """Create initialization event"""
        return StreamEventFormatter.format_sse_event({
            "type": "init",
            "message": "Initializing..."
        })
    
    @staticmethod
    def create_reasoning_event(reasoning_text: str) -> str:
        """Create reasoning event"""
        return StreamEventFormatter.format_sse_event({
            "type": "reasoning",
            "text": reasoning_text,
            "step": "thinking"
        })
    
    @staticmethod
    def create_response_event(text: str) -> str:
        """Create response event"""
        return StreamEventFormatter.format_sse_event({
            "type": "response",
            "text": text,
            "step": "answering"
        })
    
    @staticmethod
    def create_tool_use_event(tool_use: Dict[str, Any]) -> str:
        """Create tool use event"""
        return StreamEventFormatter.format_sse_event({
            "type": "tool_use",
            "toolUseId": tool_use.get("toolUseId"),
            "name": tool_use.get("name"),
            "input": tool_use.get("input", {})
        })
    
    @staticmethod
    def create_tool_result_event(
        tool_result: Dict[str, Any],
        session_id: str = None,
        tool_name: str = None
    ) -> str:
        """
        Create tool result event - uses ToolResultProcessor for processing.

        Args:
            tool_result: The tool result dictionary
            session_id: Optional session ID for file storage
            tool_name: Optional tool name for specialized processing
        """
        # Process the tool result using the dedicated processor
        result_text, result_images = ToolResultProcessor.process_tool_result(
            tool_result=tool_result,
            session_id=session_id,
            tool_name=tool_name
        )

        # Build and return the event
        event = StreamEventFormatter._build_tool_result_event(
            tool_result, result_text, result_images
        )

        return event
    
    @staticmethod
    def _build_tool_result_event(tool_result: Dict[str, Any], result_text: str, result_images: List[Dict[str, str]]) -> str:
        """Build the final tool result event"""
        tool_result_data = {
            "type": "tool_result",
            "toolUseId": tool_result.get("toolUseId"),
            "result": result_text
        }

        if result_images:
            tool_result_data["images"] = result_images

        # Include metadata if present (e.g., browserSessionId for Live View)
        if "metadata" in tool_result:
            tool_result_data["metadata"] = tool_result["metadata"]

        return StreamEventFormatter.format_sse_event(tool_result_data)
    
    @staticmethod
    def create_complete_event(message: str, images: List[Dict[str, str]] = None, usage: Dict[str, Any] = None) -> str:
        """Create completion event with optional token usage metrics"""
        completion_data = {
            "type": "complete",
            "message": message
        }
        if images:
            completion_data["images"] = images
        if usage:
            completion_data["usage"] = usage

        return StreamEventFormatter.format_sse_event(completion_data)
    
    @staticmethod
    def create_error_event(error_message: str) -> str:
        """Create error event"""
        return StreamEventFormatter.format_sse_event({
            "type": "error",
            "message": error_message
        })
    
    @staticmethod
    def create_thinking_event(message: str = "Processing your request...") -> str:
        """Create thinking event"""
        return StreamEventFormatter.format_sse_event({
            "type": "thinking",
            "message": message
        })
    
    @staticmethod
    def create_progress_event(progress_data: Dict[str, Any]) -> str:
        """Create progress event for tool execution"""
        return StreamEventFormatter.format_sse_event({
            "type": "tool_progress",
            "toolId": progress_data.get("toolId"),
            "sessionId": progress_data.get("sessionId"),
            "step": progress_data.get("step"),
            "message": progress_data.get("message"),
            "progress": progress_data.get("progress"),
            "timestamp": progress_data.get("timestamp"),
            "metadata": progress_data.get("metadata", {})
        })
    

