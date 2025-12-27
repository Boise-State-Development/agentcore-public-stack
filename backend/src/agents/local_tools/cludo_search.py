"""
Cludo Search Tool - Strands Native
Searches Boise State University information using the Cludo search API
"""

import os
import json
import logging
from typing import Optional, Dict, Any, Literal
from strands import tool
import httpx

logger = logging.getLogger(__name__)

# Cludo API configuration
CLUDO_API_ENDPOINT = "https://api-us1.cludo.com/api/v3/10000203/10000303/search"
CLUDO_SITE_KEY = os.getenv("TOOL_CLUDO_SITE_KEY")

if not CLUDO_SITE_KEY:
    logger.warning("TOOL_CLUDO_SITE_KEY environment variable not set. Cludo search will not work without it.")

# Constants for Bedrock payload limits
MAX_BEDROCK_PAYLOAD_SIZE = 15000
MAX_RESULTS_BEFORE_TRUNCATION = 8


async def query_cludo_api(
    query: str,
    operator: str = "or",
    page_size: int = 10,
    filters: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Helper function for Cludo API requests
    
    Args:
        query: Search query string
        operator: Query operator ('and' or 'or')
        page_size: Number of results to return (1-100)
        filters: Optional advanced filters for specific content types
        
    Returns:
        API response data as dictionary
        
    Raises:
        httpx.HTTPError: If the API request fails
    """
    if not CLUDO_SITE_KEY:
        raise ValueError("TOOL_CLUDO_SITE_KEY environment variable not set")

    request_body: Dict[str, Any] = {
        "query": query,
        "operator": operator,
        "pageSize": min(max(page_size, 1), 100),  # Ensure between 1 and 100
    }

    if filters:
        request_body.update(filters)

    headers = {
        "Authorization": f"SiteKey {CLUDO_SITE_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(CLUDO_API_ENDPOINT, json=request_body, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"Cludo API HTTP error: {e.response.status_code} - {e.response.text}")
            raise
        except httpx.RequestError as e:
            logger.error(f"Cludo API request error: {e}")
            raise


@tool
async def search_boise_state(
    query: str,
    operator: Literal["and", "or"] = "or",
    page_size: int = 10,
    filters: Optional[Dict[str, Any]] = None
) -> str:
    """
    Search for information from Boise State University using the official Cludo search engine.
    Use this tool to find specific institutional information, policies, programs, directories,
    and other official Boise State resources. Returns page titles, URLs, descriptions, and
    relevant context snippets. For detailed page content, use the "fetch_url_content" tool
    with the URLs from search results. Supports advanced query operators and filtering.

    Args:
        query: Search query for Boise State information (e.g., "business administration",
               "campus map", "admissions requirements")
        operator: Query operator: "and" requires all terms to match, "or" allows any term
                  to match (default: "or")
        page_size: Number of results to return (default: 10, max: 100)
        filters: Optional advanced filters for specific Boise State content types or
                 categories (e.g., {"Category": ["Programs", "Departments"]})

    Returns:
        JSON string containing search results with success status, query, result count,
        and formatted results array with titles, URLs, descriptions, and highlights

    Examples:
        # Basic search
        search_boise_state("business administration")

        # Search with AND operator
        search_boise_state("computer science degree", operator="and")

        # Search with filters
        search_boise_state("academic programs", filters={"Category": ["Programs"]})
    """
    try:
        if not CLUDO_SITE_KEY:
            return json.dumps({
                "success": False,
                "error": "TOOL_CLUDO_SITE_KEY environment variable not set. Please configure it in your .env file to use Cludo search.",
                "query": query
            }, indent=2)

        results = await query_cludo_api(query, operator, page_size, filters)

        if not results.get("TypedDocuments"):
            return json.dumps({
                "success": False,
                "query": query,
                "result_count": 0,
                "message": f'No Boise State information found matching "{query}". Try refining your search with different keywords or terms.',
                "results": []
            }, indent=2)

        typed_documents = results["TypedDocuments"]
        
        # Limit results to prevent payload size issues
        max_results = min(len(typed_documents), page_size, MAX_RESULTS_BEFORE_TRUNCATION)
        
        formatted_results = []
        for result in typed_documents[:max_results]:
            fields = result.get("Fields", {})
            
            # Extract highlights for relevant context
            highlights = []
            if "Description" in fields and "Highlights" in fields["Description"]:
                highlights = fields["Description"]["Highlights"]
            elif "Content" in fields and "Highlights" in fields["Content"]:
                highlights = fields["Content"]["Highlights"]
            
            # Clean highlights by removing HTML tags
            clean_highlights = ""
            if highlights:
                clean_highlights = " ... ".join(highlights[:3])  # Get up to 3 highlight snippets
                clean_highlights = clean_highlights.replace("<b>", "").replace("</b>", "")
            
            # Get description or fall back to first content snippet
            description = ""
            if "Description" in fields and "Value" in fields["Description"]:
                description = fields["Description"]["Value"]
            elif "Content" in fields and "Values" in fields["Content"] and fields["Content"]["Values"]:
                description = fields["Content"]["Values"][0]
            
            if not description:
                description = "No description available"
            else:
                description = description[:300]  # Limit description length
            
            formatted_results.append({
                "index": len(formatted_results) + 1,
                "title": fields.get("Title", {}).get("Value", "Untitled"),
                "url": fields.get("Url", {}).get("Value", ""),
                "description": description,
                "highlights": clean_highlights,
                "source": fields.get("Domain", {}).get("Value", "Boise State University"),
            })

        # Build JSON response
        result_data = {
            "success": True,
            "query": query,
            "operator": operator,
            "total_results": len(typed_documents),
            "result_count": len(formatted_results),
            "results": formatted_results,
            "source": "Cludo Search API - Official Boise State University Search Engine",
            "tip": "For more detailed information from any of these pages, use the 'fetch_url_content' tool with the page URL to retrieve the full content."
        }

        # Convert to JSON string
        result_json = json.dumps(result_data, indent=2)

        # Check payload size and log warning if needed (but don't truncate JSON as it would break the format)
        if len(result_json) > MAX_BEDROCK_PAYLOAD_SIZE:
            logger.warning(
                f"🚨 PAYLOAD SIZE WARNING - Cludo result JSON ({len(result_json)} chars) exceeds safe limit "
                f"({MAX_BEDROCK_PAYLOAD_SIZE}). Consider reducing page_size parameter to prevent Bedrock errors."
            )

        logger.info(
            f"Cludo search completed: Found {len(typed_documents)} results, returning {len(formatted_results)}"
        )

        return result_json

    except ValueError as e:
        error_message = str(e)
        logger.error(f"Cludo search configuration error: {error_message}")
        return json.dumps({
            "success": False,
            "error": f"Configuration error: {error_message}. Please configure TOOL_CLUDO_SITE_KEY in your .env file.",
            "query": query
        }, indent=2)

    except httpx.HTTPStatusError as e:
        error_message = f"HTTP {e.response.status_code}: {e.response.reason_phrase}"
        logger.error(f"Cludo search HTTP error: {error_message}")
        return json.dumps({
            "success": False,
            "error": f"HTTP error: {error_message}. Please try again or refine your search query.",
            "query": query,
            "status_code": e.response.status_code
        }, indent=2)

    except Exception as e:
        error_message = str(e)
        logger.error(f"Cludo search failed: {error_message}", exc_info=True)
        return json.dumps({
            "success": False,
            "error": f"Search failed: {error_message}. Please try again or refine your search query.",
            "query": query
        }, indent=2)

