"""
Cludo Search Tool - Strands Native
Searches Boise State University information using the Cludo search API

Supports advanced Cludo API features:
- Pagination for multi-page results
- Spelling corrections (FixedQuery)
- Faceted search with category counts
- Related searches for query refinement
- Sorting by specific fields
- Advanced filtering (value, range, date, exclusion)
"""

import os
import json
import logging
from typing import Optional, Dict, Any, List, Literal, Union
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
    page: int = 1,
    page_size: int = 10,
    sort: Optional[str] = None,
    filters: Optional[Dict[str, Any]] = None,
    not_filters: Optional[Dict[str, Any]] = None,
    range_filters: Optional[List[Dict[str, Any]]] = None,
    enable_related_searches: bool = False,
    enable_facet_filtering: bool = True,
) -> Dict[str, Any]:
    """
    Helper function for Cludo API requests

    Args:
        query: Search query string
        operator: Query operator ('and' or 'or')
        page: Page number for pagination (1-indexed)
        page_size: Number of results per page (1-100)
        sort: Field name to sort by (overrides default relevance ranking)
        filters: Value filters applied during search (affects ranking and facets)
        not_filters: Exclusion filters (documents matching these are excluded)
        range_filters: Range filters for numeric/date fields
        enable_related_searches: Include related search suggestions
        enable_facet_filtering: Enable facet-level filtering

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
        "page": max(page, 1),
        "perPage": min(max(page_size, 1), 100),
        "enableFacetFiltering": enable_facet_filtering,
        "responseType": "JsonObject",
    }

    if sort:
        request_body["sort"] = sort

    if enable_related_searches:
        request_body["enableRelatedSearches"] = True

    if filters:
        request_body["filters"] = filters

    if not_filters:
        request_body["notFilters"] = not_filters

    if range_filters:
        # Range filters format: [{"field": "Price", "min": 20, "max": 100}]
        request_body["rangeFilters"] = range_filters

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
    page: int = 1,
    page_size: int = 10,
    sort: Optional[str] = None,
    filters: Optional[Dict[str, Any]] = None,
    not_filters: Optional[Dict[str, Any]] = None,
    include_facets: bool = False,
    include_related_searches: bool = False,
) -> str:
    """
    Search for information from Boise State University using the official Cludo search engine.
    Use this tool to find specific institutional information, policies, programs, directories,
    and other official Boise State resources. Returns page titles, URLs, descriptions, and
    relevant context snippets. For detailed page content, use the "fetch_url_content" tool
    with the URLs from search results.

    Args:
        query: Search query for Boise State information. Supports advanced syntax:
               - Phrases: "exact phrase" for exact matching
               - Operators: AND, OR, NOT (e.g., "computer AND science")
               - Wildcards: * for multiple chars, ? for single char (e.g., "comput*")
               - Fuzzy: term~ for misspellings (e.g., "bussiness~")
               - Boosting: term^2 to increase relevance weight
        operator: Query operator: "and" requires all terms to match, "or" allows any term
                  to match (default: "or")
        page: Page number for pagination, 1-indexed (default: 1). Use with page_size
              to navigate through large result sets.
        page_size: Number of results per page (default: 10, max: 100)
        sort: Optional field name to sort results by (e.g., "Date", "Title").
              Overrides default relevance-based ranking.
        filters: Value filters to include specific content types or categories
                 (e.g., {"Category": ["Programs", "Departments"]})
        not_filters: Exclusion filters to exclude specific content types
                     (e.g., {"Category": ["News"]})
        include_facets: If True, includes available facets (categories) with counts
                        for drilling down into results (default: False)
        include_related_searches: If True, includes related search suggestions
                                  to help refine the query (default: False)

    Returns:
        JSON string containing:
        - success: Boolean indicating if search succeeded
        - query: The original search query
        - spelling_suggestion: Suggested correction if query may have typos
        - pagination: Current page, total pages, total results
        - results: Array of formatted results with titles, URLs, descriptions, highlights
        - facets: (if requested) Available categories with document counts
        - related_searches: (if requested) Suggested related queries

    Examples:
        # Basic search
        search_boise_state("business administration")

        # Paginated search - get page 2
        search_boise_state("scholarships", page=2, page_size=10)

        # Search with fuzzy matching for misspellings
        search_boise_state("admisions~")

        # Search with exact phrase
        search_boise_state('"computer science degree"')

        # Get facets to see available categories
        search_boise_state("programs", include_facets=True)

        # Exclude news articles from results
        search_boise_state("campus events", not_filters={"Category": ["News"]})

        # Sort by date instead of relevance
        search_boise_state("announcements", sort="Date")
    """
    try:
        if not CLUDO_SITE_KEY:
            return json.dumps({
                "success": False,
                "error": "TOOL_CLUDO_SITE_KEY environment variable not set. Please configure it in your .env file to use Cludo search.",
                "query": query
            }, indent=2)

        results = await query_cludo_api(
            query=query,
            operator=operator,
            page=page,
            page_size=page_size,
            sort=sort,
            filters=filters,
            not_filters=not_filters,
            enable_related_searches=include_related_searches,
            enable_facet_filtering=include_facets,
        )

        # Extract total document count for pagination
        total_documents = results.get("TotalDocument", 0)
        total_pages = (total_documents + page_size - 1) // page_size if total_documents > 0 else 0

        # Check for spelling correction suggestion
        spelling_suggestion = results.get("FixedQuery")

        if not results.get("TypedDocuments"):
            response_data: Dict[str, Any] = {
                "success": True,
                "query": query,
                "result_count": 0,
                "message": f'No Boise State information found matching "{query}". Try refining your search with different keywords or terms.',
                "results": []
            }
            # Include spelling suggestion if available
            if spelling_suggestion and spelling_suggestion != query:
                response_data["spelling_suggestion"] = spelling_suggestion
                response_data["message"] = f'No results found for "{query}". Did you mean: "{spelling_suggestion}"?'
            return json.dumps(response_data, indent=2)

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
        result_data: Dict[str, Any] = {
            "success": True,
            "query": query,
            "operator": operator,
            "pagination": {
                "current_page": page,
                "total_pages": total_pages,
                "total_results": total_documents,
                "results_on_page": len(formatted_results),
                "has_more": page < total_pages,
            },
            "results": formatted_results,
            "source": "Cludo Search API - Official Boise State University Search Engine",
            "tip": "For more detailed information from any of these pages, use the 'fetch_url_content' tool with the page URL to retrieve the full content."
        }

        # Include spelling suggestion if query was corrected
        if spelling_suggestion and spelling_suggestion != query:
            result_data["spelling_suggestion"] = spelling_suggestion

        # Include facets if requested
        if include_facets and results.get("Facets"):
            facets_data = []
            for facet in results["Facets"]:
                facet_name = facet.get("Name", "Unknown")
                facet_items = []
                for item in facet.get("Items", [])[:10]:  # Limit to top 10 facet values
                    facet_items.append({
                        "value": item.get("Value", ""),
                        "count": item.get("Count", 0),
                    })
                if facet_items:
                    facets_data.append({
                        "name": facet_name,
                        "items": facet_items,
                    })
            if facets_data:
                result_data["facets"] = facets_data

        # Include related searches if requested
        if include_related_searches and results.get("RelatedSearchDocuments"):
            related = []
            for rel in results["RelatedSearchDocuments"][:5]:  # Limit to 5 suggestions
                if isinstance(rel, dict):
                    related.append(rel.get("Query", rel.get("Title", "")))
                elif isinstance(rel, str):
                    related.append(rel)
            if related:
                result_data["related_searches"] = related

        # Convert to JSON string
        result_json = json.dumps(result_data, indent=2)

        # Check payload size and log warning if needed (but don't truncate JSON as it would break the format)
        if len(result_json) > MAX_BEDROCK_PAYLOAD_SIZE:
            logger.warning(
                f"PAYLOAD SIZE WARNING - Cludo result JSON ({len(result_json)} chars) exceeds safe limit "
                f"({MAX_BEDROCK_PAYLOAD_SIZE}). Consider reducing page_size parameter to prevent Bedrock errors."
            )

        logger.info(
            f"Cludo search completed: query='{query}', page={page}/{total_pages}, "
            f"found {total_documents} total, returning {len(formatted_results)}"
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

