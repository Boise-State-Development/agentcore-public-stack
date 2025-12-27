"""Local tools for general-purpose tasks

This package contains tools that don't require specific AWS services:
- Weather lookup
- Web search
- URL fetching and content extraction
- Data visualization
- Cludo search (Boise State University)
"""

from .weather import get_current_weather
from .web_search import ddg_web_search
from .url_fetcher import fetch_url_content
from .visualization import create_visualization
from .cludo_search import search_boise_state

__all__ = [
    'get_current_weather',
    'ddg_web_search',
    'fetch_url_content',
    'create_visualization',
    'search_boise_state',
]
