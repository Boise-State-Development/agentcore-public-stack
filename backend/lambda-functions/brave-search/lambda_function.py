"""
Brave Search Lambda for AgentCore Gateway
Provides web, local, video, image, news, and summarizer search tools
"""
import json
import os
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger()
logger.setLevel(logging.INFO)

import requests
import boto3
from botocore.exceptions import ClientError

# Cache for API key
_api_key_cache: Optional[str] = None

# Brave Search API base URL
BRAVE_API_BASE = "https://api.search.brave.com/res/v1"


def lambda_handler(event, context):
    """
    Lambda handler for Brave Search tools via AgentCore Gateway

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
        tool_handlers = {
            'brave_web_search': brave_web_search,
            'brave_local_search': brave_local_search,
            'brave_video_search': brave_video_search,
            'brave_image_search': brave_image_search,
            'brave_news_search': brave_news_search,
            'brave_summarizer': brave_summarizer,
        }

        handler = tool_handlers.get(tool_name)
        if handler:
            return handler(event)
        else:
            return error_response(f"Unknown tool: {tool_name}")

    except Exception as e:
        logger.error(f"Error: {str(e)}", exc_info=True)
        return error_response(str(e))


def get_brave_api_key() -> Optional[str]:
    """
    Get Brave API key from Secrets Manager (with caching)
    """
    global _api_key_cache

    # Return cached key if available
    if _api_key_cache:
        return _api_key_cache

    # Check environment variable first (for local testing)
    api_key = os.getenv("BRAVE_API_KEY")
    if api_key:
        _api_key_cache = api_key
        return _api_key_cache

    # Get from Secrets Manager
    secret_name = os.getenv("BRAVE_CREDENTIALS_SECRET_NAME")
    if not secret_name:
        logger.error("BRAVE_CREDENTIALS_SECRET_NAME not set")
        return None

    try:
        session = boto3.session.Session()
        client = session.client(service_name='secretsmanager')

        get_secret_value_response = client.get_secret_value(SecretId=secret_name)

        # Parse secret (stored as JSON with api_key field)
        secret_str = get_secret_value_response['SecretString']
        credentials = json.loads(secret_str)

        # Cache for future calls
        _api_key_cache = credentials.get('api_key')
        logger.info("Brave API key loaded from Secrets Manager")

        return _api_key_cache

    except ClientError as e:
        logger.error(f"Failed to get Brave API key from Secrets Manager: {e}")
        return None


def make_brave_request(endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Make authenticated request to Brave Search API
    """
    api_key = get_brave_api_key()
    if not api_key:
        raise ValueError("Failed to get Brave API key")

    url = f"{BRAVE_API_BASE}/{endpoint}"
    headers = {
        "X-Subscription-Token": api_key,
        "Accept": "application/json",
    }

    # Remove None values from params
    params = {k: v for k, v in params.items() if v is not None}

    response = requests.get(url, headers=headers, params=params, timeout=30)

    if response.status_code == 401:
        raise ValueError("Invalid Brave API key")
    elif response.status_code == 429:
        raise ValueError("Brave API rate limit exceeded")
    elif response.status_code != 200:
        raise ValueError(f"Brave API error: {response.status_code} - {response.text}")

    return response.json()


