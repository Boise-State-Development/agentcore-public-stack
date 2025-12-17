"""
Google Custom Search Lambda for AgentCore Gateway
Provides web search and image search
"""
import json
import os
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Import after logger setup
import requests
import boto3
from botocore.exceptions import ClientError

# Cache for API credentials
_credentials_cache: Optional[Dict[str, str]] = None

def lambda_handler(event, context):
    """
    Lambda handler for Google Search tools via AgentCore Gateway

    Gateway unwraps tool arguments and passes them directly to Lambda
    """
    try:
        logger.info(f"Event: {json.dumps(event)}")

        # Get tool name from context (set by AgentCore Gateway)
        tool_name = 'unknown'
        if hasattr(context, 'client_context') and context.client_context:
            if hasattr(context.client_context, 'custom'):
                tool_name = context.client_context.custom.get('bedrockAgentCoreToolName', '')
                if '___' in tool_name:
                    tool_name = tool_name.split('___')[-1]

        logger.info(f"Tool name: {tool_name}")

        # Route to appropriate tool
        if tool_name == 'google_web_search':
            return google_web_search(event)
        elif tool_name == 'google_image_search':
            return google_image_search(event)
        else:
            return error_response(f"Unknown tool: {tool_name}")

    except Exception as e:
        logger.error(f"Error: {str(e)}", exc_info=True)
        return error_response(str(e))


def get_google_credentials() -> Optional[Dict[str, str]]:
    """
    Get Google API credentials from Secrets Manager (with caching)

    Returns dict with 'api_key' and 'search_engine_id'
    """
    global _credentials_cache

    # Return cached credentials if available
    if _credentials_cache:
        return _credentials_cache

    # Check environment variables first (for local testing)
    api_key = os.getenv("GOOGLE_API_KEY")
    search_engine_id = os.getenv("GOOGLE_SEARCH_ENGINE_ID")

    if api_key and search_engine_id:
        _credentials_cache = {
            'api_key': api_key,
            'search_engine_id': search_engine_id
        }
        return _credentials_cache

    # Get from Secrets Manager
    secret_name = os.getenv("GOOGLE_CREDENTIALS_SECRET_NAME")
    if not secret_name:
        logger.error("GOOGLE_CREDENTIALS_SECRET_NAME not set")
        return None

    try:
        session = boto3.session.Session()
        client = session.client(service_name='secretsmanager')

        get_secret_value_response = client.get_secret_value(SecretId=secret_name)

        # Parse secret (stored as JSON)
        secret_str = get_secret_value_response['SecretString']
        credentials = json.loads(secret_str)

        # Cache for future calls
        _credentials_cache = credentials
        logger.info("✅ Google credentials loaded from Secrets Manager")

        return credentials

    except ClientError as e:
        logger.error(f"Failed to get Google credentials from Secrets Manager: {e}")
        return None


def check_image_accessible(url: str, timeout: int = 5) -> bool:
    """Check if image URL is accessible"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
            'Referer': 'https://www.google.com/'
        }

        # Use HEAD request to check accessibility
        response = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True)

        if response.status_code == 200:
            content_type = response.headers.get('content-type', '').lower()
            return 'image' in content_type

        # If HEAD fails, try small range request
        if response.status_code == 405:
            headers['Range'] = 'bytes=0-1023'
            response = requests.get(url, headers=headers, timeout=timeout)
            return response.status_code in [200, 206]

        return False
    except Exception:
        return False


def google_web_search(params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute Google web search"""

    # Get credentials
    credentials = get_google_credentials()
    if not credentials:
        return error_response("Failed to get Google API credentials")

    # Extract parameters (Gateway unwraps them)
    query = params.get('query')
    num_results = 5

    if not query:
        return error_response("query parameter required")

    logger.info(f"Google web search: query={query}")

    # Prepare API request
    url = "https://www.googleapis.com/customsearch/v1"
    request_params = {
        'key': credentials['api_key'],
        'cx': credentials['search_engine_id'],
        'q': query,
        'num': num_results,
        'safe': 'active'
    }

    try:
        response = requests.get(url, params=request_params, timeout=30)

        if response.status_code == 400:
            return error_response("Invalid Google API request")
        elif response.status_code == 403:
            return error_response("Google API key invalid or quota exceeded")
        elif response.status_code != 200:
            return error_response(f"Google API error: {response.status_code}")

        data = response.json()

        # Format results
        results = []
        if 'items' in data:
            for idx, item in enumerate(data['items'], 1):
                results.append({
                    "index": idx,
                    "title": item.get('title', 'No title'),
                    "link": item.get('link', 'No link'),
                    "snippet": item.get('snippet', 'No snippet')
                })

        result_data = {
            "query": query,
            "results_count": len(results),
            "results": results
        }

        return success_response(json.dumps(result_data, indent=2))

    except requests.exceptions.Timeout:
        return error_response("Google API request timed out")
    except Exception as e:
        return error_response(f"Google web search error: {str(e)}")


def google_image_search(params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute Google image search"""

    # Get credentials
    credentials = get_google_credentials()
    if not credentials:
        return error_response("Failed to get Google API credentials")

    # Extract parameters
    query = params.get('query')
    num_results = 5

    if not query:
        return error_response("query parameter required")

    logger.info(f"Google image search: query={query}")

    # Prepare API request
    url = "https://www.googleapis.com/customsearch/v1"
    request_params = {
        'key': credentials['api_key'],
        'cx': credentials['search_engine_id'],
        'q': query,
        'searchType': 'image',
        'num': 10,  # Get max results to filter for accessible ones
        'safe': 'active'
    }

    try:
        response = requests.get(url, params=request_params, timeout=30)

        if response.status_code == 400:
            return error_response("Invalid Google API request")
        elif response.status_code == 403:
            return error_response("Google API key invalid or quota exceeded")
        elif response.status_code != 200:
            return error_response(f"Google API error: {response.status_code}")

        data = response.json()

        # Filter for accessible images
        accessible_results = []
        all_items = data.get('items', [])

        for item in all_items:
            image_url = item.get('link', '')

            if image_url and check_image_accessible(image_url):
                accessible_results.append({
                    "title": item.get('title', 'Untitled'),
                    "link": item.get('link', 'No link'),
                    "snippet": item.get('snippet', 'No description'),
                    "image_url": image_url
                })

                # Stop when we have enough
                if len(accessible_results) >= num_results:
                    break

        # Format results
        formatted_results = []
        for idx, r in enumerate(accessible_results, 1):
            formatted_results.append({
                "index": idx,
                "title": r['title'],
                "link": r['link'],
                "snippet": r['snippet'],
                "image_url": r['image_url']
            })

        result_data = {
            "query": query,
            "results_count": len(formatted_results),
            "results": formatted_results
        }

        return success_response(json.dumps(result_data, indent=2))

    except requests.exceptions.Timeout:
        return error_response("Google API request timed out")
    except Exception as e:
        return error_response(f"Google image search error: {str(e)}")


def success_response(content: str) -> Dict[str, Any]:
    """Format successful MCP response"""
    return {
        'statusCode': 200,
        'body': json.dumps({
            'content': [{
                'type': 'text',
                'text': content
            }]
        })
    }


def error_response(message: str) -> Dict[str, Any]:
    """Format error response"""
    logger.error(f"Error response: {message}")
    return {
        'statusCode': 400,
        'body': json.dumps({
            'error': message
        })
    }
