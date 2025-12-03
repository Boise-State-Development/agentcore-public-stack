"""
Tool Result Processor

Handles post-processing of tool results including:
- Image extraction from various formats
- Base64 file saving to disk
- JSON content processing
- Text cleaning for display

This is a separate concern from stream processing and should be called
by the router/service layer after receiving tool results.
"""

import json
import base64
import os
import re
from typing import Dict, Any, List, Tuple


class ToolResultProcessor:
    """Processes tool results for image extraction and file operations"""

    @staticmethod
    def process_tool_result(
        tool_result: Dict[str, Any],
        session_id: str = None,
        tool_name: str = None
    ) -> Tuple[str, List[Dict[str, str]]]:
        """
        Process a tool result to extract content and images.

        Args:
            tool_result: The tool result dictionary with content field
            session_id: Session ID for file storage
            tool_name: Name of the tool that generated this result

        Returns:
            Tuple of (processed_text, list_of_images)

        Example:
            text, images = ToolResultProcessor.process_tool_result(
                tool_result={"content": [{"text": "Result"}]},
                session_id="abc123",
                tool_name="run_python_code"
            )
        """
        # Handle case where entire tool_result might be a JSON string
        if isinstance(tool_result, str):
            try:
                tool_result = json.loads(tool_result)
            except json.JSONDecodeError:
                tool_result = {
                    "toolUseId": "unknown",
                    "content": [{"text": str(tool_result)}]
                }

        # Extract all content (text and images) and process Base64
        result_text, result_images = ToolResultProcessor._extract_all_content(tool_result)

        # Handle storage based on tool type (if session_id provided)
        if session_id and tool_name:
            tool_use_id = tool_result.get("toolUseId")
            result_text = ToolResultProcessor._handle_tool_storage(
                tool_use_id, tool_name, result_text, session_id
            )

        return result_text, result_images

    @staticmethod
    def _extract_all_content(tool_result: Dict[str, Any]) -> Tuple[str, List[Dict[str, str]]]:
        """Extract text content and images from tool result"""
        # Extract basic content from MCP format
        result_text, result_images = ToolResultProcessor._extract_basic_content(tool_result)

        # Process JSON content for screenshots and additional images
        json_images, cleaned_text = ToolResultProcessor._process_json_content(result_text)
        result_images.extend(json_images)

        return cleaned_text, result_images

    @staticmethod
    def _extract_basic_content(tool_result: Dict[str, Any]) -> Tuple[str, List[Dict[str, str]]]:
        """Extract basic text and image content from MCP format"""
        result_text = ""
        result_images = []

        # Handle case where content might be a JSON string
        if "content" in tool_result and isinstance(tool_result["content"], str):
            try:
                parsed_content = json.loads(tool_result["content"])
                tool_result = tool_result.copy()
                tool_result["content"] = parsed_content
            except json.JSONDecodeError:
                pass

        if "content" in tool_result:
            content = tool_result["content"]

            for item in content:
                if isinstance(item, dict):
                    if "text" in item:
                        text_content = item["text"]

                        # Check if this text is actually a JSON-stringified MCP response
                        if text_content.strip().startswith('{"status":') and '"content":[' in text_content:
                            try:
                                parsed_mcp = json.loads(text_content)

                                # Replace the current tool_result with the parsed MCP response
                                if "content" in parsed_mcp and isinstance(parsed_mcp["content"], list):
                                    # Recursively process the unwrapped content
                                    for unwrapped_item in parsed_mcp["content"]:
                                        if isinstance(unwrapped_item, dict):
                                            if "text" in unwrapped_item:
                                                result_text += unwrapped_item["text"]
                                            elif "image" in unwrapped_item and "source" in unwrapped_item["image"]:
                                                image_source = unwrapped_item["image"]["source"]
                                                image_data = ""

                                                if "data" in image_source:
                                                    image_data = image_source["data"]
                                                elif "bytes" in image_source:
                                                    if isinstance(image_source["bytes"], bytes):
                                                        image_data = base64.b64encode(
                                                            image_source["bytes"]
                                                        ).decode('utf-8')
                                                    else:
                                                        image_data = str(image_source["bytes"])

                                                if image_data:
                                                    result_images.append({
                                                        "format": unwrapped_item["image"].get("format", "png"),
                                                        "data": image_data
                                                    })

                                    # Skip the normal text processing since we handled the unwrapped content
                                    continue
                            except json.JSONDecodeError:
                                # Fall through to normal text processing
                                pass

                        # Normal text processing (if not unwrapped)
                        result_text += text_content

                    elif "image" in item:
                        if "source" in item["image"]:
                            image_source = item["image"]["source"]
                            image_data = ""

                            if "data" in image_source:
                                image_data = image_source["data"]
                            elif "bytes" in image_source:
                                if isinstance(image_source["bytes"], bytes):
                                    image_data = base64.b64encode(
                                        image_source["bytes"]
                                    ).decode('utf-8')
                                else:
                                    image_data = str(image_source["bytes"])

                            if image_data:
                                result_images.append({
                                    "format": item["image"].get("format", "png"),
                                    "data": image_data
                                })

        return result_text, result_images

    @staticmethod
    def _process_json_content(result_text: str) -> Tuple[List[Dict[str, str]], str]:
        """Process JSON content to extract screenshots and clean text"""
        try:
            parsed_result = json.loads(result_text)
            extracted_images = ToolResultProcessor._extract_images_from_json_response(parsed_result)

            if extracted_images:
                cleaned_text = ToolResultProcessor._clean_result_text_for_display(
                    result_text, parsed_result
                )
                return extracted_images, cleaned_text
            else:
                return [], result_text

        except (json.JSONDecodeError, TypeError):
            return [], result_text

    @staticmethod
    def _extract_images_from_json_response(response_data: Dict[str, Any]) -> List[Dict[str, str]]:
        """Extract images from any JSON tool response automatically"""
        images = []

        if isinstance(response_data, dict):
            # Support common image field patterns
            image_fields = ['screenshot', 'image', 'diagram', 'chart', 'visualization', 'figure']

            for field in image_fields:
                if field in response_data and isinstance(response_data[field], dict):
                    img_data = response_data[field]

                    # Handle new lightweight screenshot format (Nova Act optimized)
                    if img_data.get("available") and "description" in img_data:
                        # This is the new optimized format - no actual image data
                        print(f"📷 Found optimized screenshot reference: {img_data.get('description')}")
                        continue

                    # Handle legacy format with actual base64 data
                    elif "data" in img_data and "format" in img_data:
                        images.append({
                            "format": img_data["format"],
                            "data": img_data["data"]
                        })

            # Preserve existing images array
            if "images" in response_data and isinstance(response_data["images"], list):
                images.extend(response_data["images"])

        return images

    @staticmethod
    def _clean_result_text_for_display(original_text: str, parsed_result: dict) -> str:
        """Clean result text by removing large image data but keeping other information"""
        try:
            import copy

            # Create a copy to avoid modifying the original
            cleaned_result = copy.deepcopy(parsed_result)

            # Remove large image data fields but keep metadata
            image_fields = ['screenshot', 'image', 'diagram', 'chart', 'visualization', 'figure']

            for field in image_fields:
                if field in cleaned_result and isinstance(cleaned_result[field], dict):
                    if "data" in cleaned_result[field]:
                        # Keep format and size info, remove the large base64 data
                        data_size = len(cleaned_result[field]["data"])
                        cleaned_result[field] = {
                            "format": cleaned_result[field].get("format", "unknown"),
                            "size": f"{data_size} characters",
                            "note": "Image data extracted and displayed separately"
                        }

            # Return the cleaned JSON string
            return json.dumps(cleaned_result, indent=2)

        except Exception as e:
            # If cleaning fails, return the original
            print(f"Warning: Failed to clean result text: {e}")
            return original_text

    @staticmethod
    def _handle_tool_storage(
        tool_use_id: str,
        tool_name: str,
        result_text: str,
        session_id: str
    ) -> str:
        """Process Base64 downloads for Python MCP tools"""
        # Only process for Python MCP tools
        if tool_name not in ['run_python_code', 'finalize_document']:
            return result_text

        try:
            processed_text, file_info = ToolResultProcessor._handle_python_mcp_base64(
                tool_use_id, result_text, session_id
            )
            if file_info:
                print(f"📁 Processed {len(file_info)} files for {tool_use_id}")
                return processed_text
        except Exception as e:
            print(f"⚠️ Error processing Base64 downloads: {e}")

        return result_text

    @staticmethod
    def _handle_python_mcp_base64(
        tool_use_id: str,
        result_text: str,
        session_id: str
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Intercept Base64 file data from Python MCP results and save to local files.

        Returns:
            Tuple of (processed_text_without_base64, file_info_list)
        """
        from agentcore.config import Config

        file_info = []
        processed_text = result_text

        try:
            # Pattern to match Base64 data URLs with optional filename attribute
            base64_pattern = r'<download(?:\s+filename="([^"]+)")?>data:([^;]+);base64,([A-Za-z0-9+/=\s]+?)</download>'

            matches = re.findall(base64_pattern, result_text)

            def process_base64_match(match):
                custom_filename = match.group(1)  # May be None if not provided
                mime_type = match.group(2)
                base64_data = match.group(3)

                try:
                    # Decode Base64 data (strip whitespace first)
                    clean_base64 = base64_data.replace('\n', '').replace('\r', '').replace(' ', '')
                    file_data = base64.b64decode(clean_base64)

                    # Determine file extension from MIME type
                    extension_map = {
                        'application/zip': '.zip',
                        'text/plain': '.txt',
                        'application/json': '.json',
                        'text/csv': '.csv',
                        'image/png': '.png',
                        'image/jpeg': '.jpg',
                        'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx'
                    }
                    extension = extension_map.get(mime_type, '.bin')

                    # Use custom filename if provided, otherwise generate one
                    if custom_filename:
                        filename = custom_filename
                    else:
                        filename = f"python_output_{len(file_info) + 1}{extension}"

                    # Create output directory using provided session_id
                    try:
                        session_output_dir = Config.get_session_output_dir(session_id)
                        tool_dir = os.path.join(session_output_dir, tool_use_id)
                        os.makedirs(tool_dir, exist_ok=True)
                    except Exception as dir_error:
                        print(f"❌ Error creating directory: {dir_error}")
                        return match.group(0)

                    # Save file
                    try:
                        file_path = os.path.join(tool_dir, filename)
                        with open(file_path, 'wb') as f:
                            f.write(file_data)

                        # Create download URL (relative to output dir, served from /output/)
                        relative_path = os.path.relpath(file_path, Config.get_output_dir())
                        download_url = f"/output/{relative_path}"

                        file_info.append({
                            'filename': filename,
                            'mime_type': mime_type,
                            'size': len(file_data),
                            'download_url': download_url,
                            'local_path': file_path
                        })

                        print(f"💾 Saved Base64 file: {filename} ({len(file_data)} bytes) -> {file_path}")

                        # Replace Base64 data with file-specific message
                        file_size_kb = len(file_data) / 1024
                        if mime_type == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
                            return f"✅ Document saved: **{filename}** ({file_size_kb:.1f} KB) - [Download]({download_url})"
                        elif mime_type == 'application/zip':
                            return f"📁 Files saved as ZIP archive: **{filename}** ({file_size_kb:.1f} KB) - [Download]({download_url})"
                        else:
                            return f"✅ File saved: **{filename}** ({file_size_kb:.1f} KB) - [Download]({download_url})"
                    except Exception as save_error:
                        print(f"❌ Error saving file: {save_error}")
                        return match.group(0)

                except Exception as e:
                    print(f"❌ Error processing Base64 data: {e}")
                    return match.group(0)

            # Process all Base64 matches
            processed_text = re.sub(base64_pattern, process_base64_match, result_text)

        except Exception as e:
            print(f"❌ Error in _handle_python_mcp_base64: {e}")

        return processed_text, file_info