def brave_web_search(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Performs comprehensive web searches with rich result types
    """
    query = params.get('query')
    if not query:
        return error_response("query parameter required")

    logger.info(f"Brave web search: query={query}")

    try:
        api_params = {
            'q': query,
            'country': params.get('country', 'US'),
            'search_lang': params.get('search_lang', 'en'),
            'ui_lang': params.get('ui_lang', 'en-US'),
            'count': min(params.get('count', 10), 20),
            'offset': min(params.get('offset', 0), 9),
            'safesearch': params.get('safesearch', 'moderate'),
            'freshness': params.get('freshness'),
            'text_decorations': params.get('text_decorations', True),
            'spellcheck': params.get('spellcheck', True),
            'summary': params.get('summary', False),
        }

        # Handle result_filter array
        result_filter = params.get('result_filter')
        if result_filter:
            api_params['result_filter'] = ','.join(result_filter) if isinstance(result_filter, list) else result_filter

        # Handle goggles array
        goggles = params.get('goggles')
        if goggles:
            api_params['goggles'] = goggles

        # Handle units
        units = params.get('units')
        if units:
            api_params['units'] = units

        data = make_brave_request('web/search', api_params)

        # Format results
        results = []
        web_results = data.get('web', {}).get('results', [])
        for idx, item in enumerate(web_results, 1):
            results.append({
                "index": idx,
                "title": item.get('title', 'No title'),
                "url": item.get('url', ''),
                "description": item.get('description', ''),
                "age": item.get('age'),
            })

        result_data = {
            "query": query,
            "results_count": len(results),
            "results": results,
        }

        # Include summary key if requested
        if params.get('summary') and 'summarizer' in data:
            result_data['summarizer_key'] = data.get('summarizer', {}).get('key')

        return success_response(json.dumps(result_data, indent=2))

    except ValueError as e:
        return error_response(str(e))
    except requests.exceptions.Timeout:
        return error_response("Brave API request timed out")
    except Exception as e:
        return error_response(f"Brave web search error: {str(e)}")


def brave_local_search(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Searches for local businesses and places with detailed information
    """
    query = params.get('query')
    if not query:
        return error_response("query parameter required")

    logger.info(f"Brave local search: query={query}")

    try:
        api_params = {
            'q': query,
            'country': params.get('country', 'US'),
            'search_lang': params.get('search_lang', 'en'),
            'ui_lang': params.get('ui_lang', 'en-US'),
            'count': min(params.get('count', 10), 20),
            'offset': min(params.get('offset', 0), 9),
            'safesearch': params.get('safesearch', 'moderate'),
            'spellcheck': params.get('spellcheck', True),
            # Force location results
            'result_filter': 'web,locations',
        }

        data = make_brave_request('web/search', api_params)

        # Extract location results
        locations = data.get('locations', {}).get('results', [])
        results = []
        for idx, loc in enumerate(locations, 1):
            results.append({
                "index": idx,
                "name": loc.get('name', 'Unknown'),
                "address": loc.get('address', ''),
                "phone": loc.get('phone'),
                "rating": loc.get('rating', {}).get('ratingValue'),
                "review_count": loc.get('rating', {}).get('ratingCount'),
                "hours": loc.get('openingHours'),
                "price_range": loc.get('priceRange'),
                "categories": loc.get('categories', []),
            })

        result_data = {
            "query": query,
            "results_count": len(results),
            "locations": results,
        }

        return success_response(json.dumps(result_data, indent=2))

    except ValueError as e:
        return error_response(str(e))
    except requests.exceptions.Timeout:
        return error_response("Brave API request timed out")
    except Exception as e:
        return error_response(f"Brave local search error: {str(e)}")


def brave_video_search(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Searches for videos with comprehensive metadata
    """
    query = params.get('query')
    if not query:
        return error_response("query parameter required")

    logger.info(f"Brave video search: query={query}")

    try:
        api_params = {
            'q': query,
            'country': params.get('country', 'US'),
            'search_lang': params.get('search_lang', 'en'),
            'ui_lang': params.get('ui_lang', 'en-US'),
            'count': min(params.get('count', 20), 50),
            'offset': min(params.get('offset', 0), 9),
            'spellcheck': params.get('spellcheck', True),
            'safesearch': params.get('safesearch', 'moderate'),
            'freshness': params.get('freshness'),
        }

        data = make_brave_request('videos/search', api_params)

        # Format video results
        results = []
        video_results = data.get('results', [])
        for idx, video in enumerate(video_results, 1):
            results.append({
                "index": idx,
                "title": video.get('title', 'No title'),
                "url": video.get('url', ''),
                "description": video.get('description', ''),
                "thumbnail": video.get('thumbnail', {}).get('src'),
                "duration": video.get('video', {}).get('duration'),
                "views": video.get('video', {}).get('views'),
                "creator": video.get('video', {}).get('creator'),
                "publisher": video.get('video', {}).get('publisher'),
                "age": video.get('age'),
            })

        result_data = {
            "query": query,
            "results_count": len(results),
            "videos": results,
        }

        return success_response(json.dumps(result_data, indent=2))

    except ValueError as e:
        return error_response(str(e))
    except requests.exceptions.Timeout:
        return error_response("Brave API request timed out")
    except Exception as e:
        return error_response(f"Brave video search error: {str(e)}")


def brave_image_search(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Searches for images (returns URLs only, no base64 encoding)
    """
    query = params.get('query')
    if not query:
        return error_response("query parameter required")

    logger.info(f"Brave image search: query={query}")

    try:
        api_params = {
            'q': query,
            'country': params.get('country', 'US'),
            'search_lang': params.get('search_lang', 'en'),
            'count': min(params.get('count', 50), 200),
            'safesearch': params.get('safesearch', 'strict'),
            'spellcheck': params.get('spellcheck', True),
        }

        data = make_brave_request('images/search', api_params)

        # Format image results
        results = []
        image_results = data.get('results', [])
        for idx, img in enumerate(image_results, 1):
            results.append({
                "index": idx,
                "title": img.get('title', 'No title'),
                "url": img.get('url', ''),
                "source_url": img.get('source', ''),
                "thumbnail": img.get('thumbnail', {}).get('src'),
                "width": img.get('properties', {}).get('width'),
                "height": img.get('properties', {}).get('height'),
                "format": img.get('properties', {}).get('format'),
            })

        result_data = {
            "query": query,
            "results_count": len(results),
            "images": results,
        }

        return success_response(json.dumps(result_data, indent=2))

    except ValueError as e:
        return error_response(str(e))
    except requests.exceptions.Timeout:
        return error_response("Brave API request timed out")
    except Exception as e:
        return error_response(f"Brave image search error: {str(e)}")


def brave_news_search(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Searches for current news articles
    """
    query = params.get('query')
    if not query:
        return error_response("query parameter required")

    logger.info(f"Brave news search: query={query}")

    try:
        api_params = {
            'q': query,
            'country': params.get('country', 'US'),
            'search_lang': params.get('search_lang', 'en'),
            'ui_lang': params.get('ui_lang', 'en-US'),
            'count': min(params.get('count', 20), 50),
            'offset': min(params.get('offset', 0), 9),
            'spellcheck': params.get('spellcheck', True),
            'safesearch': params.get('safesearch', 'moderate'),
            'freshness': params.get('freshness', 'pd'),  # Default to past day
        }

        # Handle goggles array
        goggles = params.get('goggles')
        if goggles:
            api_params['goggles'] = goggles

        data = make_brave_request('news/search', api_params)

        # Format news results
        results = []
        news_results = data.get('results', [])
        for idx, article in enumerate(news_results, 1):
            results.append({
                "index": idx,
                "title": article.get('title', 'No title'),
                "url": article.get('url', ''),
                "description": article.get('description', ''),
                "source": article.get('meta_url', {}).get('hostname'),
                "age": article.get('age'),
                "breaking": article.get('breaking', False),
                "thumbnail": article.get('thumbnail', {}).get('src'),
            })

        result_data = {
            "query": query,
            "results_count": len(results),
            "articles": results,
        }

        return success_response(json.dumps(result_data, indent=2))

    except ValueError as e:
        return error_response(str(e))
    except requests.exceptions.Timeout:
        return error_response("Brave API request timed out")
    except Exception as e:
        return error_response(f"Brave news search error: {str(e)}")


def brave_summarizer(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generates AI-powered summaries from web search results
    Requires a summary key obtained from brave_web_search with summary=true
    """
    key = params.get('key')
    if not key:
        return error_response("key parameter required (obtain from brave_web_search with summary=true)")

    logger.info(f"Brave summarizer: key={key[:20]}...")

    try:
        api_params = {
            'key': key,
            'entity_info': params.get('entity_info', False),
        }

        data = make_brave_request('summarizer/search', api_params)

        # Extract summary
        summary = data.get('summary', [])

        # Handle inline references if requested
        result_text = ""
        references = []

        for item in summary:
            if item.get('type') == 'token':
                result_text += item.get('data', '')
            elif item.get('type') == 'url':
                ref_idx = len(references) + 1
                references.append({
                    "index": ref_idx,
                    "url": item.get('data', ''),
                })
                if params.get('inline_references', False):
                    result_text += f"[{ref_idx}]"

        result_data = {
            "summary": result_text.strip(),
            "references": references if references else None,
            "title": data.get('title'),
        }

        return success_response(json.dumps(result_data, indent=2))

    except ValueError as e:
        return error_response(str(e))
    except requests.exceptions.Timeout:
        return error_response("Brave API request timed out")
    except Exception as e:
        return error_response(f"Brave summarizer error: {str(e)}")


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
