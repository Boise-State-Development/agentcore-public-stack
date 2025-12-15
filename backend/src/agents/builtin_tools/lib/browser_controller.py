"""
Browser Controller for AgentCore Browser + Nova Act integration.
Simplified implementation for browser automation with natural language.
"""

import os
import logging
import asyncio
import base64
from typing import Dict, Any, Optional
from bedrock_agentcore.tools.browser_client import BrowserClient

# NOVA_ACT DISABLED: Commented out temporarily due to version conflict with strands-agents
# To re-enable: Uncomment this import and uncomment nova-act in pyproject.toml
# from nova_act import (
#     ActInvalidModelGenerationError,
#     ActExceededMaxStepsError,
#     ActTimeoutError,
#     ActAgentError,
#     ActClientError
# )

logger = logging.getLogger(__name__)

# Global session cache
_browser_sessions: Dict[str, 'BrowserController'] = {}


class BrowserController:
    """Simplified browser controller using AgentCore Browser + Nova Act"""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.region = os.getenv('AWS_REGION', 'us-west-2')

        # Get Custom Browser ID from environment or Parameter Store
        self.browser_id = self._get_browser_id()
        self.browser_name = os.getenv('BROWSER_NAME')

        # NOVA_ACT DISABLED: API key loading commented out
        # To re-enable: Uncomment this section and uncomment nova-act in pyproject.toml
        # Try to load Nova Act API key from environment variable first
        # self.nova_api_key = os.getenv('NOVA_ACT_API_KEY')
        self.nova_api_key = None  # Set to None when nova_act is disabled

        # If not in environment, try to load from Secrets Manager
        # if not self.nova_api_key:
        #     try:
        #         import boto3
        #
        #         secrets_client = boto3.client('secretsmanager', region_name=self.region)
        #         project_name = os.getenv('PROJECT_NAME', 'strands-agent-chatbot')
        #         secret_name = f"{project_name}/nova-act-api-key"
        #
        #         logger.info(f"Loading Nova Act API key from Secrets Manager: {secret_name}")
        #         response = secrets_client.get_secret_value(SecretId=secret_name)
        #
        #         self.nova_api_key = response['SecretString']
        #         logger.info("Nova Act API key loaded successfully from Secrets Manager")
        #     except secrets_client.exceptions.ResourceNotFoundException:
        #         raise ValueError(
        #             "❌ Nova Act API key not configured. "
        #             "Browser automation tools are disabled. "
        #             "To enable, run deployment and enter your Nova Act API key."
        #         )
        #     except Exception as e:
        #         raise ValueError(f"Failed to load Nova Act API key: {e}")

        self.browser_session_client = None
        self.page = None  # Will be set from NovaAct.page
        self.nova_client = None
        self._connected = False

    def _get_browser_id(self) -> Optional[str]:
        """Get Custom Browser ID from environment or Parameter Store"""
        # 1. Check environment variable (set by AgentCore Runtime)
        browser_id = os.getenv('BROWSER_ID')
        if browser_id:
            logger.info(f"Found BROWSER_ID in environment: {browser_id}")
            return browser_id

        # 2. Try Parameter Store (for local development or alternative configuration)
        try:
            import boto3
            project_name = os.getenv('PROJECT_NAME', 'strands-agent-chatbot')
            environment = os.getenv('ENVIRONMENT', 'dev')
            param_name = f"/{project_name}/{environment}/agentcore/browser-id"

            logger.info(f"Checking Parameter Store for Browser ID: {param_name}")
            ssm = boto3.client('ssm', region_name=self.region)
            response = ssm.get_parameter(Name=param_name)
            browser_id = response['Parameter']['Value']
            logger.info(f"Found BROWSER_ID in Parameter Store: {browser_id}")
            return browser_id
        except Exception as e:
            logger.warning(f"Custom Browser ID not found in Parameter Store: {e}")
            return None

    def connect(self):
        """Connect to AgentCore Browser via WebSocket/CDP (synchronous)"""
        if self._connected:
            logger.info(f"Session {self.session_id} already connected")
            return

        try:
            logger.info(f"Connecting to AgentCore Browser for session {self.session_id}")

            # Require Custom Browser ID - no fallback to system browser
            if not self.browser_id:
                raise ValueError(
                    "❌ Custom Browser ID not found. "
                    "Browser tools require Custom Browser with Web Bot Auth. "
                    "Please deploy AgentCore Runtime Stack to create Custom Browser."
                )

            # Create AgentCore Browser session using BrowserClient with Custom Browser
            self.browser_session_client = BrowserClient(region=self.region)

            logger.info(f"🔐 Starting Custom Browser with Web Bot Auth: {self.browser_name} (ID: {self.browser_id})")
            # Pass identifier parameter to use Custom Browser
            # Nova Act optimal resolution: width 864-1536, height 1296-2304
            # Using 1536×1296 for landscape-friendly display within optimal range
            session_id = self.browser_session_client.start(
                identifier=self.browser_id,
                session_timeout_seconds=3600,
                viewport={'width': 1536, 'height': 1296}
            )

            logger.info(f"✅ Browser session started: {session_id}")
            ws_url, headers = self.browser_session_client.generate_ws_headers()

            # NOVA_ACT DISABLED: NovaAct initialization commented out
            # To re-enable: Uncomment this section and uncomment nova-act in pyproject.toml
            # Initialize Nova Act client with AgentCore Browser CDP connection
            # from nova_act import NovaAct
            # self.nova_client = NovaAct(
            #     cdp_endpoint_url=ws_url,
            #     cdp_headers=headers,
            #     nova_act_api_key=self.nova_api_key,
            #     starting_page="about:blank"  # Will navigate later
            # )
            # # Start NovaAct (enters context manager)
            # self.nova_client.__enter__()
            #
            # # Get page from NovaAct for screenshots
            # self.page = self.nova_client.page

            # When nova_act is disabled, browser tools are not available
            raise ValueError(
                "❌ Nova Act is currently disabled. "
                "Browser automation tools require nova-act which is temporarily disabled due to version conflicts. "
                "To re-enable: Uncomment nova-act in pyproject.toml and uncomment nova_act code in browser_controller.py"
            )

            # self._connected = True
            # logger.info(f"Successfully connected to AgentCore Browser for session {self.session_id}")

        except Exception as e:
            logger.error(f"Failed to connect to AgentCore Browser: {e}")
            self.close()
            raise

    def navigate(self, url: str) -> Dict[str, Any]:
        """Navigate to URL and return result with screenshot"""
        # NOVA_ACT DISABLED: Browser navigation requires nova_act
        logger.warning("Browser navigation is disabled - nova_act is not available")
        return {
            "status": "error",
            "message": "❌ Browser navigation is currently disabled. Nova Act is temporarily disabled due to version conflicts. To re-enable: Uncomment nova-act in pyproject.toml and uncomment nova_act code in browser_controller.py",
            "screenshot": None
        }
        
        # Original implementation (commented out):
        # try:
        #     if not self._connected:
        #         self.connect()
        #
        #     logger.info(f"Navigating to {url}")
        #
        #     # Use NovaAct's go_to_url() instead of act() for more reliable navigation
        #     # go_to_url() handles timeout better and waits for page to fully load
        #     self.nova_client.go_to_url(url)
        #
        #     current_url = self.page.url
        #     page_title = self.page.title()
        #     screenshot_data = self._take_screenshot()
        #
        #     logger.info(f"✅ Successfully navigated to: {current_url}")
        #     logger.info(f"   Page title: {page_title}")
        #
        #     return {
        #         "status": "success",
        #         "message": f"Navigated to {current_url}",
        #         "current_url": current_url,
        #         "page_title": page_title,
        #         "screenshot": screenshot_data
        #     }
        # except Exception as e:
        #     logger.error(f"Navigation failed: {e}")
        #     return {
        #         "status": "error",
        #         "message": f"Navigation failed: {str(e)}",
        #         "screenshot": None
        #     }

    def act(self, instruction: str, max_steps: int = 5, timeout: int = 120) -> Dict[str, Any]:
        """Execute natural language instruction using Nova Act

        Args:
            instruction: Natural language instruction for the browser
            max_steps: Maximum number of steps (browser actuations) to take
            timeout: Timeout in seconds for the entire act call
        """
        # NOVA_ACT DISABLED: Browser actions require nova_act
        logger.warning("Browser act is disabled - nova_act is not available")
        return {
            "status": "error",
            "message": "❌ Browser actions are currently disabled. Nova Act is temporarily disabled due to version conflicts. To re-enable: Uncomment nova-act in pyproject.toml and uncomment nova_act code in browser_controller.py",
            "instruction": instruction,
            "screenshot": None
        }
        
        # Original implementation (commented out):
        # try:
        #     if not self._connected:
        #         self.connect()
        #
        #     logger.info(f"Executing action: {instruction}")
        #     logger.info(f"Parameters: max_steps={max_steps}, timeout={timeout}s")
        #
        #     # Execute Nova Act instruction (first arg is positional)
        #     # observation_delay_ms: Wait after action for page loads (1.5s helps with slow-loading content)
        #     result = self.nova_client.act(
        #         instruction,
        #         max_steps=max_steps,
        #         timeout=timeout,
        #         observation_delay_ms=1500  # 1.5 second delay for page loads
        #     )
        #
        #     current_url = self.page.url
        #     page_title = self.page.title()
        #     screenshot_data = self._take_screenshot()
        #
        #     # Parse Nova Act result
        #     success = getattr(result, 'success', False)
        #     details = getattr(result, 'details', '') or str(result)
        #
        #     # Extract and log execution metadata
        #     metadata = getattr(result, 'metadata', None)
        #     execution_info = ""
        #     if metadata:
        #         session_id = getattr(metadata, 'session_id', None)
        #         act_id = getattr(metadata, 'act_id', None)
        #         steps_executed = getattr(metadata, 'num_steps_executed', None)
        #         start_time = getattr(metadata, 'start_time', None)
        #         end_time = getattr(metadata, 'end_time', None)
        #
        #         # Log detailed metadata
        #         logger.info(f"✅ Act completed:")
        #         if session_id:
        #             logger.info(f"   Session ID: {session_id}")
        #         if act_id:
        #             logger.info(f"   Act ID: {act_id}")
        #         if steps_executed is not None:
        #             logger.info(f"   Steps: {steps_executed}/{max_steps}")
        #             execution_info = f" (executed {steps_executed}/{max_steps} steps)"
        #         if start_time and end_time:
        #             duration = end_time - start_time
        #             logger.info(f"   Duration: {duration:.2f}s")
        #
        #     # Note: Bedrock API only accepts "success" or "error" status
        #     # Even if action was partial, we return "success" with details in message
        #     return {
        #         "status": "success",  # Always "success" if no exception (Bedrock requirement)
        #         "message": f"{'✓ ' if success else '⚠️ '}{details}{execution_info}",
        #         "instruction": instruction,
        #         "current_url": current_url,
        #         "page_title": page_title,
        #         "screenshot": screenshot_data
        #     }
        #
        # except ActInvalidModelGenerationError as e:
        #     # Schema validation failed or model generated invalid output
        #     logger.error(f"❌ Invalid model generation: {e}")
        #     screenshot_data = self._get_error_screenshot()
        #     return {
        #         "status": "error",
        #         "message": f"Invalid model output: {str(e)}\n\nTry simplifying the instruction or breaking it into smaller steps.",
        #         "instruction": instruction,
        #         "screenshot": screenshot_data
        #     }
        #
        # except ActExceededMaxStepsError as e:
        #     # Task too complex for the given max_steps
        #     logger.error(f"❌ Exceeded max steps ({max_steps}): {e}")
        #     screenshot_data = self._get_error_screenshot()
        #     return {
        #         "status": "error",
        #         "message": f"Task too complex (exceeded {max_steps} steps): {str(e)}\n\nBreak this into smaller, simpler instructions.",
        #         "instruction": instruction,
        #         "screenshot": screenshot_data
        #     }
        #
        # except ActTimeoutError as e:
        #     # Operation timed out
        #     logger.error(f"❌ Timeout ({timeout}s): {e}")
        #     screenshot_data = self._get_error_screenshot()
        #     return {
        #         "status": "error",
        #         "message": f"Operation timed out after {timeout}s: {str(e)}\n\nTry increasing timeout or simplifying the task.",
        #         "instruction": instruction,
        #         "screenshot": screenshot_data
        #     }
        #
        # except (ActAgentError, ActClientError) as e:
        #     # Retriable errors - agent failed or invalid request
        #     logger.error(f"❌ Act error: {e}")
        #     screenshot_data = self._get_error_screenshot()
        #     return {
        #         "status": "error",
        #         "message": f"Action failed: {str(e)}\n\nYou may retry with a different instruction.",
        #         "instruction": instruction,
        #         "screenshot": screenshot_data
        #     }
        #
        # except Exception as e:
        #     # Unknown error
        #     logger.error(f"❌ Unexpected error: {e}")
        #     screenshot_data = self._get_error_screenshot()
        #     return {
        #         "status": "error",
        #         "message": f"Action failed: {str(e)}",
        #         "instruction": instruction,
        #         "screenshot": screenshot_data
        #     }

    def _get_error_screenshot(self) -> Optional[bytes]:
        """Helper to safely capture screenshot on error"""
        try:
            if self._connected and self.page:
                return self._take_screenshot()
        except:
            pass
        return None

    def extract(self, description: str, schema: Optional[Dict] = None, max_steps: int = 3, timeout: int = 90) -> Dict[str, Any]:
        """Extract structured data using Nova Act

        Args:
            description: Natural language description of what data to extract
            schema: Optional JSON schema for validation (None = no schema validation)
            max_steps: Maximum number of steps for extraction
            timeout: Timeout in seconds for extraction
        """
        # NOVA_ACT DISABLED: Browser extraction requires nova_act
        logger.warning("Browser extraction is disabled - nova_act is not available")
        return {
            "status": "error",
            "message": "❌ Browser extraction is currently disabled. Nova Act is temporarily disabled due to version conflicts. To re-enable: Uncomment nova-act in pyproject.toml and uncomment nova_act code in browser_controller.py",
            "description": description,
            "screenshot": None
        }
        
        # Original implementation (commented out):
        # try:
        #     if not self._connected:
        #         self.connect()
        #
        #     logger.info(f"Extracting data: {description}")
        #     logger.info(f"Parameters: max_steps={max_steps}, timeout={timeout}s, schema={schema is not None}")
        #
        #     # Build extraction prompt
        #     prompt = f"{description} from the current webpage"
        #
        #     # Execute Nova Act extraction (first arg is positional)
        #     # observation_delay_ms: Wait after action for page loads (1.5s helps with slow-loading content)
        #     result = self.nova_client.act(
        #         prompt,
        #         schema=schema,
        #         max_steps=max_steps,
        #         timeout=timeout,
        #         observation_delay_ms=1500  # 1.5 second delay for page loads
        #     )
        #
        #     current_url = self.page.url
        #     page_title = self.page.title()
        #     screenshot_data = self._take_screenshot()
        #
        #     # Parse extracted data
        #     extracted_data = getattr(result, 'parsed_response', None) or getattr(result, 'response', {})
        #
        #     # Extract and log execution metadata
        #     metadata = getattr(result, 'metadata', None)
        #     execution_info = ""
        #     if metadata:
        #         session_id = getattr(metadata, 'session_id', None)
        #         act_id = getattr(metadata, 'act_id', None)
        #         steps_executed = getattr(metadata, 'num_steps_executed', None)
        #         start_time = getattr(metadata, 'start_time', None)
        #         end_time = getattr(metadata, 'end_time', None)
        #
        #         # Log detailed metadata
        #         logger.info(f"✅ Extraction completed:")
        #         if session_id:
        #             logger.info(f"   Session ID: {session_id}")
        #         if act_id:
        #             logger.info(f"   Act ID: {act_id}")
        #         if steps_executed is not None:
        #             logger.info(f"   Steps: {steps_executed}/{max_steps}")
        #             execution_info = f" (executed {steps_executed}/{max_steps} steps)"
        #         if start_time and end_time:
        #             duration = end_time - start_time
        #             logger.info(f"   Duration: {duration:.2f}s")
        #
        #     return {
        #         "status": "success",
        #         "message": f"Data extracted successfully{execution_info}",
        #         "data": extracted_data,
        #         "description": description,
        #         "current_url": current_url,
        #         "page_title": page_title,
        #         "screenshot": screenshot_data
        #     }
        #
        # except ActInvalidModelGenerationError as e:
        #     # Schema validation failed - this is the error you experienced!
        #     logger.error(f"❌ Schema validation failed: {e}")
        #     screenshot_data = self._get_error_screenshot()
        #     return {
        #         "status": "error",
        #         "message": f"Schema validation failed: {str(e)}\n\nThe extracted data didn't match the expected format. Try simplifying the description or using no schema.",
        #         "description": description,
        #         "screenshot": screenshot_data
        #     }
        #
        # except ActExceededMaxStepsError as e:
        #     # Extraction too complex
        #     logger.error(f"❌ Exceeded max steps ({max_steps}): {e}")
        #     screenshot_data = self._get_error_screenshot()
        #     return {
        #         "status": "error",
        #         "message": f"Extraction too complex (exceeded {max_steps} steps): {str(e)}\n\nSimplify what you're trying to extract.",
        #         "description": description,
        #         "screenshot": screenshot_data
        #     }
        #
        # except ActTimeoutError as e:
        #     # Extraction timed out
        #     logger.error(f"❌ Timeout ({timeout}s): {e}")
        #     screenshot_data = self._get_error_screenshot()
        #     return {
        #         "status": "error",
        #         "message": f"Extraction timed out after {timeout}s: {str(e)}",
        #         "description": description,
        #         "screenshot": screenshot_data
        #     }
        #
        # except (ActAgentError, ActClientError) as e:
        #     # Retriable errors
        #     logger.error(f"❌ Extraction error: {e}")
        #     screenshot_data = self._get_error_screenshot()
        #     return {
        #         "status": "error",
        #         "message": f"Extraction failed: {str(e)}\n\nYou may retry with a different description.",
        #         "description": description,
        #         "screenshot": screenshot_data
        #     }
        #
        # except Exception as e:
        #     # Unknown error
        #     logger.error(f"❌ Unexpected error: {e}")
        #     screenshot_data = self._get_error_screenshot()
        #     return {
        #         "status": "error",
        #         "message": f"Extraction failed: {str(e)}",
        #         "description": description,
        #         "screenshot": screenshot_data
        #     }

    def get_page_info(self) -> Dict[str, Any]:
        """Get structured information about the current page state.

        Fast and reliable - uses Playwright API directly (no AI inference).
        Returns comprehensive page state for quick situation assessment.
        """
        # NOVA_ACT DISABLED: get_page_info requires a connected browser session
        logger.warning("Browser get_page_info is disabled - nova_act is not available")
        return {
            "status": "error",
            "message": "❌ Browser page info is currently disabled. Nova Act is temporarily disabled due to version conflicts. To re-enable: Uncomment nova-act in pyproject.toml and uncomment nova_act code in browser_controller.py"
        }
        
        # Original implementation (commented out):
        # try:
        #     if not self._connected:
        #         self.connect()
        #
        #     logger.info("Getting page info")
        #
        #     # Page context
        #     page_info = {
        #         "url": self.page.url,
        #         "title": self.page.title(),
        #         "load_state": "complete" if self.page.url != "about:blank" else "initial"
        #     }
        #
        #     # Scroll position
        #     scroll_info = self.page.evaluate("""() => {
        #         return {
        #             current: window.scrollY,
        #             max: Math.max(
        #                 document.body.scrollHeight,
        #                 document.documentElement.scrollHeight
        #             ) - window.innerHeight,
        #             viewport_height: window.innerHeight
        #         }
        #     }""")
        #
        #     max_scroll = max(scroll_info['max'], 1)  # Avoid division by zero
        #     page_info["scroll"] = {
        #         "current": scroll_info['current'],
        #         "max": max_scroll,
        #         "percentage": int((scroll_info['current'] / max_scroll) * 100) if max_scroll > 0 else 0
        #     }
        #
        #     # Interactive elements (visible only, top 10 each)
        #     buttons = self.page.evaluate("""() => {
        #         const buttons = Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"], [role="button"]'));
        #         return buttons
        #             .filter(btn => {
        #                 const rect = btn.getBoundingClientRect();
        #                 return rect.width > 0 && rect.height > 0 &&
        #                        rect.top < window.innerHeight && rect.bottom > 0;
        #             })
        #             .slice(0, 10)
        #             .map(btn => ({
        #                 text: (btn.innerText || btn.value || btn.getAttribute('aria-label') || '').trim().slice(0, 50),
        #                 visible: true,
        #                 enabled: !btn.disabled
        #             }))
        #             .filter(btn => btn.text.length > 0);
        #     }""")
        #
        #     links = self.page.evaluate("""() => {
        #         const links = Array.from(document.querySelectorAll('a[href]'));
        #         return links
        #             .filter(link => {
        #                 const rect = link.getBoundingClientRect();
        #                 return rect.width > 0 && rect.height > 0 &&
        #                        rect.top < window.innerHeight && rect.bottom > 0;
        #             })
        #             .slice(0, 10)
        #             .map(link => ({
        #                 text: (link.innerText || link.textContent || '').trim().slice(0, 50),
        #                 href: link.getAttribute('href')
        #             }))
        #             .filter(link => link.text.length > 0);
        #     }""")
        #
        #     inputs = self.page.evaluate("""() => {
        #         const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"]), textarea, select'));
        #         return inputs
        #             .filter(input => {
        #                 const rect = input.getBoundingClientRect();
        #                 return rect.width > 0 && rect.height > 0;
        #             })
        #             .slice(0, 10)
        #             .map(input => {
        #                 const base = {
        #                     type: input.type || input.tagName.toLowerCase(),
        #                     name: input.name || input.id || '',
        #                     placeholder: input.placeholder || '',
        #                     label: (input.labels?.[0]?.textContent || '').trim().slice(0, 50)
        #                 };
        #
        #                 if (input.tagName.toLowerCase() === 'select') {
        #                     base.options = Array.from(input.options).slice(0, 5).map(opt => opt.text.trim());
        #                 }
        #
        #                 return base;
        #             });
        #     }""")
        #
        #     interactive = {
        #         "buttons": buttons,
        #         "links": links,
        #         "inputs": inputs
        #     }
        #
        #     # Content structure
        #     headings = self.page.evaluate("""() => {
        #         const headings = Array.from(document.querySelectorAll('h1, h2, h3'));
        #         return headings
        #             .slice(0, 10)
        #             .map(h => h.textContent.trim().slice(0, 100))
        #             .filter(text => text.length > 0);
        #     }""")
        #
        #     content_info = self.page.evaluate("""() => {
        #         return {
        #             image_count: document.querySelectorAll('img').length,
        #             has_form: document.querySelectorAll('form').length > 0,
        #             has_table: document.querySelectorAll('table').length > 0
        #         };
        #     }""")
        #
        #     content = {
        #         "headings": headings,
        #         "image_count": content_info['image_count'],
        #         "has_form": content_info['has_form'],
        #         "has_table": content_info['has_table']
        #     }
        #
        #     # State indicators
        #     state_info = self.page.evaluate("""() => {
        #         const alerts = Array.from(document.querySelectorAll('[role="alert"], .alert, .error, .warning'));
        #         const modals = Array.from(document.querySelectorAll('[role="dialog"], .modal, [aria-modal="true"]'));
        #         const loading = Array.from(document.querySelectorAll('.loading, .spinner, [aria-busy="true"]'));
        #
        #         return {
        #             has_alerts: alerts.length > 0,
        #             alert_messages: alerts.slice(0, 3).map(a => a.textContent.trim().slice(0, 100)).filter(t => t),
        #             has_modals: modals.filter(m => {
        #                 const style = window.getComputedStyle(m);
        #                 return style.display !== 'none';
        #             }).length > 0,
        #             has_loading: loading.filter(l => {
        #                 const style = window.getComputedStyle(l);
        #                 return style.display !== 'none';
        #             }).length > 0
        #         };
        #     }""")
        #
        #     state = {
        #         "has_alerts": state_info['has_alerts'],
        #         "alert_messages": state_info['alert_messages'],
        #         "has_modals": state_info['has_modals'],
        #         "has_loading": state_info['has_loading']
        #     }
        #
        #     # Navigation
        #     breadcrumbs = self.page.evaluate("""() => {
        #         const crumbs = document.querySelectorAll('[aria-label*="breadcrumb"] a, .breadcrumb a, .breadcrumbs a');
        #         return Array.from(crumbs)
        #             .map(a => a.textContent.trim())
        #             .filter(text => text.length > 0);
        #     }""")
        #
        #     navigation = {
        #         "can_go_back": self.page.evaluate("() => window.history.length > 1"),
        #         "can_go_forward": False,  # Not easily detectable
        #         "breadcrumbs": breadcrumbs
        #     }
        #
        #     logger.info(f"✅ Page info collected: {len(buttons)} buttons, {len(links)} links, {len(inputs)} inputs")
        #
        #     return {
        #         "status": "success",
        #         "page": page_info,
        #         "interactive": interactive,
        #         "content": content,
        #         "state": state,
        #         "navigation": navigation
        #     }
        #
        # except Exception as e:
        #     logger.error(f"Failed to get page info: {e}")
        #     return {
        #         "status": "error",
        #         "message": f"Failed to get page info: {str(e)}"
        #     }

    def _take_screenshot(self) -> Optional[bytes]:
        """Take screenshot and return as raw bytes"""
        try:
            if not self.page:
                return None

            # Take screenshot as PNG bytes
            screenshot_bytes = self.page.screenshot(type='png', full_page=False)

            return screenshot_bytes
        except Exception as e:
            logger.error(f"Screenshot failed: {e}")
            return None

    def close(self):
        """Close browser session and cleanup"""
        try:
            # NOVA_ACT DISABLED: NovaAct cleanup commented out
            # Close NovaAct context manager first
            # if self.nova_client:
            #     try:
            #         self.nova_client.__exit__(None, None, None)
            #     except Exception as e:
            #         logger.warning(f"Error closing NovaAct client: {e}")

            # Close browser session
            if self.browser_session_client:
                try:
                    self.browser_session_client.stop()
                except Exception as e:
                    logger.warning(f"Error stopping browser session: {e}")

            self._connected = False
            logger.info(f"Browser session {self.session_id} closed")

        except Exception as e:
            logger.error(f"Error closing browser session: {e}")


def get_or_create_controller(session_id: Optional[str] = None) -> BrowserController:
    """Get existing controller or create new one (auto-detects session_id from agent context)"""
    # Auto-detect session_id from environment (set by ChatbotAgent)
    # Uses SESSION_ID (per-conversation) for isolated browser sessions
    if not session_id:
        session_id = os.getenv('SESSION_ID') or os.getenv('USER_ID') or "default"
        logger.info(f"Auto-detected browser session_id: {session_id}")

    if session_id not in _browser_sessions:
        logger.info(f"Creating new browser controller for session {session_id}")
        _browser_sessions[session_id] = BrowserController(session_id)
    return _browser_sessions[session_id]


def close_session(session_id: str):
    """Close and remove browser session"""
    if session_id in _browser_sessions:
        controller = _browser_sessions[session_id]
        controller.close()
        del _browser_sessions[session_id]
        logger.info(f"Closed and removed browser session {session_id}")
